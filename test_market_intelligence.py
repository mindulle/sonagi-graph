"""market_intelligence 그래프 검증 — L1(노드 계약) + L2/L3(수동 조립 파이프라인).

이 그래프는 외부 호출이 없는 mock 수집기라 노드 조합은 결정론이다. 다만 현재
`IntelligenceState.status`가 `Annotated`가 아니라, collector 3개가 병렬로 status를
쓰는 순간 `InvalidUpdateError`가 난다(그래프 본체 수정은 GRAPH-10 범위 밖).
그래서 E2E는 xfail로 결함을 고정하고, 계약·결정론은 노드 단위로 검증한다.
"""

from __future__ import annotations

import json

import pytest
from langgraph.errors import InvalidUpdateError

from graphs import market_intelligence_graph as mi


def _send_targets(sends):
    targets = []
    for send in sends:
        payload = getattr(send, "arg", None)
        if payload is None:
            payload = getattr(send, "state", None)
        targets.append(payload["target"])
    return targets


def test_dispatcher_contract_and_fanout_targets():
    """dispatcher는 대상 3개를 정하고, 라우터는 대상마다 Send를 만든다."""
    state = mi.dispatcher_node({})

    assert state["status"] == "dispatching"
    assert state["targets"] == ["naver", "reddit", "aliexpress"]

    sends = mi.continue_to_collectors({"targets": state["targets"]})
    assert len(sends) == 3
    assert all(send.node == "collector" for send in sends)
    assert sorted(_send_targets(sends)) == ["aliexpress", "naver", "reddit"]


def test_collector_and_analyzer_node_contracts():
    """수집기는 자기 타겟 1건만 반환하고, 분석기는 전체 건수를 집계한다."""
    collected = mi.collector_node({"target": "naver"})

    assert collected["status"] == "collected_naver"
    assert collected["raw_data"] == [
        {"source": "naver", "info": "sample data from naver"}
    ]

    analyzed = mi.analyzer_node({"raw_data": collected["raw_data"] * 3})
    assert analyzed["status"] == "analyzing"
    assert analyzed["analyzed_data"] == [{"result": "filtered data", "count": 3}]


def test_analyzer_degrades_safely_with_no_input():
    """수집이 0건이어도 분석기는 예외 없이 count=0을 낸다(빈 결과 경로)."""
    analyzed = mi.analyzer_node({"raw_data": []})

    assert analyzed["analyzed_data"] == [{"result": "filtered data", "count": 0}]
    assert analyzed["status"] == "analyzing"


def test_loader_returns_only_status():
    """loader는 DB 적재 상태만 되돌리고 수집 데이터를 재기록하지 않는다."""
    state = mi.loader_node({"raw_data": [{"source": "naver"}], "status": "analyzing"})

    assert state == {"status": "completed"}


def test_empty_targets_fan_out_to_nothing():
    assert mi.continue_to_collectors({"targets": []}) == []


#: L3 골든 — mock 수집 파이프라인의 정규화 결과(수집기 상수가 바뀌면 갱신 대상).
EXPECTED_PIPELINE = (
    '{"analyzed_data": [{"count": 3, "result": "filtered data"}], '
    '"raw_data": [{"info": "sample data from aliexpress", "source": "aliexpress"}, '
    '{"info": "sample data from naver", "source": "naver"}, '
    '{"info": "sample data from reddit", "source": "reddit"}], '
    '"targets": ["naver", "reddit", "aliexpress"]}'
)


def test_l3_manual_fanout_pipeline_is_deterministic():
    """L3: Send 팬아웃을 수동으로 접은 파이프라인이 2회 동일하고 골든과 일치한다."""

    def run() -> str:
        dispatched = mi.dispatcher_node({})
        collected = [
            mi.collector_node({"target": target})["raw_data"][0]
            for target in dispatched["targets"]
        ]
        analyzed = mi.analyzer_node({"raw_data": collected})
        return json.dumps(
            {
                "targets": dispatched["targets"],
                "raw_data": sorted(collected, key=lambda row: row["source"]),
                "analyzed_data": analyzed["analyzed_data"],
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    assert run() == run()
    assert run() == EXPECTED_PIPELINE


@pytest.mark.xfail(
    reason=(
        "IntelligenceState.status가 Annotated가 아니라 collector 병렬 쓰기에서 "
        "InvalidUpdateError. graphs/market_intelligence_graph.py 수정 필요(GRAPH-10 범위 밖)"
    ),
    raises=InvalidUpdateError,
    strict=False,
)
def test_graph_fanout_e2e_runs_three_collectors():
    from langgraph.checkpoint.memory import MemorySaver

    graph = mi.build_market_intelligence_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "market-e2e"}}

    state = graph.invoke({"status": "init"}, config)

    assert sorted(row["source"] for row in state["raw_data"]) == [
        "aliexpress",
        "naver",
        "reddit",
    ]
    assert state["analyzed_data"] == [{"result": "filtered data", "count": 3}]
    assert graph.get_state(config).next == ("loader",)
