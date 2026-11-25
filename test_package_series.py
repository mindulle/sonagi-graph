"""PackageSeriesGraph 단위 테스트."""

import pytest

from graphs import package_series_graph as mod


def test_graph_compiles():
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import MemorySaver

    g = mod.build_package_series_graph(checkpointer=MemorySaver())
    assert g is not None


def test_lock_mark_loads_existing_svg(tmp_path):
    p = tmp_path / "mark.svg"
    p.write_text('<svg><g id="symbol"/></svg>', encoding="utf-8")

    result = mod.lock_mark({"mark_source": "existing", "mark_path": str(p)})
    assert result["status"] == "mark_locked"
    assert result["mark_svg"] == '<svg><g id="symbol"/></svg>'


def test_lock_mark_skips_if_already_locked():
    result = mod.lock_mark({"mark_svg": "<svg/>", "mark_source": "existing"})
    assert result["status"] == "mark_already_locked"


def test_lock_mark_skips_without_existing_source():
    result = mod.lock_mark({"mark_source": "none"})
    assert result["status"] == "mark_skipped"


def test_lock_mark_rejects_missing_path():
    with pytest.raises(ValueError):
        mod.lock_mark({"mark_source": "existing", "mark_path": "/nope/mark.svg"})


def test_route_after_series_hitl_proceeds_when_approved():
    state = {
        "status": "approved",
        "current_index": 1,
        "deliverables": [{"name": "a"}, {"name": "b"}],
    }
    assert mod.route_after_series_hitl(state) == "deliver_one"

    state2 = {
        "status": "approved",
        "current_index": 2,
        "deliverables": [{"name": "a"}, {"name": "b"}],
    }
    assert mod.route_after_series_hitl(state2) == "publish_all"


def test_route_after_series_hitl_stops_when_not_approved():
    state = {
        "status": "awaiting_approval_x",
        "current_index": 1,
        "deliverables": [{"name": "a"}],
    }
    assert (
        mod.route_after_series_hitl(state) is None
        or mod.route_after_series_hitl(state) == "__end__"
    )
