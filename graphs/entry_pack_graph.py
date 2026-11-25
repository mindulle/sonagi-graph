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
import re
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

DEFAULT_SPEC: Dict[str, Any] = {
    "platform": "generic",
    "styles_per_direction": 2,
    "variant_cap": 24,
    "top_n": 9,
    "mockups": 3,
    "image_width": 2000,
    "max_height": 3000,
    "jpg_quality": 90,
}


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
    """manifest 항목 하나. 실행 루트 밖 파일은 절대 경로로 남긴다."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        return core_artifacts.artifact_entry(target, root=root)
    except ValueError:  # root 밖(예: 외부 브리프 파일)
        return core_artifacts.artifact_entry(target)


def _collect_artifacts(paths: List[Any], root: Path) -> List[Dict[str, Any]]:
    """경로 목록 → manifest 산출물 항목(중복 제거, 순서 유지)."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    for path in paths:
        entry = _artifact_entry(path, root)
        if entry is None or entry["path"] in seen:
            continue
        seen.add(entry["path"])
        rows.append(entry)
    return rows


def _build_manifest(
    state: EntryState,
    run_dir: Path,
    artifacts: List[Dict[str, Any]],
    *,
    status: str = "ok",
) -> Dict[str, Any]:
    """입력 지문(브리프 해시·시드·spec 해시)과 산출물 해시를 담은 run manifest."""
    spec = state.get("spec") or {}
    brief = str(state.get("content") or state.get("brief") or "")
    return core_artifacts.build_manifest(
        run_id=run_dir.name,
        kind="entry_pack",
        status=status,
        inputs={
            "brief_sha256": core_artifacts.sha256_bytes(brief.encode("utf-8")),
            "seed": str(spec.get("seed") or ""),
            "spec_sha256": core_artifacts.sha256_json(spec),
        },
        artifacts=artifacts,
        meta={
            "platform": spec.get("platform"),
            "media": spec.get("media"),
            "variants": len(state.get("variants") or []),
            "selection": list(state.get("selection") or []),
        },
    )


def _svg_size(path: Path) -> tuple[float, float]:
    head = path.read_text(encoding="utf-8")[:400]
    width = re.search(r'<svg[^>]*\bwidth="([0-9.]+)', head)
    height = re.search(r'<svg[^>]*\bheight="([0-9.]+)', head)
    return (
        float(width.group(1)) if width else 1200.0,
        float(height.group(1)) if height else 900.0,
    )


def _svg_width(path: Path) -> float:
    return _svg_size(path)[0]


def _direction_text(direction: Dict[str, Any]) -> str:
    parts = [direction.get("title", ""), direction.get("description", "")]
    parts += [str(m) for m in direction.get("motifs") or []]
    parts.append(direction.get("visual_language", ""))
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def spec_node(state: EntryState) -> Dict[str, Any]:
    """브리프를 IR로 바꾸고 플랫폼·규격·시드 기본값을 확정한다."""
    raw = state.get("brief") or state.get("content") or ""
    ir = craft.parse_brief(raw)
    spec = dict(DEFAULT_SPEC)
    spec.update(state.get("spec") or {})
    spec["tones"] = list(spec.get("tones") or ir.get("tones") or ["편집"])
    spec["media"] = spec.get("media") or ir.get("media", "web")
    if "themes" not in spec:
        spec["themes"] = ["light", "dark"] if spec["media"] == "web" else ["light"]
    if "breakpoints" not in spec:
        spec["breakpoints"] = ["desktop"] if spec["media"] == "web" else ["mobile"]
    if not spec.get("seed"):
        spec["seed"] = hashlib.sha1(str(raw).encode("utf-8")).hexdigest()[:8]
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


def _fallback_direction(ir: Dict[str, Any]) -> Dict[str, Any]:
    tones = " · ".join(ir.get("tones") or ["편집"])
    return {
        "title": "기본 방향",
        "description": f"{tones} 톤의 기본 구성",
        "motifs": [],
        "visual_language": "",
    }


def direction_node(state: EntryState) -> Dict[str, Any]:
    """LLM으로 서로 다른 시각 방향 3~5개를 만든다."""
    from graphs.logo_design_graph import ConceptDirection, ConceptOutput

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
    """방향 × 스타일 × 브레이크포인트 × 테마로 후보를 만든다(시드 고정)."""
    spec = state.get("spec") or {}
    ir = state.get("ir") or craft.parse_brief(state.get("brief", ""))
    directions = state.get("directions") or [_fallback_direction(ir)]
    styles = craft.load_registry(ASSETS_DIR)

    per_direction: List[Dict[str, Any]] = []
    remaining = list(styles)
    for index, direction in enumerate(directions):
        brief = dict(ir)
        brief["purpose"] = _direction_text(direction)
        brief["tones"] = list(ir.get("tones") or ["편집"])
        # 이미 쓴 스타일은 다음 방향에서 빼서 방향마다 다른 조합이 나오게 한다.
        pool = remaining or styles
        plan = craft.plan_variants(
            brief,
            pool,
            limit=int(spec.get("styles_per_direction", 2)),
            breakpoints=tuple(spec.get("breakpoints") or ("desktop",)),
            themes=tuple(spec.get("themes") or ("light",)),
            seed=f"{spec.get('seed')}-{index}",
        )
        used_names = {v["style"] for v in plan}
        used_ids = {v["style_id"] for v in plan}
        remaining = [
            s
            for s in remaining
            if s.get("name") not in used_names and s.get("id") not in used_ids
        ]
        for variant in plan:
            variant["direction"] = direction.get("title", f"방향 {index + 1}")
            variant["direction_index"] = index
            variant["motifs"] = list(direction.get("motifs") or [])
            per_direction.append(variant)

    seen = set()
    variants = []
    for variant in per_direction:
        key = (variant["style"], variant["breakpoint"], variant["theme"])
        if key in seen:
            continue
        seen.add(key)
        variants.append(variant)
        if len(variants) >= int(spec.get("variant_cap", 24)):
            break
    return {"variants": variants, "status": "variants"}


def render_node(state: EntryState) -> Dict[str, Any]:
    """후보마다 SVG를 렌더한다(render_recipe, 결정론)."""
    spec = state.get("spec") or {}
    variants = state.get("variants") or []
    if not variants:
        return {"renders": [], "status": "render_empty"}

    run_id = state.get("run_id", "entry")
    run_dir = OUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    content = state.get("content") or state.get("brief") or ""
    content_path = Path(str(content))
    if not content_path.is_file():
        content_path = run_dir / "brief.md"
        content_path.write_text(str(content), encoding="utf-8")

    recipe = run_dir / "recipe.json"
    _run(
        [
            sys.executable,
            ASSETS_DIR / "scripts/import_content.py",
            content_path,
            "--catalog",
            CATALOG,
            "--out",
            recipe,
        ]
    )

    renders = []
    for variant in variants:
        out = run_dir / variant["id"]
        cmd = [
            sys.executable,
            ASSETS_DIR / "scripts/render_recipe.py",
            "--recipe",
            recipe,
            "--style",
            ASSETS_DIR / "styles" / f"{variant['style']}.json",
            "--catalog",
            CATALOG,
            "--tokens",
            TOKENS,
            "--out",
            out,
            "--breakpoint",
            variant["breakpoint"],
        ]
        if variant.get("theme") == "dark":
            cmd.append("--dark")
        _run(cmd)
        svg = next(out.glob("*.svg"), None)
        manifest = next(out.glob("*.manifest.json"), None)
        if not svg:
            continue
        renders.append(
            {
                **variant,
                "svg": str(svg),
                "manifest": str(manifest) if manifest else None,
            }
        )

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
        "recipe": str(recipe),
        "renders": renders,
        "manifest": str(manifest_path),
        "manifest_digest": core_artifacts.manifest_digest(manifest),
        "status": "rendered",
    }


def critic_node(state: EntryState) -> Dict[str, Any]:
    """린트로 기계 판정한다. FAIL은 선택지에서 빠진다."""
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
    """목표 폭·최대 높이 안에 들어가게 스케일을 정한다(긴 페이지도 업로드 규격 유지)."""
    native_w, native_h = _svg_size(svg)
    native_w = native_w or 1200.0
    native_h = native_h or 900.0
    scale = min(float(width) / native_w, float(max_height) / native_h)
    scale = max(0.1, min(8.0, scale))
    _run([VECTORCRAFT, "convert", svg, out, "--scale", round(scale, 4)], timeout=120)


def _to_jpg(png: Path, out: Path, quality: int = 90) -> None:
    """JPG는 알파를 흰 배경으로 합성한다(투명 영역이 검게 나오지 않게)."""
    from PIL import Image

    with Image.open(png) as image:
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            base = Image.new("RGB", rgba.size, (255, 255, 255))
            base.paste(rgba, mask=rgba.split()[-1])
            rgb = base
        else:
            rgb = image.convert("RGB")
        rgb.save(out, "JPEG", quality=quality, optimize=True)


def _describe(state: EntryState, option: Dict[str, Any]) -> str:
    spec = state.get("spec") or {}
    direction = next(
        (
            d
            for d in state.get("directions") or []
            if d.get("title") == option.get("direction")
        ),
        {},
    )
    if spec.get("skip_llm"):
        motifs = ", ".join(direction.get("motifs") or []) or "형태 중심"
        return (
            f"{option.get('direction') or '기본 방향'} — "
            f"{direction.get('description') or '브리프 기반 구성'}. "
            f"모티프: {motifs}. 스타일 {option.get('style')} ({option.get('theme')})."
        )
    try:
        llm = chat_model(
            "ENTRY_COPY_LLM_MODEL", default_model="gemini-3-flash", temperature=0.6
        )
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "콘테스트 제출용 설명을 2~3문장으로 쓰세요. 미사여구 없이 무엇을 왜 그렇게 했는지.",
                ),
                (
                    "user",
                    "브리프: {brief}\n방향: {direction}\n모티프: {motifs}\n스타일: {style} ({theme})",
                ),
            ]
        )
        resp = (prompt | llm).invoke(
            {
                "brief": state.get("brief", ""),
                "direction": option.get("direction", ""),
                "motifs": ", ".join(direction.get("motifs") or []),
                "style": option.get("style", ""),
                "theme": option.get("theme", ""),
            }
        )
        text = str(getattr(resp, "content", resp)).strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    return f"{option.get('direction') or '기본 방향'} — 스타일 {option.get('style')} ({option.get('theme')})."


def pack_node(state: EntryState) -> Dict[str, Any]:
    """선택된 후보를 제출 규격(PNG·JPG)으로 내보내고 설명·메타를 묶는다."""
    spec = state.get("spec") or {}
    selection = state.get("selection") or []
    by_id = {r["id"]: r for r in state.get("renders") or []}
    options = {o["id"]: o for o in state.get("options") or []}
    run_dir = OUT_ROOT / state.get("run_id", "entry")
    pack_dir = run_dir / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)

    width = int(spec.get("image_width", 2000))
    max_height = int(spec.get("max_height", 3000))
    quality = int(spec.get("jpg_quality", 90))
    items = []
    for variant_id in selection:
        render = by_id.get(variant_id)
        if not render:
            continue
        svg = Path(render["svg"])
        png = pack_dir / f"{variant_id}.png"
        jpg = pack_dir / f"{variant_id}.jpg"
        _convert_png(svg, png, width, max_height)
        _to_jpg(png, jpg, quality)
        option = options.get(variant_id) or render
        items.append(
            {
                "id": variant_id,
                "direction": option.get("direction"),
                "style": render.get("style"),
                "theme": render.get("theme"),
                "svg": str(svg),
                "png": str(png),
                "jpg": str(jpg),
                "png_size": png.stat().st_size,
                "jpg_size": jpg.stat().st_size,
                "sha256": core_artifacts.sha256_file(png),
                "description": _describe(state, option),
            }
        )

    meta = {
        "platform": spec.get("platform"),
        "brief": state.get("brief", ""),
        "seed": spec.get("seed"),
        "image_width": width,
        "variants_total": len(state.get("variants") or []),
        "rendered_total": len(state.get("renders") or []),
        "selected_total": len(items),
        "items": items,
    }
    (pack_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [f"# 엔트리 팩 — {spec.get('platform')}", ""]
    for item in items:
        lines += [
            f"## {item['id']}",
            f"- 방향: {item['direction']}",
            f"- 스타일: {item['style']} ({item['theme']})",
            f"- PNG: `{Path(item['png']).name}` ({item['png_size']:,}B)",
            f"- JPG: `{Path(item['jpg']).name}` ({item['jpg_size']:,}B)",
            "",
            item["description"],
            "",
        ]
    (pack_dir / "entry.md").write_text("\n".join(lines), encoding="utf-8")

    artifact_paths: List[Any] = []
    for item in items:
        artifact_paths += [item["svg"], item["png"], item["jpg"]]
    artifact_paths += [pack_dir / "meta.json", pack_dir / "entry.md"]
    manifest = _build_manifest(
        state, run_dir, _collect_artifacts(artifact_paths, run_dir)
    )
    manifest_path = core_artifacts.write_manifest(run_dir, manifest)

    return {
        "entry": {
            "dir": str(pack_dir),
            "items": items,
            "meta": str(pack_dir / "meta.json"),
            "manifest": str(manifest_path),
            "manifest_digest": core_artifacts.manifest_digest(manifest),
        },
        "status": "packed",
    }


def _card_mockup(page: Path, out: Path, label: str) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    canvas_size = (1400, 900)
    canvas = Image.new("RGB", canvas_size, (239, 237, 232))
    draw = ImageDraw.Draw(canvas)
    card = (150, 110, 1250, 750)
    shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (card[0] + 6, card[1] + 18, card[2] + 6, card[3] + 18),
        radius=18,
        fill=(0, 0, 0, 60),
    )
    canvas.paste(
        Image.alpha_composite(
            canvas.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(14))
        ).convert("RGB"),
        (0, 0),
    )
    draw.rounded_rectangle(
        card, radius=14, fill=(255, 255, 255), outline=(220, 216, 208), width=2
    )

    with Image.open(page) as image:
        inner = (card[0] + 28, card[1] + 28, card[2] - 28, card[3] - 28)
        fitted = image.convert("RGB")
        ratio = min(inner[2] - inner[0], inner[3] - inner[1]) / max(
            fitted.width, fitted.height
        )
        size = (max(1, int(fitted.width * ratio)), max(1, int(fitted.height * ratio)))
        fitted = fitted.resize(size, Image.LANCZOS)
        x = inner[0] + ((inner[2] - inner[0]) - size[0]) // 2
        y = inner[1] + ((inner[3] - inner[1]) - size[1]) // 2
        canvas.paste(fitted, (x, y))

    if label:
        draw.text((150, 800), label[:80], fill=(60, 58, 54))
    canvas.save(out, "PNG", optimize=True)


def _poster_mockup(page: Path, out: Path) -> None:
    from PIL import Image, ImageFilter

    canvas = Image.new("RGB", (1400, 900), (214, 209, 200))
    frame = (300, 70, 1100, 830)
    with Image.open(page) as image:
        fitted = image.convert("RGB")
        ratio = min(frame[2] - frame[0], frame[3] - frame[1]) / max(
            fitted.width, fitted.height
        )
        size = (max(1, int(fitted.width * ratio)), max(1, int(fitted.height * ratio)))
        fitted = fitted.resize(size, Image.LANCZOS)
        x = frame[0] + ((frame[2] - frame[0]) - size[0]) // 2
        y = frame[1] + ((frame[3] - frame[1]) - size[1]) // 2
        canvas.paste(fitted, (x, y))
    canvas.save(out, "PNG", optimize=True)


def mockup_node(state: EntryState) -> Dict[str, Any]:
    """선택된 엔트리 상위 N개로 평면 목업을 만든다(Pillow, 결정론)."""
    entry = state.get("entry") or {}
    items = entry.get("items") or []
    if not items:
        return {"mockups": [], "status": "mockup_empty"}
    spec = state.get("spec") or {}
    limit = int(spec.get("mockups", 3))
    run_dir = Path(entry.get("dir", OUT_ROOT))
    mock_dir = run_dir / "mockups"
    mock_dir.mkdir(parents=True, exist_ok=True)

    mockups = []
    for item in items[:limit]:
        card = mock_dir / f"{item['id']}-card.png"
        _card_mockup(Path(item["png"]), card, item.get("direction") or item["id"])
        poster = mock_dir / f"{item['id']}-poster.png"
        _poster_mockup(Path(item["png"]), poster)
        mockups.append(
            {
                "id": item["id"],
                "card": str(card),
                "poster": str(poster),
                "card_sha256": core_artifacts.sha256_file(card),
            }
        )

    manifest_path = entry.get("manifest")
    if manifest_path and Path(manifest_path).is_file():
        manifest = core_artifacts.read_manifest(manifest_path)
        known = {row.get("path") for row in manifest.get("artifacts") or []}
        rows = list(manifest.get("artifacts") or [])
        for path in [p for m in mockups for p in (m["card"], m["poster"])]:
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
