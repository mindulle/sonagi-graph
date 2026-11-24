"""CopywritingCraftGraph 단위 테스트 — LLM 호출은 스텁으로 대체한다.

클리셰 필터 → 오감 주입 → 3색 카피 → HITL 결재 흐름과, 선택 인덱스가 승자를 고르는지 확인한다.
"""

import pytest
from langchain_core.runnables import RunnableLambda

from graphs import copywriting_craft_graph as mod
from langgraph.checkpoint.memory import MemorySaver
mod.graph = mod.build_copywriting_craft_graph(checkpointer=MemorySaver())
from graphs.copywriting_craft_graph import (
    ClicheAnalysis,
    CopyOptionItem,
    CopywritingOutput,
    SensoryMetaphor,
)

OPTIONS = [
    CopyOptionItem(
        flavor="A안 (담백·절제형)",
        title="단단한 뿌리",
        verse="겨울을 견딘다",
        prose="p",
        cadence_score=80,
        rationale="r",
    ),
    CopyOptionItem(
        flavor="B안 (시적·서정형)",
        title="45도 소나기",
        verse="유리 위를 흐른다",
        prose="p",
        cadence_score=90,
        rationale="r",
    ),
    CopyOptionItem(
        flavor="C안 (철학·주권형)",
        title="기계는 일하고",
        verse="사람은 쉽니다",
        prose="p",
        cadence_score=85,
        rationale="r",
    ),
]


class _StubLLM:
    def __init__(self):
        self.payloads = {
            "ClicheAnalysis": ClicheAnalysis(
                cliches_found=["최고의"], abstract_words=["혁신"], critique="c"
            ),
            "SensoryMetaphor": SensoryMetaphor(
                tactile_words=["흑요석"], atmospheric_elements=["6500K"]
            ),
            "CopywritingOutput": CopywritingOutput(options=OPTIONS),
        }

    def with_structured_output(self, schema):
        return RunnableLambda(lambda _prompt_value: self.payloads[schema.__name__])


@pytest.fixture
def stub_llm(monkeypatch):
    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: _StubLLM())


def _start(thread_id):
    config = {"configurable": {"thread_id": thread_id}}
    list(
        mod.graph.stream(
            {"scene_id": "Act 1", "raw_draft": "최고의 혁신", "context": "랜딩"},
            config=config,
        )
    )
    return config


def test_three_options_then_pause_before_hitl(stub_llm):
    config = _start("pause")
    state = mod.graph.get_state(config)

    assert state.next == ("hitl_checkpoint",)
    assert len(state.values["copy_options"]) == 3
    assert state.values["approval_status"] == "pending"
    assert state.values.get("winning_copy") is None


def test_selected_option_becomes_winner(stub_llm):
    config = _start("select")
    mod.graph.update_state(config, {"selected_option_index": 1})
    list(mod.graph.stream(None, config=config))

    values = mod.graph.get_state(config).values
    assert values["status"] == "published"
    assert values["approval_status"] == "approved"
    assert values["winning_copy"]["title"] == "45도 소나기"


def test_out_of_range_index_does_not_publish(stub_llm):
    config = _start("bad-index")
    mod.graph.update_state(config, {"selected_option_index": 99})
    list(mod.graph.stream(None, config=config))

    values = mod.graph.get_state(config).values
    assert values["approval_status"] == "pending"
    assert values.get("winning_copy") is None
    assert values["status"] == "awaiting_director_approval"
