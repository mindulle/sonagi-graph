"""PackageSeriesGraph 단위 테스트."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.runnables import RunnableLambda

from graphs import package_series_graph as mod


def _fake_press_run(out_dir):
    out = Path(out_dir)
    cand = out / "candidates" / "c01"
    cand.mkdir(parents=True, exist_ok=True)
    (cand / "mark.svg").write_text('<svg><g id="symbol"/></svg>', encoding="utf-8")
    (out / "run.json").write_text(
        '{"selected": [{"id": "c01", "territory": "stamp_bar", "score": 0.8}], "status": "awaiting_approval"}',
        encoding="utf-8",
    )
    return 0


def test_graph_compiles():
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import MemorySaver

    g = mod.build_package_series_graph(checkpointer=MemorySaver())
    assert g is not None


def test_press_mark_once_generates_and_locks(monkeypatch, tmp_path):
    monkeypatch.setenv("SONAGI_PRESS_DIR", str(tmp_path / "press"))
    monkeypatch.setattr(mod, "OUT_ROOT", tmp_path / "out")

    def fake_run(args, cwd=None, env=None):
        out = Path(args[args.index("--out") + 1])
        _fake_press_run(out)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    import graphs.logo_design_graph as craft_mod

    monkeypatch.setattr(craft_mod, "_run_subprocess", fake_run)

    result = mod.press_mark_once({"brief": "TestBrand", "mark_source": "generate"})
    assert result["status"] == "mark_locked"
    assert result["mark_svg"] is not None
    assert result["mark_path"].endswith("mark.svg")


def test_press_mark_once_skips_if_already_locked():
    result = mod.press_mark_once({"mark_svg": "<svg/>", "mark_source": "generate"})
    assert result["status"] == "mark_already_locked"


def test_press_mark_once_skips_if_not_generate():
    result = mod.press_mark_once({"mark_source": "existing"})
    assert result["status"] == "mark_skipped"


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
