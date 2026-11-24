"""PackageDeliveryGraph 단위 테스트 — press는 monkeypatch로 대체한다."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.runnables import RunnableLambda

from graphs import package_delivery_graph as mod


def _fake_press_run(out_dir):
    """press_graph.py가 만드는 산출물(run.json + mark.svg)을 가짜로 만든다."""
    out = Path(out_dir)
    cand = out / "candidates" / "c01"
    cand.mkdir(parents=True, exist_ok=True)
    (cand / "mark.svg").write_text('<svg><g id="symbol"/></svg>', encoding="utf-8")
    (out / "run.json").write_text(
        json.dumps(
            {
                "selected": [{"id": "c01", "territory": "stamp_bar", "score": 0.8}],
                "status": "awaiting_approval",
            }
        ),
        encoding="utf-8",
    )
    return 0


def test_graph_compiles_with_checkpointer():
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import MemorySaver

    g = mod.build_package_delivery_graph(checkpointer=MemorySaver())
    assert g is not None


def test_press_mark_node_locks_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("SONAGI_PRESS_DIR", str(tmp_path / "press"))
    monkeypatch.setattr(mod, "OUT_ROOT", tmp_path / "out")

    def fake_run(args, cwd=None):
        # args: [python3, press_graph.py, "run", brief, "--out", out, "--candidates", "12"]
        out = Path(args[args.index("--out") + 1])
        _fake_press_run(out)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mod, "_run_subprocess", fake_run)

    result = mod.press_mark_node({"brief": "TestBrand", "mark_source": "generate"})
    assert result["status"] == "mark_locked"
    assert "mark_svg" in result
    assert result["press_selected"][0]["id"] == "c01"


def test_press_mark_skipped_when_not_generate():
    result = mod.press_mark_node({"brief": "x", "mark_source": "none"})
    assert result["status"] == "mark_skipped"


def test_load_mark_requires_existing_file(tmp_path):
    p = tmp_path / "mark.svg"
    p.write_text("<svg/>", encoding="utf-8")
    result = mod.load_mark_node({"mark_source": "existing", "mark_path": str(p)})
    assert result["status"] == "mark_loaded"
    assert result["mark_svg"] == "<svg/>"

    with pytest.raises(ValueError):
        mod.load_mark_node(
            {"mark_source": "existing", "mark_path": str(tmp_path / "nope.svg")}
        )


def test_print_prep_reports_missing_cmyk_profile():
    state = {
        "print_spec": {"bleed_mm": 3, "cmyk": True},
        "craft_variants": [{"id": "v1"}],
        "mark_source": "none",
    }
    result = mod.print_prep_node(state)
    assert result["print_ok"] is False
    assert "CMYK" in result["print_report"]


def test_print_prep_passes_with_full_spec():
    state = {
        "print_spec": {"bleed_mm": 3, "cmyk": True, "cmyk_profile": "ISO Coated v2"},
        "craft_variants": [{"id": "v1"}],
        "mark_source": "none",
    }
    result = mod.print_prep_node(state)
    assert result["print_ok"] is True
