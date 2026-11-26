"""Entry Pack Graph — 콘테스트 엔트리를 위한 변주·검증·포장 파이프라인.

입상 전 단계가 목표다. 납품(소유권 이전)용 원본이 아니라 **심사용 제출물**을 만든다.

```
브리프
  │
  ▼
spec      플랫폼·규격·톤·시드 확정 (결정론)
  │
  ▼
knowledge SearXNG 레퍼런스 + llm-wiki 원칙 + 카탈로그 규모
  │
  ▼
direction LLM 3~5 방향 (모티프·시각 언어)
  │
  ▼
variants  방향 × 스타일 × 테마 → 후보 (시드 고정, 재현 가능)
  │
  ▼
render    render_recipe (결정론 렌더)
  │
  ▼
critic    lint_render (대비·겹침·라이선스·계약)
  │
  ▼
gate      상위 N 선별 (FAIL 제외)
  │
  ▼
hitl      사람이 선택 (interrupt)
  │
  ▼
pack      PNG·JPG·설명문·meta.json
  │
  ▼
mockup    평면 목업 (카드·포스터)
```

사용::

    from graphs.entry_pack_graph import build_entry_pack_graph

    graph = build_entry_pack_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "entry-1"}}
    graph.invoke({
        "brief": "핸드메이드 티 브랜드 랜딩 — 차분하고 고급스러운, 웹",
        "spec": {"platform": "99designs", "image_width": 2000},
    }, config)
    # hitl에서 멈춘다. state["options"]를 보고 selection을 넣어 재개.
    graph.update_state(config, {"selection": ["variant-id", ...]})
    graph.invoke(None, config)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.prompts import ChatPromptTemplate  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

import craft  # noqa: E402
from graphs.core import artifacts as core_artifacts  # noqa: E402
from graphs.core import env as core_env  # noqa: E402
from graphs.core import exec as core_exec  # noqa: E402
from graphs.llm import chat_model  # noqa: E402
from graphs.profiles import base as profiles_base  # noqa: E402
from graphs.profiles import presets as profile_presets  # noqa: E402
from graphs.profiles.base import svg_size as _svg_size  # noqa: E402
from graphs.profiles.base import svg_width as _svg_width  # noqa: E402
from graphs.profiles.web import WebProfile  # noqa: E402
from graphs.profiles.web import direction_text as _direction_text  # noqa: E402
from graphs.profiles.web import fallback_direction as _fallback_direction  # noqa: E402

#: 경로·주소는 core/env가 단일 정의한다. 여기 이름은 기존 호출부·테스트를 위한 얇은
#: 별칭일 뿐, 기본값을 다시 선언하지 않는다(`SONAGI_ENTRY_OUT` 별칭은 core가 안다).
_PATHS = core_env.load_paths()

ASSETS_DIR: Path = _PATHS.assets_dir
CATALOG: Path = _PATHS.catalog
TOKENS: Path = _PATHS.tokens
WIKI_DIR: Path = _PATHS.wiki_dir
OUT_ROOT: Path = core_env.out_root("entry_pack")
SEARXNG_URL: str = _PATHS.searxng_url
VECTORCRAFT: str = _PATHS.vectorcraft

DEFAULT_SPEC: Dict[str, Any] = WebProfile().defaults()


class EntryState(TypedDict, total=False):
    brief: str
    content: Optional[str]
    spec: Dict[str, Any]
    ir: Dict[str, Any]
    knowledge: Dict[str, Any]
    directions: List[Dict[str, Any]]
    variants: List[Dict[str, Any]]
    recipe: Optional[str]
    renders: List[Dict[str, Any]]
    critiques: List[Dict[str, Any]]
    options: List[Dict[str, Any]]
    selection: List[str]
    approval_status: str  # "pending" | "approved"
    entry: Dict[str, Any]
    mockups: List[Dict[str, Any]]
    run_id: str
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(args, timeout: int = 300) -> str:
    """core/exec로 실행한다. 실패는 `CommandError`(RuntimeError)로 올라온다.

    호출부·테스트가 monkeypatch할 수 있도록 얇은 래퍼만 남긴다.
    """
    result = core_exec.run([str(arg) for arg in args], timeout=timeout, cwd=ASSETS_DIR)
    return result.stdout


def _artifact_entry(path: Any, root: Path) -> Optional[Dict[str, Any]]:
    """manifest 항목 하나(profile 공통 구현으로 위임)."""
    return profiles_base.artifact_entry(path, root)


def _collect_artifacts(paths: List[Any], root: Path) -> List[Dict[str, Any]]:
    """경로 목록 → manifest 산출물 항목(중복 제거, 순서 유지)."""
    return profiles_base.collect_artifacts(paths, root)


def _build_manifest(
    state: EntryState,
    run_dir: Path,
    artifacts: List[Dict[str, Any]],
    *,
    status: str = "ok",
) -> Dict[str, Any]:
    """입력 지문(브리프 해시·시드·spec 해시)과 산출물 해시를 담은 run manifest."""
    return profiles_base.run_manifest(
        state, run_dir, artifacts, kind="entry_pack", status=status
    )


def _profile_context(run_dir: Optional[Path] = None) -> profiles_base.ProfileContext:
    """지금 이 모듈의 실행 훅으로 컨텍스트를 만든다(테스트 monkeypatch 전파)."""
    return profiles_base.ProfileContext(
        run=_run,
        convert_png=_convert_png,
        to_jpg=_to_jpg,
        out_root=OUT_ROOT,
        assets_dir=ASSETS_DIR,
        catalog=CATALOG,
        tokens=TOKENS,
        run_dir=run_dir,
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def spec_node(state: EntryState) -> Dict[str, Any]:
    """브리프를 IR로 바꾸고 플랫폼·규격·시드 기본값을 확정한다."""
    raw = state.get("brief") or state.get("content") or ""
    ir = craft.parse_brief(raw)
    raw_spec = dict(state.get("spec") or {})
    # profile 해석은 기본값 병합보다 먼저 — web/print가 서로 다른 기본값을 갖는다.
    profile = profiles_base.profile_for_spec(raw_spec)
    spec = dict(profile.defaults())
    spec.update(raw_spec)
    spec["tones"] = list(spec.get("tones") or ir.get("tones") or ["편집"])
    spec["media"] = spec.get("media") or ir.get("media", "web")
    if "themes" not in spec:
        spec["themes"] = ["light", "dark"] if spec["media"] == "web" else ["light"]
    if "breakpoints" not in spec:
        spec["breakpoints"] = ["desktop"] if spec["media"] == "web" else ["mobile"]
    # 플랫폼을 명시했을 때만 프리셋을 병합한다 — platform 없는 web/print 기본 동작 불변.
    if raw_spec.get("platform"):
        spec, _, _ = profile_presets.apply(spec)
    if not spec.get("seed"):
        spec["seed"] = hashlib.sha1(str(raw).encode("utf-8")).hexdigest()[:8]
    # 선택 훅: profile이 입력을 먼저 검증하면 knowledge·LLM 호출 전에 멈춘다.
    validate = getattr(profile, "validate", None)
    if callable(validate):
        validate(spec)
    run_id = state.get("run_id") or f"entry-{uuid.uuid4().hex[:8]}"
    return {"ir": ir, "spec": spec, "run_id": run_id, "status": "specified"}


def _searxng(query: str, limit: int = 5) -> List[Dict[str, str]]:
    url = f"{SEARXNG_URL.rstrip('/')}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"}
    )
    with urllib.request.urlopen(url, timeout=6) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    rows = payload.get("results") or []
    return [
        {"title": r.get("title", ""), "url": r.get("url", "")}
        for r in rows[:limit]
        if r.get("url")
    ]


def _wiki_notes(query: str, limit: int = 3) -> List[Dict[str, str]]:
    if not WIKI_DIR.is_dir():
        return []
    done = core_exec.run(
        ["rg", "-il", "--glob", "*.md", query, str(WIKI_DIR)],
        timeout=20,
        check=False,
    )
    notes = []
    for line in done.stdout.splitlines()[:limit]:
        path = Path(line)
        if not path.is_file():
            continue
        title = path.stem
        notes.append({"title": title, "path": str(path)})
    return notes


def knowledge_node(state: EntryState) -> Dict[str, Any]:
    """레퍼런스와 사내 원칙을 모은다. 실패해도 파이프라인은 계속 간다."""
    spec = state.get("spec") or {}
    if spec.get("skip_knowledge"):
        return {"knowledge": {"skipped": True}, "status": "knowledge_skipped"}

    ir = state.get("ir") or craft.parse_brief(state.get("brief", ""))
    query = " ".join(
        [ir.get("purpose", "")[:80], *[str(t) for t in ir.get("tones") or []]]
    ).strip()
    knowledge: Dict[str, Any] = {"query": query, "web": [], "wiki": [], "catalog": 0}
    errors = []

    try:
        knowledge["web"] = _searxng(f"{query} design reference")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"searxng: {str(exc)[:120]}")

    try:
        knowledge["wiki"] = _wiki_notes(ir.get("tones", ["디자인"])[0])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"wiki: {str(exc)[:120]}")

    try:
        if CATALOG.is_file():
            knowledge["catalog"] = len(
                json.loads(CATALOG.read_text(encoding="utf-8")).get("items", [])
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"catalog: {str(exc)[:120]}")

    if errors:
        knowledge["errors"] = errors
    return {"knowledge": knowledge, "status": "knowledge"}


def direction_node(state: EntryState) -> Dict[str, Any]:
    """LLM으로 서로 다른 시각 방향 3~5개를 만든다."""
    from graphs.profiles.base import ConceptOutput

    spec = state.get("spec") or {}
    ir = state.get("ir") or craft.parse_brief(state.get("brief", ""))
    if spec.get("skip_llm"):
        return {
            "directions": [_fallback_direction(ir)],
            "status": "directions_fallback",
        }

    knowledge = state.get("knowledge") or {}
    refs = "; ".join(w.get("title", "") for w in knowledge.get("web") or [])
    notes = "\n".join(f"- {n['title']}" for n in knowledge.get("wiki") or [])
    brief = state.get("brief", "")
    if refs:
        brief = f"{brief}\n\n[레퍼런스]\n{refs}"
    if notes:
        brief = f"{brief}\n\n[사내 원칙]\n{notes}"

    llm = chat_model(
        "ENTRY_DIRECTION_LLM_MODEL", default_model="gemini-3-flash", temperature=0.4
    )
    structured = llm.with_structured_output(ConceptOutput)
    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "당신은 시각 디자이너입니다. 콘테스트 브리프를 읽고 3~5개의 서로 다른 방향을 제안하세요.\n"
                "이 결과는 렌더러가 스타일을 고르는 데 쓰입니다. 방향마다 형태 모티프 2~4개와\n"
                "선·면·질감·비례에 대한 시각 언어를 구체적으로 적으세요. 비슷한 방향은 피하세요.",
            ),
            ("user", "브리프: {brief}"),
        ]
    )
    try:
        result = (prompt | structured).invoke({"brief": brief})
        directions = [d.model_dump() for d in result.directions]
    except Exception:  # noqa: BLE001
        directions = [_fallback_direction(ir)]
    for d in directions:
        d["motifs"] = [m for m in (d.get("motifs") or []) if isinstance(m, str)][:4]
    return {"directions": directions, "status": "directed"}


def variants_node(state: EntryState) -> Dict[str, Any]:
    """방향 × 스타일 × 브레이크포인트 × 테마로 후보를 만든다(profile에 위임)."""
    spec = state.get("spec") or {}
    ir = state.get("ir") or craft.parse_brief(state.get("brief", ""))
    directions = state.get("directions") or [_fallback_direction(ir)]
    styles = craft.load_registry(ASSETS_DIR)
    profile = profiles_base.profile_for_spec(spec)
    variants = profile.plan({**ir, "directions": directions}, spec, styles)
    return {"variants": variants, "status": "variants"}


def render_node(state: EntryState) -> Dict[str, Any]:
    """후보마다 렌더한다(profile에 위임, 결정론)."""
    spec = state.get("spec") or {}
    variants = state.get("variants") or []
    if not variants:
        return {"renders": [], "status": "render_empty"}

    run_id = state.get("run_id", "entry")
    run_dir = OUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = _profile_context(run_dir)
    ctx.extra["content"] = state.get("content") or state.get("brief") or ""
    profile = profiles_base.profile_for_spec(spec)

    renders = []
    for variant in variants:
        rendered = profile.render(variant, ctx)
        if rendered:
            renders.append(rendered)

    render_paths: List[Any] = []
    for render in renders:
        render_paths.append(render["svg"])
        if render.get("manifest"):
            render_paths.append(render["manifest"])
    manifest = _build_manifest(
        {**state, "renders": renders},
        run_dir,
        _collect_artifacts(render_paths, run_dir),
        status="rendered",
    )
    manifest_path = core_artifacts.write_manifest(run_dir, manifest)

    return {
        "recipe": ctx.extra.get("recipe"),
        "renders": renders,
        "manifest": str(manifest_path),
        "manifest_digest": core_artifacts.manifest_digest(manifest),
        "status": "rendered",
    }


def critic_node(state: EntryState) -> Dict[str, Any]:
    """린트로 기계 판정한다. FAIL은 선택지에서 빠진다.

    profile이 자체 게이트를 가지면(print 등) web 린트 규칙(hero·레이어 수·contract)은
    적용 대상이 아니다. 이때는 후보를 통과로 두고, 실제 검사는 profile의 pack 게이트가
    맡는다 — 여기서 막으면 사람이 고를 선택지가 사라진다.
    """
    spec = state.get("spec") or {}
    profile = profiles_base.profile_for_spec(spec)
    if profile.gates():
        critiques = [
            {
                "id": render["id"],
                "verdict": "pass",
                "findings": [],
                "plan_score": render.get("score", 0),
                "svg": render["svg"],
                "direction": render.get("direction"),
                "style": render.get("style"),
                "theme": render.get("theme"),
            }
            for render in state.get("renders") or []
        ]
        return {"critiques": critiques, "status": "critiqued"}

    critiques = []
    for render in state.get("renders") or []:
        cmd = [
            sys.executable,
            ASSETS_DIR / "scripts/lint_render.py",
            "--svg",
            render["svg"],
            "--json",
        ]
        if render.get("manifest"):
            cmd += ["--manifest", render["manifest"]]
        cmd += ["--tokens", TOKENS]
        try:
            payload = json.loads(_run(cmd))
        except Exception as exc:  # noqa: BLE001
            payload = {
                "verdict": "fail",
                "findings": [{"severity": "fail", "rule": "lint.error"}],
                "error": str(exc)[:200],
            }
        payload["id"] = render["id"]
        payload["svg"] = render["svg"]
        payload["plan_score"] = render.get("score", 0)
        payload["direction"] = render.get("direction")
        payload["style"] = render.get("style")
        payload["theme"] = render.get("theme")
        critiques.append(payload)
    return {"critiques": critiques, "status": "critiqued"}


def gate_node(state: EntryState) -> Dict[str, Any]:
    """상위 N개를 고르고 사람이 볼 선택지를 만든다."""
    spec = state.get("spec") or {}
    options = craft.pick(state.get("critiques") or [], limit=int(spec.get("top_n", 9)))
    by_id = {r["id"]: r for r in state.get("renders") or []}
    for option in options:
        option["svg"] = (by_id.get(option["id"]) or {}).get("svg")
        option["direction"] = (by_id.get(option["id"]) or {}).get("direction")
        option["style"] = (by_id.get(option["id"]) or {}).get("style")
        option["theme"] = (by_id.get(option["id"]) or {}).get("theme")
    return {"options": options, "status": "gated"}


def hitl_node(state: EntryState) -> Dict[str, Any]:
    """사람이 고른다. selection이 비어 있으면 상위 3개를 쓴다."""
    if state.get("approval_status") == "approved" and state.get("selection"):
        return {"status": "selected"}
    options = state.get("options") or []
    if state.get("approval_status") == "approved" and options:
        # 자동 선택은 방향이 겹치지 않게 고른다(같은 스타일 3개는 의미가 없다).
        picked: List[str] = []
        seen_directions = set()
        for option in options:
            direction = option.get("direction")
            if direction in seen_directions:
                continue
            picked.append(option["id"])
            seen_directions.add(direction)
            if len(picked) >= 3:
                break
        if not picked:
            picked = [o["id"] for o in options[:3]]
        return {"selection": picked, "status": "selected"}
    return {"approval_status": "pending", "status": "awaiting_selection"}


def _convert_png(svg: Path, out: Path, width: int, max_height: int = 3000) -> None:
    """목표 폭·최대 높이 안에 들어가게 스케일을 정한다(공통 변환기 위임)."""
    profiles_base.convert_png(svg, out, width, max_height, run=_run)


def _to_jpg(png: Path, out: Path, quality: int = 90) -> None:
    """JPG는 알파를 흰 배경으로 합성한다(공통 변환기 위임)."""
    profiles_base.to_jpg(png, out, quality)


def pack_node(state: EntryState) -> Dict[str, Any]:
    """선택된 후보를 납품 팩으로 묶는다(profile에 위임)."""
    spec = state.get("spec") or {}
    selection = state.get("selection") or []
    by_id = {r["id"]: r for r in state.get("renders") or []}
    items = [by_id[variant_id] for variant_id in selection if variant_id in by_id]

    run_dir = OUT_ROOT / state.get("run_id", "entry")
    run_dir.mkdir(parents=True, exist_ok=True)
    profile = profiles_base.profile_for_spec(spec)
    ctx = _profile_context(run_dir)
    entry = profile.pack(items, spec, {**state, "profile_ctx": ctx})
    return {"entry": entry, "status": "packed"}


def _mockup_paths(mock: Dict[str, Any]) -> List[str]:
    """목업 항목에서 실제 이미지 경로만 꺼낸다(web의 card/poster, print의 flat/pouch)."""
    suffix = {".png", ".jpg", ".jpeg", ".svg"}
    return [
        value
        for value in mock.values()
        if isinstance(value, str) and Path(value).suffix.lower() in suffix
    ]


def mockup_node(state: EntryState) -> Dict[str, Any]:
    """선택된 엔트리 상위 N개로 평면 목업을 만든다(profile에 위임)."""
    entry = state.get("entry") or {}
    items = entry.get("items") or []
    if not items:
        return {"mockups": [], "status": "mockup_empty"}
    spec = state.get("spec") or {}
    limit = int(spec.get("mockups", 3))
    run_dir = Path(entry.get("dir", OUT_ROOT))
    mock_dir = run_dir / "mockups"
    mock_dir.mkdir(parents=True, exist_ok=True)

    profile = profiles_base.profile_for_spec(spec)
    mockups = profile.mockups(items[:limit], {**spec, "mockup_dir": str(mock_dir)})

    manifest_path = entry.get("manifest")
    if manifest_path and Path(manifest_path).is_file():
        manifest = core_artifacts.read_manifest(manifest_path)
        known = {row.get("path") for row in manifest.get("artifacts") or []}
        rows = list(manifest.get("artifacts") or [])
        for path in [p for mock in mockups for p in _mockup_paths(mock)]:
            row = _artifact_entry(path, Path(manifest_path).parent)
            if row is not None and row["path"] not in known:
                rows.append(row)
                known.add(row["path"])
        manifest["artifacts"] = rows
        manifest["status"] = "ok"
        core_artifacts.write_manifest(manifest_path, manifest)
        entry = {
            **entry,
            "manifest_digest": core_artifacts.manifest_digest(manifest),
        }

    return {"mockups": mockups, "entry": entry, "status": "mocked"}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def build_entry_pack_graph(checkpointer=None, interrupt_before=None):
    builder = StateGraph(EntryState)

    builder.add_node("spec", spec_node)
    builder.add_node("knowledge", knowledge_node)
    builder.add_node("direction", direction_node)
    builder.add_node("variants", variants_node)
    builder.add_node("render", render_node)
    builder.add_node("critic", critic_node)
    builder.add_node("gate", gate_node)
    builder.add_node("hitl", hitl_node)
    builder.add_node("pack", pack_node)
    builder.add_node("mockup", mockup_node)

    builder.add_edge(START, "spec")
    builder.add_edge("spec", "knowledge")
    builder.add_edge("knowledge", "direction")
    builder.add_edge("direction", "variants")
    builder.add_edge("variants", "render")
    builder.add_edge("render", "critic")
    builder.add_edge("critic", "gate")
    builder.add_edge("gate", "hitl")
    builder.add_edge("hitl", "pack")
    builder.add_edge("pack", "mockup")
    builder.add_edge("mockup", END)

    if interrupt_before is None:
        interrupt_before = ["hitl"]
    return builder.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)


graph = build_entry_pack_graph()
