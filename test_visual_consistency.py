"""VisualConsistencyGraph 단위 테스트 — LLM 호출은 스텁으로 대체한다.

벡터/모션/이미지 세 크리틱의 가중치(35·35·30), 판정 임계값, HITL 게이트를 결정론적으로 확인한다.
"""

import pytest
from langchain_core.runnables import RunnableLambda

from graphs import visual_consistency_graph as mod
from langgraph.checkpoint.memory import MemorySaver
mod.graph = mod.build_visual_consistency_graph(checkpointer=MemorySaver())
from graphs.visual_consistency_graph import (
    ImageryCritiqueResult,
    MotionCritiqueResult,
    VectorCritiqueResult,
)


class _StubLLM:
    def __init__(self, payloads):
        self.payloads = payloads

    def with_structured_output(self, schema):
        return RunnableLambda(lambda _prompt_value: self.payloads[schema.__name__])


def _payload(vector, motion, imagery):
    return {
        "VectorCritiqueResult": VectorCritiqueResult(
            score=vector,
            stroke_weight_consistent=vector >= 60,
            style_consistent=vector >= 60,
            grid_aligned=True,
            detected_flaws=[],
            corrected_svgs=[],
            summary="v",
        ),
        "MotionCritiqueResult": MotionCritiqueResult(
            score=motion,
            has_linear_motion=motion < 60,
            golden_snap_compliant=motion >= 60,
            has_stagger_offset=motion >= 60,
            detected_flaws=[],
            corrected_motion_code="/* none */",
            summary="m",
        ),
        "ImageryCritiqueResult": ImageryCritiqueResult(
            score=imagery,
            color_temp_consistent=imagery >= 60,
            lighting_consistent=imagery >= 60,
            composition_balanced=True,
            detected_flaws=[],
            styling_recommendations=[],
            summary="i",
        ),
    }


@pytest.fixture
def stub_llm(monkeypatch, tmp_path):
    def install(vector=90, motion=80, imagery=70):
        monkeypatch.setattr(
            mod, "get_llm", lambda *a, **k: _StubLLM(_payload(vector, motion, imagery))
        )

    monkeypatch.setattr(mod, "REVIEW_DIR", tmp_path / "Design_Reviews")
    return install


def _run(thread_id, stub_llm, **scores):
    stub_llm(**scores)
    config = {"configurable": {"thread_id": thread_id}}
    list(
        mod.graph.stream(
            {"project_name": "Unit", "vectors": [], "motions": [], "imagery": []},
            config=config,
        )
    )
    return mod.graph.get_state(config)


def test_scorecard_weights_are_vector35_motion35_imagery30(stub_llm):
    state = _run("weights", stub_llm, vector=90, motion=80, imagery=70)

    # 90*0.35 + 80*0.35 + 70*0.30 = 31.5 + 28 + 21 = 80.5 -> round() 반올림으로 80
    assert state.values["total_score"] == 80
    assert state.values["verdict"] == "PASS"


def test_pause_before_vault_and_no_early_write(stub_llm):
    state = _run("pause", stub_llm, vector=70, motion=60, imagery=55)

    # 70*0.35 + 60*0.35 + 55*0.30 = 24.5 + 21 + 16.5 = 62
    assert state.values["total_score"] == 62
    assert state.values["verdict"] == "NEEDS_REVISION"
    assert state.next == ("vault_loader",)
    assert not list(mod.REVIEW_DIR.glob("Review_*.md"))


def test_resume_writes_design_review(stub_llm):
    config = {"configurable": {"thread_id": "resume"}}
    stub_llm()
    list(mod.graph.stream({"project_name": "Mindulle V3"}, config=config))
    list(mod.graph.stream(None, config=config))

    saved = mod.graph.get_state(config).values["saved_path"]
    assert mod.Path(saved).exists()
    assert "Visual Design Consistency" in mod.Path(saved).read_text(encoding="utf-8")


def test_severe_flaws_reject(stub_llm):
    state = _run("reject", stub_llm, vector=20, motion=10, imagery=30)

    assert state.values["verdict"] == "REJECT"
