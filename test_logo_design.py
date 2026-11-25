"""PackageDeliveryGraph 단위 테스트.

마크 생성은 편집 도구(VectorCraft·DesignCraft)가 담당하므로, 그래프는
`mark_source="existing"`(파일 로드) 또는 `"none"`(레이아웃만) 경로만 검증한다.
"""

import pytest
from langchain_core.runnables import RunnableLambda

from graphs import logo_design_graph as mod


def test_graph_compiles_with_checkpointer():
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import MemorySaver

    g = mod.build_logo_design_graph(checkpointer=MemorySaver())
    assert g is not None


def test_route_mark_loads_existing_mark():
    assert mod.route_mark({"mark_source": "existing"}) == "load_mark"


def test_route_mark_falls_back_to_craft():
    # 마크 생성 경로는 제거됐다. 알 수 없는 값도 마크 없이 진행한다.
    assert mod.route_mark({"mark_source": "none"}) == "craft"
    assert mod.route_mark({"mark_source": "generate"}) == "craft"
    assert mod.route_mark({}) == "craft"


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


def test_load_mark_skips_when_source_is_none():
    assert mod.load_mark_node({"mark_source": "none"})["status"] == "mark_none"


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
    from graphs.logo_design_graph import ConceptOutput, ConceptDirection

    stub = RunnableLambda(
        lambda _: ConceptOutput(
            directions=[
                ConceptDirection(
                    title="미니멀 모노그램",
                    description="이니셜 결합",
                    motifs=["겹친 이니셜", "균일 스트로크"],
                    visual_language="2px 단색 선, 정사각 비례",
                ),
                ConceptDirection(
                    title="네거티브 스페이스",
                    description="여백 활용",
                    motifs=["원 안의 여백"],
                    visual_language="면 중심, 1도 인쇄",
                ),
            ]
        )
    )
    monkeypatch.setattr(
        mod,
        "chat_model",
        lambda *a, **k: type(
            "L", (), {"with_structured_output": lambda self, s: stub}
        )(),
    )

    result = mod.concept_node({"brief": "민들레 스튜디오"})
    assert len(result["concepts"]) == 2
    assert result["status"] == "concepted"
    assert result["concepts"][0]["motifs"] == ["겹친 이니셜", "균일 스트로크"]


def test_concept_node_sanitizes_motifs(monkeypatch):
    """모티프는 문자열만, 최대 4개로 정리된다."""
    from graphs.logo_design_graph import ConceptOutput, ConceptDirection

    stub = RunnableLambda(
        lambda _: ConceptOutput(
            directions=[
                ConceptDirection(
                    title="A",
                    description="d",
                    motifs=["a", "b", "c", "d", "e", "f"],
                )
            ]
        )
    )
    monkeypatch.setattr(
        mod,
        "chat_model",
        lambda *a, **k: type(
            "L", (), {"with_structured_output": lambda self, s: stub}
        )(),
    )

    result = mod.concept_node({"brief": "x"})
    assert result["concepts"][0]["motifs"] == ["a", "b", "c", "d"]


def test_context_test_skips_when_no_mark():
    result = mod.context_test_node({"mark_svg": None})
    assert result["context_test_score"] == 100
    assert result["status"] == "context_skipped"


def test_context_test_renders_multiple_sizes(monkeypatch, tmp_path):
    """context_test_node가 여러 크기로 렌더링한다."""
    import subprocess
    from pathlib import Path

    def fake_run(cmd, capture_output=True, text=True, timeout=None, env=None, **kw):
        out = cmd[cmd.index("--out") + 1] if "--out" in cmd else None
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
    assert (
        mod.route_after_context(
            {"context_test_score": 50, "iteration_count": 0, "max_iterations": 2}
        )
        == "refine"
    )
    # 점수 70 이상 → print_prep
    assert (
        mod.route_after_context(
            {"context_test_score": 80, "iteration_count": 0, "max_iterations": 2}
        )
        == "print_prep"
    )
    # iteration 초과 → 강제 진행
    assert (
        mod.route_after_context(
            {"context_test_score": 50, "iteration_count": 2, "max_iterations": 2}
        )
        == "print_prep"
    )


def test_refine_increments_iteration(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    def fake_invoke(msgs):
        return type(
            "R",
            (),
            {"content": '{"brief": "보강된 브리프", "direction": "단순화된 기하"}'},
        )()

    monkeypatch.setattr(
        mod,
        "chat_model",
        lambda *a, **k: RunnableLambda(lambda msgs: fake_invoke(msgs)),
    )

    result = mod.refine_node(
        {
            "brief": "원래 브리프",
            "context_test_report": {"issues": ["16px에서 안 보임"]},
            "iteration_count": 0,
        }
    )
    assert result["brief"] == "보강된 브리프"
    assert result["concept"] == "단순화된 기하"
    assert result["iteration_count"] == 1
    assert result["status"] == "refined"


def test_refine_fallback_on_llm_failure(monkeypatch):
    class BadLLM:
        def invoke(self, msgs):
            raise RuntimeError("LLM down")

    monkeypatch.setattr(mod, "chat_model", lambda *a, **k: BadLLM())

    result = mod.refine_node(
        {
            "brief": "원래",
            "context_test_report": {"issues": ["fail"]},
            "iteration_count": 1,
        }
    )
    assert "원래" in result["brief"]
    assert "방향 전환" in result["brief"]
    assert result["iteration_count"] == 2
    assert result["status"] == "refined"


# ---------------------------------------------------------------------------
# research 노드
# ---------------------------------------------------------------------------


def test_research_skipped_when_mark_source_none():
    result = mod.research_node({"brief": "x", "mark_source": "none"})
    assert result["status"] == "research_skipped"


def test_research_skipped_when_flag_set():
    result = mod.research_node(
        {"brief": "x", "mark_source": "existing", "skip_research": True}
    )
    assert result["status"] == "research_skipped"


def test_research_collects_and_analyzes(monkeypatch):
    """research_node가 source_verification과 knowledge_harvester를 호출하고 LLM으로 분석한다."""
    from langchain_core.runnables import RunnableLambda

    class FakeSV:
        def invoke(self, *a, **k):
            return {
                "verified_sources": [
                    {"source_data": {"title": "Logo Design Reference"}}
                ],
                "status": "completed",
            }

    class FakeKH:
        def invoke(self, *a, **k):
            return {
                "met_museum_results": [{"title": "Bauhaus Poster"}],
                "status": "completed",
            }

    import graphs.logo_design_graph as ldm

    monkeypatch.setattr(ldm, "uuid", __import__("uuid"))

    import sys

    fake_sv_mod = type(sys)("fake_sv")
    fake_sv_mod.build_source_verification_graph = lambda checkpointer=None: FakeSV()
    fake_kh_mod = type(sys)("fake_kh")
    fake_kh_mod.build_knowledge_harvester_graph = lambda checkpointer=None: FakeKH()
    monkeypatch.setitem(sys.modules, "graphs.source_verification_graph", fake_sv_mod)
    monkeypatch.setitem(sys.modules, "graphs.knowledge_harvester_graph", fake_kh_mod)

    class FakeResp:
        content = (
            "추천 방향은 모노그램입니다. type_block과 seal_frame 계열이 적합합니다."
        )

    monkeypatch.setattr(
        ldm, "chat_model", lambda *a, **k: RunnableLambda(lambda _: FakeResp())
    )

    result = ldm.research_node({"brief": "미니멀 로고", "mark_source": "existing"})
    assert result["status"] == "researched"
    assert "recommended_concept" not in result
    assert "type_block" in result["research_summary"]


# ---------------------------------------------------------------------------
# concept 결과 검증
# ---------------------------------------------------------------------------


def test_concept_node_sets_selected_concept(monkeypatch):
    """concept_node가 첫 번째 방향의 제목을 selected_concept으로 설정한다."""
    from graphs.logo_design_graph import ConceptOutput, ConceptDirection

    stub = RunnableLambda(
        lambda _: ConceptOutput(
            directions=[
                ConceptDirection(title="A", description="d"),
                ConceptDirection(title="B", description="d"),
            ]
        )
    )
    monkeypatch.setattr(
        mod,
        "chat_model",
        lambda *a, **k: type(
            "L", (), {"with_structured_output": lambda self, s: stub}
        )(),
    )

    result = mod.concept_node({"brief": "test"})
    assert result["selected_concept"] == "A"
    assert len(result["concepts"]) == 2


def test_concept_node_survives_llm_failure(monkeypatch):
    """LLM이 실패해도 기본 방향 1개로 진행한다."""

    class BadLLM:
        def with_structured_output(self, schema):
            def invoke(_):
                raise RuntimeError("down")

            return invoke

    monkeypatch.setattr(mod, "chat_model", lambda *a, **k: BadLLM())

    result = mod.concept_node({"brief": "test"})
    assert len(result["concepts"]) == 1
    assert result["selected_concept"] == "기본 방향"
