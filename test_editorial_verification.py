"""EditorialVerificationGraph 단위 테스트 — LLM 호출은 스텁으로 대체한다.

실제 프록시 호출 검증은 `graphs/editorial_verification_graph.py`를 직접 스트리밍해서 한다.
여기서는 가중치·판정 임계값·HITL 게이트·볼트 저장 경로만 결정론적으로 확인한다.
"""

import pytest
from langchain_core.runnables import RunnableLambda

from graphs import editorial_verification_graph as mod
from graphs.editorial_verification_graph import (
    AcademicCitationEvaluation,
    AestheticStanceEvaluation,
    DiagramEvaluation,
    LogicalCoherenceEvaluation,
)


class _StubLLM:
    """`prompt | llm.with_structured_output(Schema)`를 그대로 흉내 내는 스텁."""

    def __init__(self, payloads):
        self.payloads = payloads

    def with_structured_output(self, schema):
        return RunnableLambda(lambda _prompt_value: self.payloads[schema.__name__])


def _payload(academic, logical, aesthetic, diagram):
    return {
        "AcademicCitationEvaluation": AcademicCitationEvaluation(
            score=academic,
            has_citations=academic >= 60,
            unsupported_claims=[],
            hallucination_risks=[],
            summary="a",
        ),
        "LogicalCoherenceEvaluation": LogicalCoherenceEvaluation(
            score=logical,
            fallacies=[],
            structural_leaps=[],
            contradictions=[],
            summary="l",
        ),
        "AestheticStanceEvaluation": AestheticStanceEvaluation(
            score=aesthetic,
            identified_stance="미니멀리즘",
            stance_drifts=[],
            voice_consistency="일관",
            summary="s",
        ),
        "DiagramEvaluation": DiagramEvaluation(
            score=diagram,
            has_diagrams=True,
            diagram_needed=False,
            semantic_discrepancies=[],
            visual_flaws=[],
            corrected_diagram_code=None,
            summary="d",
        ),
    }


@pytest.fixture
def stub_llm(monkeypatch, tmp_path):
    def install(academic=90, logical=85, aesthetic=88, diagram=92):
        monkeypatch.setattr(
            mod,
            "get_llm",
            lambda *a, **k: _StubLLM(_payload(academic, logical, aesthetic, diagram)),
        )

    monkeypatch.setattr(mod, "REVIEW_DIR", tmp_path / "Editorial_Reviews")
    return install


def _run(thread_id, stub_llm, **scores):
    stub_llm(**scores)
    config = {"configurable": {"thread_id": thread_id}}
    list(
        mod.graph.stream(
            {"title": "T", "content": "본문", "target_stance": "미니멀리즘"},
            config=config,
        )
    )
    return mod.graph.get_state(config)


def test_scorecard_weights_are_logical35_aesthetic25_diagram20_academic20(stub_llm):
    state = _run("weights", stub_llm, academic=90, logical=80, aesthetic=70, diagram=60)

    # 80*0.35 + 70*0.25 + 60*0.20 + 90*0.20 = 28 + 17.5 + 12 + 18 = 75.5 -> 76
    assert state.values["total_score"] == 76
    assert state.values["verdict"] == "NEEDS_REVISION"


def test_high_scores_pass_and_pause_before_vault(stub_llm):
    state = _run("pass", stub_llm, academic=90, logical=85, aesthetic=88, diagram=92)

    assert state.values["total_score"] == 88
    assert state.values["verdict"] == "PASS"
    assert state.next == ("vault_loader",)
    assert not list((mod.REVIEW_DIR).glob("Review_*.md")), (
        "HITL 승인 전에는 리포트를 저장하지 않는다"
    )


def test_resume_writes_report_to_vault(stub_llm):
    config = {"configurable": {"thread_id": "vault"}}
    stub_llm()
    list(mod.graph.stream({"title": "검증 리포트", "content": "본문"}, config=config))
    list(mod.graph.stream(None, config=config))

    saved = mod.graph.get_state(config).values["saved_path"]
    assert mod.Path(saved).exists()
    assert "PASS" in mod.Path(saved).read_text(encoding="utf-8")


def test_low_scores_reject(stub_llm):
    state = _run("reject", stub_llm, academic=30, logical=40, aesthetic=20, diagram=10)

    assert state.values["verdict"] == "REJECT"
    assert state.values["total_score"] < 50
