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


# ---------------------------------------------------------------------------
# 신규 노드: concept, context_test, refine, iteration
# ---------------------------------------------------------------------------


def test_concept_node_generates_directions(monkeypatch):
    """concept_node가 LLM으로 3~5개 방향을 생성한다."""
    from langchain_core.runnables import RunnableLambda
    from graphs.package_delivery_graph import ConceptOutput, ConceptDirection

    stub = RunnableLambda(lambda _: ConceptOutput(directions=[
        ConceptDirection(title="미니멀 모노그램", description="이니셜 결합", press_concept="monogram", territory_hint="monogram_overlap"),
        ConceptDirection(title="네거티브 스페이스", description="여백 활용", press_concept="negative_space", territory_hint="negative_hidden"),
        ConceptDirection(title="압인", description="전통 활자", press_concept="press_imprint", territory_hint="seal_frame"),
    ]))
    monkeypatch.setattr(mod, "chat_model", lambda *a, **k: type("L", (), {"with_structured_output": lambda self, s: stub})())

    result = mod.concept_node({"brief": "민들레 스튜디오"})
    assert len(result["concepts"]) == 3
    assert result["status"] == "concepted"
    assert result["concepts"][0]["press_concept"] == "monogram"


def test_concept_node_respects_user_concept_override(monkeypatch):
    """사용자가 concept을 직접 지정하면 모든 방향이 그 concept으로 고정된다."""
    from langchain_core.runnables import RunnableLambda
    from graphs.package_delivery_graph import ConceptOutput, ConceptDirection

    stub = RunnableLambda(lambda _: ConceptOutput(directions=[
        ConceptDirection(title="A", description="d", press_concept="monogram", territory_hint="monogram_overlap"),
    ]))
    monkeypatch.setattr(mod, "chat_model", lambda *a, **k: type("L", (), {"with_structured_output": lambda self, s: stub})())

    result = mod.concept_node({"brief": "x", "concept": "mascot"})
    assert result["concepts"][0]["press_concept"] == "mascot"


def test_context_test_skips_when_no_mark():
    result = mod.context_test_node({"mark_svg": None})
    assert result["context_test_score"] == 100
    assert result["status"] == "context_skipped"


def test_context_test_renders_multiple_sizes(monkeypatch, tmp_path):
    """context_test_node가 여러 크기로 렌더링한다."""
    import subprocess
    from pathlib import Path

    def fake_run(cmd, capture_output=True, text=True, timeout=None, env=None, **kw):
        # vectorcraft-cli convert ... --out PATH --scale S
        out = cmd[cmd.index("--scale") - 1] if "--scale" in cmd else None
        # find --out
        if "--out" in cmd:
            out = cmd[cmd.index("--out") + 1]
        if out:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"\x00" * 200)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod, "VECTORCRAFT", "vectorcraft-cli")
    monkeypatch.setattr("subprocess.run", fake_run)

    svg = '<svg viewBox="0 0 120 120" width="120" height="120"><circle cx="60" cy="60" r="40" fill="#161616"/></svg>'
    result = mod.context_test_node({"mark_svg": svg})
    assert result["status"] == "context_tested"
    assert result["context_test_score"] >= 0
    assert "sizes" in result["context_test_report"]


def test_critic_combines_context_and_craft():
    state = {"context_test_score": 85, "status": "crafted"}
    result = mod.critic_node(state)
    assert result["context_test_score"] == 85
    assert result["status"] == "critiqued"

    # craft 실패 시 20점 감점
    state2 = {"context_test_score": 85, "status": "craft_failed"}
    result2 = mod.critic_node(state2)
    assert result2["context_test_score"] == 65


def test_route_after_context_loops_on_low_score():
    # 점수 70 미만 + iteration 0 < max 2 → refine
    assert mod.route_after_context({"context_test_score": 50, "iteration_count": 0, "max_iterations": 2}) == "refine"
    # 점수 70 이상 → print_prep
    assert mod.route_after_context({"context_test_score": 80, "iteration_count": 0, "max_iterations": 2}) == "print_prep"
    # iteration 초과 → 강제 진행
    assert mod.route_after_context({"context_test_score": 50, "iteration_count": 2, "max_iterations": 2}) == "print_prep"


def test_refine_increments_iteration(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    stub = RunnableLambda(lambda _: type("R", (), {"content": '{"brief": "보강된 브리프", "concept": "monogram"}'})())
    monkeypatch.setattr(mod, "chat_model", lambda *a, **k: type("L", (), {"with_structured_output": lambda self, s: stub})() if False else type("L", (), {})())
    # chat_model returns an object with .with_structured_output or direct invoke
    # refine uses prompt | llm (no structured output), so llm needs .invoke
    from langchain_core.runnables import RunnableLambda

    def fake_invoke(msgs):
        return type("R", (), {"content": '{"brief": "보강된 브리프", "concept": "monogram"}'})()
    monkeypatch.setattr(mod, "chat_model", lambda *a, **k: RunnableLambda(lambda msgs: fake_invoke(msgs)))

    result = mod.refine_node({"brief": "원래 브리프", "context_test_report": {"issues": ["16px에서 안 보임"]}, "iteration_count": 0})
    assert result["brief"] == "보강된 브리프"
    assert result["concept"] == "monogram"
    assert result["iteration_count"] == 1
    assert result["status"] == "refined"


def test_refine_fallback_on_llm_failure(monkeypatch):
    class BadLLM:
        def invoke(self, msgs):
            raise RuntimeError("LLM down")
    monkeypatch.setattr(mod, "chat_model", lambda *a, **k: BadLLM())

    result = mod.refine_node({"brief": "원래", "context_test_report": {"issues": ["fail"]}, "iteration_count": 1})
    assert "원래" in result["brief"]
    assert result["iteration_count"] == 2
    assert result["status"] == "refined"
