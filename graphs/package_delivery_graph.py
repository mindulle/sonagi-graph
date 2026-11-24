"""Package Delivery Graph — press 마크 + asset craft 레이아웃 + 인쇄 준비를 한 파이프라인으로.

브리프 하나로 마크 생성(결정론) → 패키지 변주(LLM) → 인쇄 준비(결정론) → HITL을
한 번에 돌린다. `mark_source`로 press를 건너뛸 수 있고, `print_spec`이 없으면
인쇄 준비도 건너뛴다.

사용::

    from graphs.package_delivery_graph import build_graph

    graph = build_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "pkg-1"}}
    graph.invoke({
        "brief": "민들레 스튜디오 패키지 세트",
        "mark_source": "generate",   # "generate" | "existing" | "none"
        "content": "/path/to/brief.md",
        "print_spec": {"bleed_mm": 3, "cmyk": True},
    }, config)
    # hitl 노드에서 멈춘다. 검토 후 approval_status="approved"로 재개.
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

from langgraph.graph import END, START, StateGraph

PRESS_DIR = Path(os.environ.get("SONAGI_PRESS_DIR", "/home/ubuntu/sonagi-work"))
PRESS_GRAPH = PRESS_DIR / "tools" / "press_graph.py"
ASSETS_DIR = Path(os.environ.get("SONAGI_ASSETS_DIR", "/home/ubuntu/sonagi-assets"))
OUT_ROOT = Path(os.environ.get("SONAGI_PACKAGE_OUT", "/tmp/package_delivery"))


class PackageDeliveryState(TypedDict, total=False):
    brief: str
    concept: Optional[
        str
    ]  # press concept: seed_dispersal, press_imprint, monogram, negative_space, mascot
    mark_source: str  # "generate" | "existing" | "none"
    mark_path: Optional[str]  # 기존 마크 경로 (mark_source="existing")
    mark_svg: Optional[str]  # 잠긴 마크 SVG
    press_run_dir: Optional[str]
    press_selected: List[Dict[str, Any]]
    content: Optional[str]
    deliverables: List[str]
    print_spec: Optional[Dict[str, Any]]
    craft_run_id: Optional[str]
    craft_variants: List[Dict[str, Any]]
    craft_status: str
    print_report: Optional[str]
    print_ok: bool
    approval_status: str  # "pending" | "approved" | "rejected"
    approval_id: Optional[str]
    status: str
    published: List[str]


_SHIM_DIR = None


def _inkscape_shim_dir():
    """press가 부르는 `inkscape`를 vectorcraft-cli로 넘기는 shim 디렉터리."""
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


def _run_subprocess(args, cwd=None):
    env = os.environ.copy()
    shim = _inkscape_shim_dir()
    if shim:
        env["PATH"] = shim + os.pathsep + env.get("PATH", "")
    done = subprocess.run(
        ["python3", *map(str, args)],
        capture_output=True,
        text=True,
        cwd=str(cwd or PRESS_DIR),
        env=env,
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"실패: {' '.join(map(str, args))}\n{done.stdout[-500:]}\n{done.stderr[-500:]}"
        )
    return done


def press_mark_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """press로 마크 후보를 생성하고, top 1을 '잠금'한다. mark_source != 'generate'면 건너뛴다."""
    if state.get("mark_source", "none") != "generate":
        return {"status": "mark_skipped"}

    brief_text = state.get("brief", "Sonagi Package")
    out_dir = OUT_ROOT / f"press_{uuid.uuid4().hex[:6]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # press 브리프 YAML 생성
    import yaml

    palette = ["#161616", "#D97706", "#FBF9F5"]
    brief_doc = {
        "brand_name": brief_text,
        "palette": palette,
        "concept": state.get("concept", "press_imprint"),
        "media_scope": ["web", "print", "favicon"],
    }
    brief_path = out_dir / "brief.yaml"
    brief_path.write_text(
        yaml.safe_dump(brief_doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    cmd = [
        str(PRESS_GRAPH),
        "run",
        str(brief_path),
        "--out",
        str(out_dir),
        "--candidates",
        "12",
    ]
    concept = state.get("concept")
    if concept:
        cmd += ["--concept", concept]
    _run_subprocess(cmd, cwd=PRESS_DIR)

    run_doc = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))
    selected = run_doc.get("selected", [])
    if not selected:
        raise RuntimeError("press: 선택된 마크가 없습니다")

    mark_id = selected[0]["id"]
    mark_svg = (out_dir / "candidates" / mark_id / "mark.svg").read_text(
        encoding="utf-8"
    )

    return {
        "press_run_dir": str(out_dir),
        "press_selected": selected,
        "mark_svg": mark_svg,
        "mark_path": str(out_dir / "candidates" / mark_id / "mark.svg"),
        "status": "mark_locked",
    }


def load_mark_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """mark_source='existing'일 때 기존 마크를 로드한다."""
    if state.get("mark_source") != "existing":
        return {"status": "mark_none"}
    path = state.get("mark_path")
    if not path or not Path(path).exists():
        raise ValueError(f"mark_path가 유효하지 않습니다: {path}")
    return {"mark_svg": Path(path).read_text(encoding="utf-8"), "status": "mark_loaded"}


def craft_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """asset_craft로 패키지 변주를 생성한다. mark_svg가 있으면 브리프에 주입한다.

    asset_craft는 sonagi-assets 카탈로그에 의존하므로, 카탈로그가 없거나
    본문이 카탈로그 항목을 참조하지 못하면 실패할 수 있다. 그 경우 에러를
    상태에 기록하고 빈 변주로 계속 진행한다 (그래프 전체가 죽지 않도록).
    """
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


def print_prep_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """인쇄 준비 검증 (sonagi-print-ops 체크리스트의 결정론 부분)."""
    spec = state.get("print_spec")
    if not spec:
        return {
            "print_ok": True,
            "print_report": "인쇄 사양 없음 — 건너뜀",
            "status": "print_skipped",
        }

    issues = []
    # 1. 변주 SVG 파일 존재 확인
    variants = state.get("craft_variants", [])
    craft_status = state.get("craft_status", "")
    if craft_status.startswith("error"):
        issues.append(f"asset_craft 실패: {craft_status}")
    if not variants and not craft_status.startswith("error"):
        issues.append("변주가 생성되지 않았습니다")

    # 2. 마크 존재 확인
    if state.get("mark_source") != "none" and not state.get("mark_svg"):
        issues.append("마크가 잠기지 않았습니다")

    # 3. bleed 확인 (스펙에 명시된 값)
    bleed = spec.get("bleed_mm", 0)
    if bleed < 3:
        issues.append(f"bleed {bleed}mm < 3mm (sonagi-print-ops 기준)")

    # 4. CMYK 요청 확인
    if spec.get("cmyk") and not spec.get("cmyk_profile"):
        issues.append("CMYK 프로파일 미지정 (ISO Coated v2 권장)")

    ok = len(issues) == 0
    report = "✅ 인쇄 준비 통과" if ok else "⚠️ " + "; ".join(issues)
    return {"print_ok": ok, "print_report": report, "status": "print_checked"}


def hitl_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """디렉터 승인 게이트."""
    if state.get("approval_status") == "approved":
        return {"status": "approved"}
    pause_msg = (
        "⏳ **디렉터 결재 대기 중**\n"
        f"- 마크: {'잠김' if state.get('mark_svg') else '없음'}\n"
        f"- 변주: {len(state.get('craft_variants', []))}개\n"
        f"- 인쇄: {state.get('print_report', '-')}\n"
        "- 승인 시 `approval_status='approved'`로 재개한다."
    )
    return {
        "approval_status": "pending",
        "status": "awaiting_approval",
    }


def publish_node(state: PackageDeliveryState) -> Dict[str, Any]:
    """최종 납품: 출력물을 delivery 디렉터리로 복사."""
    delivery = OUT_ROOT / "delivery"
    delivery.mkdir(parents=True, exist_ok=True)
    published = []
    # 마크 SVG
    if state.get("mark_svg"):
        mark_path = delivery / "mark.svg"
        mark_path.write_text(state["mark_svg"], encoding="utf-8")
        published.append(str(mark_path))
    return {"published": published, "status": "delivered"}


def route_after_hitl(state: PackageDeliveryState):
    return "publish" if state.get("approval_status") == "approved" else END


def build_package_delivery_graph(checkpointer=None):
    builder = StateGraph(PackageDeliveryState)

    builder.add_node("press_mark", press_mark_node)
    builder.add_node("load_mark", load_mark_node)
    builder.add_node("craft", craft_node)
    builder.add_node("print_prep", print_prep_node)
    builder.add_node("hitl", hitl_node)
    builder.add_node("publish", publish_node)

    # mark_source에 따라 press 또는 load_mark로 분기
    def route_mark(state):
        src = state.get("mark_source", "none")
        if src == "generate":
            return "press_mark"
        if src == "existing":
            return "load_mark"
        return "craft"

    builder.add_conditional_edges(
        START,
        route_mark,
        {
            "press_mark": "press_mark",
            "load_mark": "load_mark",
            "craft": "craft",
        },
    )
    builder.add_edge("press_mark", "craft")
    builder.add_edge("load_mark", "craft")
    builder.add_edge("craft", "print_prep")
    builder.add_edge("print_prep", "hitl")
    builder.add_conditional_edges(
        "hitl", route_after_hitl, {"publish": "publish", END: END}
    )
    builder.add_edge("publish", END)

    return builder.compile(checkpointer=checkpointer, interrupt_before=["hitl"])


graph = build_package_delivery_graph()
