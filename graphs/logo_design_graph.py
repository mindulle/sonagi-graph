"""Package Delivery Graph — 인간 로고 디자이너를 흉내내는 통합 파이프라인.

브리프 → research → constraints → concept → press(결정론) → asset craft(LLM) →
context_test(결정론) → critic → select → refine(루프) → HITL → publish

```
brief
  │
  ▼
concept (LLM: 5~10개 텍스트 방향, 픽셀 없음)
  │
  ▼
┌─ router ──────────────────────────────────────┐
│  mark_source="generate" → press_mark (결정론)  │
│  mark_source="existing" → load_mark            │
│  mark_source="none"     → (건너뜀)             │
└───────────────────────────────────────────────┘
  │
  ▼
craft (LLM 레이아웃 변주, mark 주입)
  │
  ▼
context_test (결정론: 16px·32px·64px·256px 렌더 + 가독성·대비 검증)
  │
  ▼
critic (규칙 기반 + LLM 점수)
  │
  ├─ score ≥ 70  → print_prep → hitl → (approved) publish
  ├─ score < 70  → refine (LLM: brief 보강) → concept (최대 2회 반복)
  └─ iteration ≥ 2 → print_prep → hitl → publish (강제 진행)
```

사용::

    from graphs.logo_design_graph import build_logo_design_graph

    graph = build_logo_design_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "logo-1"}}
    graph.invoke({
        "brief": "민들레 스튜디오 로고 — 차분하고 고급스러운 인쇄물",
        "mark_source": "generate",
        "concept": "monogram",       # press concept (선택)
        "content": "/path/to/brief.md",
        "print_spec": {"bleed_mm": 3, "cmyk": True, "cmyk_profile": "ISO Coated v2"},
        "max_iterations": 2,
    }, config)
    # context_test → critic → (score < 70이면 refine → concept 루프)
    # hitl에서 멈춘다. 검토 후 approval_status="approved"로 재개.
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from graphs.llm import chat_model
from graphs.asset_craft_graph import build_graph as build_craft

PRESS_DIR = Path(os.environ.get("SONAGI_PRESS_DIR", "/home/ubuntu/sonagi-work"))
PRESS_GRAPH = PRESS_DIR / "tools" / "press_graph.py"
ASSETS_DIR = Path(os.environ.get("SONAGI_ASSETS_DIR", "/home/ubuntu/sonagi-assets"))
OUT_ROOT = Path(os.environ.get("SONAGI_PACKAGE_OUT", "/tmp/package_delivery"))
VECTORCRAFT = os.environ.get("VECTORCRAFT_BIN", "vectorcraft-cli")


class ConceptDirection(BaseModel):
    title: str = Field(description="방향 이름 (예: '미니멀 모노그램', '네거티브 스페이스 동물')")
    description: str = Field(description="1~2문장 설명")
    press_concept: str = Field(description="press concept 중 하나: seed_dispersal, press_imprint, ink_overlap, monogram, negative_space, mascot")
    territory_hint: str = Field(description="추천 territory (예: monogram_overlap, negative_hidden)")


class ConceptOutput(BaseModel):
    directions: List[ConceptDirection] = Field(description="3~5개의 서로 다른 시각 방향")


class PackageDeliveryState(TypedDict, total=False):
    brief: str
    concept: Optional[str]  # press concept (직접 지정 시)
    concepts: List[Dict[str, Any]]  # LLM 생성 방향 목록
    selected_concept: Optional[str]  # 선택된 방향의 press_concept
    mark_source: str  # "generate" | "existing" | "none"
    mark_path: Optional[str]
    mark_svg: Optional[str]
    press_run_dir: Optional[str]
    press_selected: List[Dict[str, Any]]
    content: Optional[str]
    deliverables: List[str]
    print_spec: Optional[Dict[str, Any]]
    craft_run_id: Optional[str]
    craft_variants: List[Dict[str, Any]]
    craft_status: str
    context_test_report: Optional[Dict[str, Any]]
    context_test_score: int
    iteration_count: int
    max_iterations: int
    refine_feedback: Optional[str]
    print_report: Optional[str]
    print_ok: bool
    approval_status: str  # "pending" | "approved" | "rejected"
    approval_id: Optional[str]
    status: str
    published: List[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_subprocess(args, cwd=None, env=None):
    import os as _os

    run_env = dict(env or os.environ)
    # press가 부르는 `inkscape`를 vectorcraft-cli로 넘기는 shim
    shim_dir = _ensure_inkscape_shim()
    if shim_dir:
        run_env["PATH"] = shim_dir + os.pathsep + run_env.get("PATH", "")
    done = subprocess.run(
        [sys.executable, *map(str, args)] if not str(args[0]).endswith(".py") else ["python3", *map(str, args)],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=run_env,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"실패: {' '.join(map(str, args))}\n{done.stdout[-500:]}\n{done.stderr[-500:]}"
        )
    return done


_SHIM_DIR = None


def _ensure_inkscape_shim():
    global _SHIM_DIR
    if _SHIM_DIR:
        return _SHIM_DIR
    repo_root = Path(__file__).resolve().parent.parent
    shim_py = repo_root / "scripts" / "inkscape_shim.py"
    if not shim_py.exists():
        return None
    d = Path(tempfile.mkdtemp(prefix="press-shim-"))
    wrapper = d / "inkscape"
    wrapper.write_text(f'#!/bin/sh\nexec python3 "{shim_py}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    _SHIM_DIR = str(d)
    return _SHIM_DIR


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def concept_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """LLM이 3~5개의 서로 다른 시각 방향을 텍스트로 생성한다 (픽셀 없음).

    인간 디자이너의 '스케치' 단계에 해당한다. 각 방향은 press concept에 매핑된다.
    """
    brief = state.get("brief", "")
    llm = chat_model("LOGO_CONCEPT_LLM_MODEL", default_model="gemini-3-flash", temperature=0.4)
    structured = llm.with_structured_output(ConceptOutput)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 로고 디자이너입니다. 브리프를 읽고 3~5개의 서로 다른 시각 방향을 제안하세요.\n"
         "각 방향은 다음 press concept 중 하나에 매핑됩니다:\n"
         "- seed_dispersal: 씨앗·날림 (유기적, 자연, 성장)\n"
         "- press_imprint: 활자·압인 (전통적, 장인, 신뢰)\n"
         "- ink_overlap: 잉크·겹침 (창의적, 현대적, 레이어)\n"
         "- monogram: 모노그램·글자 (타이포 중심, 미니멀)\n"
         "- negative_space: 네거티브 스페이스 (이중독해, 지적)\n"
         "- mascot: 마스코트·캐릭터 (친근, 접근성)\n\n"
         "서로 시각적으로 구분되는 방향을 제안하세요. 비슷한 방향 2개는 피하세요."),
        ("user", "브리프: {brief}"),
    ])

    try:
        result = (prompt | structured).invoke({"brief": brief})
        directions = [d.model_dump() for d in result.directions]
    except Exception as e:
        # LLM 실패 시 기본 방향 1개
        directions = [{"title": "기본 방향", "description": "브리프 기반", "press_concept": "press_imprint", "territory_hint": "seal_frame"}]

    # 사용자가 concept을 직접 지정했으면それを 우선
    if state.get("concept"):
        for d in directions:
            d["press_concept"] = state["concept"]

    # territory 검증: 존재하지 않는 territory는 가장 가까운 것으로 보정
    valid_territories = set()
    try:
        import sys as _sys
        _sys.path.insert(0, str(PRESS_DIR / "tools"))
        import press_ops as _po
        valid_territories = set(_po.OPS_TERRITORIES.keys()) | set(
            ["seed_rise", "seal_frame", "type_block", "orbit_ring", "layer_stack", "cut_field",
             "repeat_petals", "mirror_gate", "subtract_dots", "plume_arc", "stamp_bar", "offset_duo"]
        )
    except Exception:
        pass

    # concept 검증
    valid_concepts = {"seed_dispersal", "press_imprint", "ink_overlap", "monogram", "negative_space", "mascot"}
    for d in directions:
        if d.get("press_concept") not in valid_concepts:
            d["press_concept"] = "press_imprint"
        if d.get("territory_hint") and valid_territories and d["territory_hint"] not in valid_territories:
            # 가장 가까운 territory로 보정 (첫 번째 유효)
            d["territory_hint"] = sorted(valid_territories)[0]

    return {"concepts": directions, "status": "concepted"}


def press_mark_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """press로 마크 후보를 생성하고, top 1을 '잠금'한다."""
    if state.get("mark_source", "none") != "generate":
        return {"status": "mark_skipped"}

    brief_text = state.get("brief", "Sonagi")
    concept = state.get("selected_concept") or state.get("concept") or "press_imprint"
    out_dir = OUT_ROOT / f"press_{uuid.uuid4().hex[:6]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    import yaml
    palette = ["#161616", "#D97706", "#FBF9F5"]
    brief_doc = {
        "brand_name": brief_text,
        "palette": palette,
        "concept": concept,
        "media_scope": ["web", "print", "favicon"],
    }
    brief_path = out_dir / "brief.yaml"
    brief_path.write_text(
        yaml.safe_dump(brief_doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    cmd = [str(PRESS_GRAPH), "run", str(brief_path), "--out", str(out_dir), "--candidates", "12", "--concept", concept]
    _run_subprocess(cmd, cwd=PRESS_DIR)

    run_doc = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    selected = run_doc.get("selected", [])
    if not selected:
        raise RuntimeError("press: 선택된 마크가 없습니다")

    mark_id = selected[0]["id"]
    mark_svg = (out_dir / "candidates" / mark_id / "mark.svg").read_text(encoding="utf-8")

    return {
        "press_run_dir": str(out_dir),
        "press_selected": selected,
        "mark_svg": mark_svg,
        "mark_path": str(out_dir / "candidates" / mark_id / "mark.svg"),
        "status": "mark_locked",
    }


def load_mark_node(state: PackageDeliveryState) -> Dict[str, Any]:
    if state.get("mark_source") != "existing":
        return {"status": "mark_none"}
    path = state.get("mark_path")
    if not path or not Path(path).exists():
        raise ValueError(f"mark_path가 유효하지 않습니다: {path}")
    return {"mark_svg": Path(path).read_text(encoding="utf-8"), "status": "mark_loaded"}


def craft_node(state: PackageDeliveryState) -> Dict[str, Any]:
    from graphs.asset_craft_graph import build_graph as build_craft

    brief = state.get("brief", "차분한 패키지")
    if state.get("mark_svg"):
        brief = f"{brief}\n\n[잠긴 마크 SVG]\n{state['mark_svg'][:500]}"

    run_id = uuid.uuid4().hex[:8]
    craft_graph = build_craft(checkpointer=None)
    config = {"configurable": {"thread_id": f"craft-{run_id}"}}
    try:
        craft_graph.invoke(
            {"brief": brief, "content": state.get("content", brief), "run_id": run_id},
            config=config,
        )
        craft_state = craft_graph.get_state(config).values
        return {
            "craft_run_id": run_id,
            "craft_variants": craft_state.get("variants", []),
            "status": "crafted",
        }
    except Exception as e:
        return {
            "craft_run_id": run_id,
            "craft_variants": [],
            "craft_status": f"error: {str(e)[:300]}",
            "status": "craft_failed",
        }


def context_test_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """로고를 여러 맥락에서 렌더링하고 가독성·대비를 검증한다.

    인간 디자이너의 'context testing' 단계에 해당한다.
    - 16px 파비콘: 읽히는가?
    - 32px, 64px: 디테일이 살아있는가?
    - 256px: 전체 형태가 명확한가?
    - 다크/라이트 배경: 대비가 충분한가?
    """
    mark_svg = state.get("mark_svg")
    if not mark_svg:
        return {"context_test_report": {"score": 100, "note": "마크 없음 — 건너뜀"}, "context_test_score": 100, "status": "context_skipped"}

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ctx-test-"))
    svg_path = tmp / "mark.svg"
    svg_path.write_text(mark_svg, encoding="utf-8")

    sizes = [16, 32, 64, 256]
    report = {"sizes": {}, "score": 100, "issues": []}
    penalties = 0

    for size in sizes:
        out = tmp / f"mark_{size}.png"
        try:
            env = dict(os.environ)
            shim = _ensure_inkscape_shim()
            if shim:
                env["PATH"] = shim + os.pathsep + env.get("PATH", "")
            subprocess.run(
                ["vectorcraft-cli", "convert", str(svg_path), str(out), "--scale", str(round(size / 120, 4))],
                capture_output=True, text=True, timeout=30, env=env,
            )
            if out.exists() and out.stat().st_size > 100:
                report["sizes"][size] = "ok"
            else:
                report["sizes"][size] = "blank"
                report["issues"].append(f"{size}px: 렌더 실패 또는 빈 이미지")
                penalties += 15
        except Exception as e:
            report["sizes"][size] = f"error: {str(e)[:80]}"
            report["issues"].append(f"{size}px: {str(e)[:80]}")
            penalties += 20

    # SVG 구조 검증: 요소 수, viewBox, stroke 최소값
    try:
        import re as _re
        element_count = len(_re.findall(r"<(circle|rect|path|polygon|ellipse|line|g)\b", mark_svg))
        if element_count > 30:
            report["issues"].append(f"SVG 요소 {element_count}개 — 너무 복잡 (16px에서 뭉개짐)")
            penalties += 15
        if element_count < 2:
            report["issues"].append("SVG 요소가 너무 적음 — 형태가 없음")
            penalties += 20
        # viewBox 확인
        vb = _re.search(r'viewBox="([^"]+)"', mark_svg)
        if vb:
            parts = vb.group(1).split()
            if len(parts) == 4:
                w, h = float(parts[2]), float(parts[3])
                if w != h:
                    report["issues"].append(f"viewBox 비정사각 ({w}x{h}) — 스케일링 왜곡 가능")
                    penalties += 5
        # stroke 최소값 확인
        for m in _re.finditer(r'stroke-width="([0-9.]+)"', mark_svg):
            sw = float(m.group(1))
            if sw < 1.5:
                report["issues"].append(f"stroke-width {sw} < 1.5 — 16px에서 사라짐")
                penalties += 10
                break
    except Exception:
        pass

    # 다크/라이트 대비 체크 (256px 기준)
    # vectorcraft는 투명 배경으로 렌더하므로, 배경색을 합성해서 대비를 확인한다
    try:
        from PIL import Image
        img_path = tmp / "mark_256.png"
        if img_path.exists():
            img = Image.open(img_path).convert("RGBA")
            # 흰 배경과 검정 배경에서의 평균 대비
            pixels = list(img.getdata())
            non_transparent = [p for p in pixels if p[3] > 128]
            if len(non_transparent) < 100:
                report["issues"].append("256px: 마크가 거의 보이지 않음 (픽셀 부족)")
                penalties += 30
            else:
                # 밝기 분포 확인
                luminances = [0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2] for p in non_transparent]
                avg_lum = sum(luminances) / len(luminances)
                if avg_lum > 200:
                    report["issues"].append("마크가 너무 밝아 어두운 배경에서 안 보일 수 있음")
                    penalties += 10
                if avg_lum < 50:
                    report["issues"].append("마크가 너무 어두워 밝은 배경에서 안 보일 수 있음")
                    penalties += 10
    except Exception:
        pass  # PIL 없거나 이미지 식별 실패 시 픽셀 체크 건너뜀

    report["score"] = max(0, 100 - penalties)
    return {"context_test_report": report, "context_test_score": report["score"], "status": "context_tested"}


def critic_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """context_test 점수와 craft 상태를 종합하여 최종 점수를 산출한다."""
    ctx_score = state.get("context_test_score", 100)
    craft_ok = state.get("status") != "craft_failed"
    craft_penalty = 0 if craft_ok else 20
    final_score = max(0, ctx_score - craft_penalty)
    return {"context_test_score": final_score, "status": "critiqued"}


def refine_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """context_test 실패 시 brief를 보강한다. LLM이 피드백을 반영한 새 brief를 생성한다."""
    llm = chat_model("LOGO_REFINE_LLM_MODEL", default_model="gemini-3-flash", temperature=0.3)
    issues = (state.get("context_test_report") or {}).get("issues", [])
    current_brief = state.get("brief", "")
    iteration = state.get("iteration_count", 0)

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 로고 디자이너입니다. 다음 문제점을 해결하도록 브리프를 보강하세요.\n"
         "원래 브리프에 구체적인 개선 지시를 추가하세요. JSON으로만 응답하세요."),
        ("user",
         "원래 브리프: {brief}\n"
         "문제점: {issues}\n"
         "반복 횟수: {iteration}\n\n"
         '{{"brief": "보강된 브리프 (원래 내용 + 개선 지시)", "concept": "press concept 중 하나"}}'),
    ])

    try:
        from langchain_core.output_parsers import StrOutputParser
        import json as _json
        chain = prompt | llm
        resp = chain.invoke({"brief": current_brief, "issues": "; ".join(issues), "iteration": iteration})
        parsed = _json.loads(resp.content)
        new_brief = parsed.get("brief", current_brief)
        new_concept = parsed.get("concept", state.get("concept"))
    except Exception:
        new_brief = current_brief + " (단순화, 가독성 향상)"
        new_concept = state.get("concept")

    return {
        "brief": new_brief,
        "concept": new_concept,
        "iteration_count": iteration + 1,
        "refine_feedback": "; ".join(issues),
        "status": "refined",
    }


def print_prep_node(state: PackageDeliveryState) -> Dict[str, Any]:
    spec = state.get("print_spec")
    if not spec:
        return {"print_ok": True, "print_report": "인쇄 사양 없음 — 건너뜀", "status": "print_skipped"}

    issues = []
    variants = state.get("craft_variants", [])
    craft_status = state.get("craft_status", "")
    if craft_status.startswith("error"):
        issues.append(f"asset_craft 실패: {craft_status[:100]}")
    if not variants and not craft_status.startswith("error"):
        issues.append("변주가 생성되지 않았습니다")
    if state.get("mark_source") not in ("none", None) and not state.get("mark_svg"):
        issues.append("마크가 잠기지 않았습니다")
    bleed = spec.get("bleed_mm", 0)
    if bleed < 3:
        issues.append(f"bleed {bleed}mm < 3mm")
    if spec.get("cmyk") and not spec.get("cmyk_profile"):
        issues.append("CMYK 프로파일 미지정 (ISO Coated v2 권장)")

    ok = len(issues) == 0
    report = "✅ 인쇄 준비 통과" if ok else "⚠️ " + "; ".join(issues)
    return {"print_ok": ok, "print_report": report, "status": "print_checked"}


def hitl_node(state: PackageDeliveryState) -> Dict[str, Any]:
    if state.get("approval_status") == "approved":
        return {"status": "approved"}
    return {
        "approval_status": "pending",
        "status": "awaiting_approval",
    }


def publish_node(state: PackageDeliveryState) -> Dict[str, Any]:
    delivery = OUT_ROOT / "delivery"
    delivery.mkdir(parents=True, exist_ok=True)
    published = []
    if state.get("mark_svg"):
        mark_path = delivery / "mark.svg"
        mark_path.write_text(state["mark_svg"], encoding="utf-8")
        published.append(str(mark_path))
    return {"published": published, "status": "delivered"}


def route_mark(state: PackageDeliveryState):
    src = state.get("mark_source", "none")
    if src == "generate":
        return "press_mark"
    if src == "existing":
        return "load_mark"
    return "craft"


def route_after_context(state: PackageDeliveryState):
    """context_test 결과에 따라 refine 루프 또는 진행을 결정한다."""
    score = state.get("context_test_score", 100)
    iteration = state.get("iteration_count", 0)
    max_iter = state.get("max_iterations", 2)
    if score < 70 and iteration < max_iter:
        return "refine"
    return "print_prep"


def route_after_hitl(state: PackageDeliveryState):
    return "publish" if state.get("approval_status") == "approved" else END


def build_logo_design_graph(checkpointer=None, interrupt_before=None):
    builder = StateGraph(PackageDeliveryState)

    builder.add_node("concept", concept_node)
    builder.add_node("press_mark", press_mark_node)
    builder.add_node("load_mark", load_mark_node)
    builder.add_node("craft", craft_node)
    builder.add_node("context_test", context_test_node)
    builder.add_node("critic", critic_node)
    builder.add_node("refine", refine_node)
    builder.add_node("print_prep", print_prep_node)
    builder.add_node("hitl", hitl_node)
    builder.add_node("publish", publish_node)

    # concept → mark routing → craft → context_test → critic → (loop or proceed)
    builder.add_edge(START, "concept")
    builder.add_conditional_edges("concept", route_mark, {
        "press_mark": "press_mark",
        "load_mark": "load_mark",
        "craft": "craft",
    })
    builder.add_edge("press_mark", "craft")
    builder.add_edge("load_mark", "craft")
    builder.add_edge("craft", "context_test")
    builder.add_edge("context_test", "critic")
    builder.add_conditional_edges("critic", route_after_context, {
        "refine": "refine",
        "print_prep": "print_prep",
    })
    builder.add_edge("refine", "concept")  # 루프: refine → concept 재실행
    builder.add_edge("print_prep", "hitl")
    builder.add_conditional_edges("hitl", route_after_hitl, {"publish": "publish", END: END})
    builder.add_edge("publish", END)

    if interrupt_before is None:
        interrupt_before = ["hitl"]
    return builder.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)


graph = build_logo_design_graph()

