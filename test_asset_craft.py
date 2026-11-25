"""asset_craft 그래프 검증 — L2(fixture E2E) + L3(결정론).

`sonagi-assets` 실저장소·실렌더·네트워크 없이 돈다. `asset_repo` 픽스처가
`scripts/*.py`를 결정론 스텁으로 대신하고, 그래프 모듈의 경로 상수만 바꾼다.
그래프 파일(`graphs/asset_craft_graph.py`)은 수정하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from graphs import asset_craft_graph as ac

BRIEF = "민들레 홀씨 미니멀 인쇄물"

#: L3 골든 — 고정 브리프의 IR·참조·후보 계획 지문(결정론이 깨지면 갱신 대상).
PLAN_SHA256 = "97103695c9cad8a23c9ab6fdb96ba8f821aec8091cd57f8fc0af3df028c14afa"


def _run_to_gate(graph, config, *, content="본문", run_id="run-e2e"):
    return graph.invoke(
        {"brief": BRIEF, "content": content, "run_id": run_id},
        config,
    )


def test_pipeline_builds_candidates_and_stops_at_human_gate(asset_repo):
    """brief→scout→compose→render→critic→select 계약과 HITL 정지 지점을 고정한다."""
    from langgraph.checkpoint.memory import MemorySaver

    graph = ac.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "craft-e2e"}}
    state = _run_to_gate(graph, config)

    # 선택 전에 멈춘다 — 사람이 options를 보고 selection을 넣어야 approve가 돈다.
    assert graph.get_state(config).next == ("approve",)
    assert state["status"] == "awaiting_selection"

    # brief: 자연어 → IR(톤·매체). 인쇄물이면 모바일 브레이크포인트만 만든다.
    assert state["ir"]["tones"] == ["미니멀"]
    assert state["ir"]["media"] == "print"
    assert state["run_id"] == "run-e2e"

    # scout: 주제어(dandelion/seed)로 참조를 고르고 blocked 라이선스는 후보에서 뺀다.
    references = state["references"]
    assert references, "주제어가 맞는 참조가 최소 1건 있어야 한다"
    assert all(ref["scope"] in {"bundle", "output"} for ref in references)
    assert all(ref["name"] != "Seed Blocked" for ref in references)
    # 쿼터 3/3/2를 억지로 메우지 않는다 — 실제로 채운 수와 부족 사유가 남는다.
    assert state["scout_report"]["filled"] == {
        "illustration": 2,
        "icon": 1,
        "typography": 1,
    }
    assert len(references) == 4
    assert {row["role"] for row in state["scout_report"]["shortfall"]} == {
        "illustration",
        "icon",
        "typography",
    }

    # compose: 인쇄물 → mobile, light. 후보 3개.
    variants = state["variants"]
    assert len(variants) == 3
    assert {v["breakpoint"] for v in variants} == {"mobile"}
    assert {v["theme"] for v in variants} == {"light"}

    # render: 스텁 렌더러 산출물이 실제로 남는다(SVG + manifest).
    renders = state["renders"]
    assert len(renders) == len(variants)
    for render in renders:
        assert Path(render["svg"]).is_file()
        assert Path(render["manifest"]).is_file()
        assert Path(render["svg"]).read_text(encoding="utf-8").startswith("<svg")

    # critic: 렌더마다 린트 결과가 붙고, id/svg가 이어져야 선택 단계가 맞는다.
    critiques = {row["id"]: row for row in state["critiques"]}
    assert set(critiques) == {v["id"] for v in variants}
    assert all(
        {"verdict", "findings", "plan_score", "id", "svg"} <= set(row)
        for row in critiques.values()
    )

    # select: FAIL(risk-loud)은 선택지에서 빠지고, 점수 내림차순으로 3개 이하.
    options = state["options"]
    assert options, "통과/경고 후보가 있어야 한다"
    assert all(option["verdict"] != "fail" for option in options)
    assert [o["score"] for o in options] == sorted(
        (o["score"] for o in options), reverse=True
    )
    assert "risk-loud-mobile-light" not in {o["id"] for o in options}


def test_human_selection_is_written_to_decision_ledger(asset_repo):
    """선택 재개 → 원장(decisions.json)에 스타일 판정과 스카우트 근거가 남는다."""
    from langgraph.checkpoint.memory import MemorySaver

    graph = ac.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "craft-ledger"}}
    state = _run_to_gate(graph, config)
    chosen = state["options"][-1]["id"]

    graph.update_state(config, {"selection": chosen})
    final = graph.invoke(None, config)

    assert final["status"] == "approved"
    assert final["selection"] == chosen
    assert final["messages"][-1].content.startswith("선택: ")

    ledger = Path(final["ledger"])
    assert ledger.is_file()
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["by"] == "craft-graph"
    assert any(decision.startswith("style=") for decision in payload["decisions"])
    assert any(decision.startswith("scout=") for decision in payload["decisions"])


def test_unknown_selection_falls_back_to_top_option(asset_repo):
    """사람이 없는 id를 넣어도 조용히 최상위 후보로 확정된다(원장 오염 없음)."""
    from langgraph.checkpoint.memory import MemorySaver

    graph = ac.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "craft-fallback"}}
    state = _run_to_gate(graph, config)
    top = state["options"][0]["id"]

    graph.update_state(config, {"selection": "does-not-exist"})
    final = graph.invoke(None, config)

    assert final["selection"] == top
    assert final["status"] == "approved"


def test_lint_failure_is_excluded_and_fail_is_never_silently_promoted():
    """select_node는 FAIL을 버리지 않고 후보에서만 제외한다(경고는 남긴다)."""
    critiques = [
        {"id": "a-pass", "verdict": "pass", "findings": []},
        {
            "id": "b-fail",
            "verdict": "fail",
            "findings": [{"rule": "contrast", "severity": "fail"}],
        },
        {
            "id": "c-warn",
            "verdict": "warn",
            "findings": [{"rule": "spacing", "severity": "warn"}],
        },
    ]

    state = ac.select_node({"critiques": critiques})

    assert state["status"] == "awaiting_selection"
    assert [option["id"] for option in state["options"]] == ["a-pass", "c-warn"]
    assert state["options"][1]["rules"] == ["spacing"]


def test_render_and_approve_error_paths_are_explicit(asset_repo):
    """content 누락·후보 전멸은 조용히 넘어가지 않고 명시적 예외로 끝난다."""
    with pytest.raises(ValueError, match="content"):
        ac.render_node({"run_id": "no-content", "variants": [], "content": ""})

    with pytest.raises(RuntimeError, match="후보가 없습니다"):
        ac.approve_node({"selection": "x", "options": [], "renders": []})


def test_scout_reports_shortfall_without_topic_terms(asset_repo, monkeypatch):
    """주제어가 없으면 다른 역할로 억지로 메우지 않고 shortfall에 이유를 남긴다."""
    catalog = asset_repo.root / "test_data/golden/catalog-empty.json"
    catalog.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "x1",
                        "name": "Unrelated",
                        "category": "painting",
                        "ext": "png",
                        "topics": ["unrelated"],
                        "license_scope": "bundle",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ac, "CATALOG", catalog)
    monkeypatch.setenv("SONAGI_CATALOG", str(catalog))

    state = ac.scout_node({"brief": "미니멀 인쇄물"})

    assert state["references"] == []
    assert state["scout_report"]["terms"] == []
    assert state["scout_report"]["shortfall"]
    assert "주제어 없음" in state["scout_report"]["shortfall"][0]["reason"]


def test_web_brief_switches_to_desktop_breakpoint(asset_repo):
    """매체가 web이면 인쇄물(mobile)과 달리 desktop 브레이크포인트로 계획한다."""
    state = ac.compose_node({"brief": "따뜻한 랜딩"})

    assert state["status"] == "composed"
    assert {v["breakpoint"] for v in state["variants"]} == {"desktop"}
    assert len(state["variants"]) == 3


def _plan_snapshot() -> dict:
    state = {"brief": BRIEF, "run_id": "l3-fixed"}
    state.update(ac.brief_node(state))
    state.update(ac.scout_node(state))
    state.update(ac.compose_node(state))
    return {
        "ir": state["ir"],
        "references": state["references"],
        "scout_report": state["scout_report"],
        "variants": state["variants"],
    }


def test_l3_brief_scout_compose_is_deterministic(asset_repo):
    """L3: 같은 브리프 2회 → IR·참조·후보가 동일하고, 골든 계획과도 일치한다."""
    first, second = _plan_snapshot(), _plan_snapshot()

    assert first == second
    canonical = json.dumps(first, sort_keys=True, ensure_ascii=False)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == PLAN_SHA256
    assert [variant["id"] for variant in first["variants"]] == [
        "minimal-clean-mobile-light",
        "risk-loud-mobile-light",
        "warn-soft-mobile-light",
    ]
    assert [ref["id"] for ref in first["references"]] == [
        "ill-1",
        "ico-1",
        "ill-2",
        "typo-1",
    ]
