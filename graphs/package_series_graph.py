"""Package Series Graph — 3건 납품을 한 번에 돌리는 배치 오케스트레이터.

마크를 1번만 잠그고, 그 마크로 N개의 패키지를 순차 생성한다.
마크는 편집 도구(VectorCraft·DesignCraft)에서 만든 SVG를 `mark_path`로 넘긴다.
마크가 없으면 레이아웃만 검증한다(`mark_source="none"`).
각 패키지는 독립적인 HITL 게이트를 가진다.

사용::

    from graphs.package_series_graph import build_package_series_graph

    graph = build_package_series_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "series-1"}}
    graph.invoke({
        "brief": "민들레 스튜디오 패키지 세트",
        "mark_source": "existing",
        "mark_path": "/path/to/mark.svg",
        "content": "/path/to/brief.md",
        "deliverables": [
            {"name": "박스", "print_spec": {"bleed_mm": 3, "cmyk": True, "cmyk_profile": "ISO Coated v2"}},
            {"name": "봉투", "print_spec": {"bleed_mm": 3, "cmyk": True, "cmyk_profile": "ISO Coated v2"}},
            {"name": "라벨", "print_spec": {"bleed_mm": 3, "cmyk": True, "cmyk_profile": "ISO Coated v2"}},
        ],
    }, config)
    # 마크 로드 → 패키지 1 → HITL 1 → 패키지 2 → HITL 2 → 패키지 3 → HITL 3 → 납품
"""

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from graphs.logo_design_graph import build_logo_design_graph

OUT_ROOT = Path(os.environ.get("SONAGI_PACKAGE_OUT", "/tmp/package_delivery"))


class PackageSeriesState(TypedDict, total=False):
    brief: str
    concept: Optional[str]
    content: Optional[str]
    deliverables: List[Dict[str, Any]]
    mark_source: str
    mark_svg: Optional[str]
    mark_path: Optional[str]
    current_index: int
    results: List[Dict[str, Any]]
    approval_status: str
    status: str
    published: List[str]


def lock_mark(state: PackageSeriesState) -> Dict[str, Any]:
    """마크를 1번만 확정한다. 편집 도구에서 만든 SVG를 그대로 쓴다."""
    if state.get("mark_svg"):
        return {"status": "mark_already_locked"}
    if state.get("mark_source", "none") != "existing":
        return {"status": "mark_skipped"}

    path = state.get("mark_path")
    if not path or not Path(path).exists():
        raise ValueError(f"mark_path가 유효하지 않습니다: {path}")
    return {
        "mark_svg": Path(path).read_text(encoding="utf-8"),
        "status": "mark_locked",
    }


def deliver_one(state: PackageSeriesState) -> Dict[str, Any]:
    """현재 인덱스의 패키지를 1개 생성한다."""
    idx = state.get("current_index", 0)
    deliverables = state.get("deliverables", [])
    if idx >= len(deliverables):
        return {"status": "all_delivered"}

    d = deliverables[idx]
    name = d.get("name", f"deliverable-{idx}")
    print_spec = d.get("print_spec")

    # logo_design 그래프를 1회 실행 (mark는 공유)
    sub_graph = build_logo_design_graph(checkpointer=MemorySaver(), interrupt_before=[])
    config = {"configurable": {"thread_id": f"series-{uuid.uuid4().hex[:6]}-d{idx}"}}
    sub_graph.invoke(
        {
            "brief": f"{state.get('brief', '')} — {name}",
            "mark_source": "existing" if state.get("mark_svg") else "none",
            "mark_path": state.get("mark_path"),
            "mark_svg": state.get("mark_svg"),
            "content": state.get("content"),
            "print_spec": print_spec,
            "max_iterations": 1,
            "approval_status": "approved",  # 서브그래프는 자동 승인; 시리즈 레벨에서 최종 검토
        },
        config=config,
    )

    sub_state = sub_graph.get_state(config).values

    # 결과 수집
    results = list(state.get("results", []))
    results.append(
        {
            "name": name,
            "index": idx,
            "status": sub_state.get("status", "unknown"),
            "print_report": sub_state.get("print_report"),
            "print_ok": sub_state.get("print_ok", False),
            "context_score": sub_state.get("context_test_score", 0),
            "variants": len(sub_state.get("craft_variants", [])),
        }
    )

    return {
        "results": results,
        "current_index": idx + 1,
        "status": f"delivered_{name}",
    }


def series_hitl_node(state: PackageSeriesState) -> Dict[str, Any]:
    """각 패키지 생성 후 디렉터 승인 게이트."""
    if state.get("approval_status") == "approved":
        return {"status": "approved", "approval_status": "pending"}  # reset for next
    idx = state.get("current_index", 0) - 1
    deliverables = state.get("deliverables", [])
    name = (
        deliverables[idx].get("name", f"deliverable-{idx}")
        if idx < len(deliverables)
        else "unknown"
    )
    return {
        "approval_status": "pending",
        "status": f"awaiting_approval_{name}",
    }


def route_after_series_hitl(state: PackageSeriesState):
    if state.get("status") != "approved":
        return END
    idx = state.get("current_index", 0)
    deliverables = state.get("deliverables", [])
    if idx < len(deliverables):
        return "deliver_one"
    return "publish_all"


def publish_all_node(state: PackageSeriesState) -> Dict[str, Any]:
    results = state.get("results", [])
    published = []
    for r in results:
        published.append(f"{r['name']}: {r['status']}")
    return {"published": published, "status": "series_completed"}


def build_package_series_graph(checkpointer=None):
    builder = StateGraph(PackageSeriesState)

    builder.add_node("lock_mark", lock_mark)
    builder.add_node("deliver_one", deliver_one)
    builder.add_node("series_hitl", series_hitl_node)
    builder.add_node("publish_all", publish_all_node)

    builder.add_edge(START, "lock_mark")
    builder.add_edge("lock_mark", "deliver_one")
    builder.add_edge("deliver_one", "series_hitl")
    builder.add_conditional_edges(
        "series_hitl",
        route_after_series_hitl,
        {
            "deliver_one": "deliver_one",
            "publish_all": "publish_all",
        },
    )
    builder.add_edge("publish_all", END)

    return builder.compile(checkpointer=checkpointer, interrupt_before=["series_hitl"])


graph = build_package_series_graph()
