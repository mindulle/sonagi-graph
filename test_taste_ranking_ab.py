"""T3 (GRAPH-37) 후보 순서·설명에 취향 반영 + A/B top-3 일치율 테스트."""

from __future__ import annotations

from graphs import entry_pack_graph as mod
from graphs.taste import rank as taste_rank


def test_rank_top_options_preserves_order_when_no_taste_signals():
    options = [
        {"id": "z-opt", "style": "s1"},
        {"id": "a-opt", "style": "s2"},
    ]
    res = taste_rank.rank_top_options(options, profile={"decisions": 0}, decisions=[])
    assert [o["id"] for o in res["options"]] == ["z-opt", "a-opt"]
    assert res["confidence"] == "early"
    assert "early" in res["options"][0]["recommendation_reason"]


def test_rank_top_options_reorders_by_taste_and_adds_reason():
    options = [
        {"id": "opt-low", "style": "cold-grid", "score": 90},
        {"id": "opt-high", "style": "forest-paper", "score": 80},
        {"id": "opt-mid", "style": "warm-editorial", "score": 85},
    ]
    profile = {
        "decisions": 5,
        "style_winrate": {"forest-paper": 0.85, "warm-editorial": 0.5, "cold-grid": 0.1},
    }
    res = taste_rank.rank_top_options(options, profile=profile, decisions=[])

    assert [o["id"] for o in res["options"]] == ["opt-high", "opt-mid", "opt-low"]
    assert res["confidence"] == "early"
    assert res["options"][0]["taste_confidence"] == "early"
    assert "forest-paper 승률 0.85" in res["options"][0]["recommendation_reason"]
    assert res["ab"]["before_top3"] == ["opt-low", "opt-high", "opt-mid"]
    assert res["ab"]["after_top3"] == ["opt-high", "opt-mid", "opt-low"]


def test_gate_and_hitl_record_top3_agreement_without_touching_generation():
    critiques = [
        {"id": "c1", "verdict": "PASS", "findings": [], "score": 95},
        {"id": "c2", "verdict": "PASS", "findings": [], "score": 90},
        {"id": "c3", "verdict": "PASS", "findings": [], "score": 85},
    ]
    renders = [
        {"id": "c1", "style": "cold-grid", "direction": "A", "theme": "light"},
        {"id": "c2", "style": "forest-paper", "direction": "B", "theme": "light"},
        {"id": "c3", "style": "warm-editorial", "direction": "C", "theme": "light"},
    ]
    state = {
        "spec": {"top_n": 3},
        "critiques": critiques,
        "renders": renders,
        "taste_profile": {
            "decisions": 35,
            "style_winrate": {"forest-paper": 0.9, "cold-grid": 0.1},
        },
    }
    gated = mod.gate_node(state)
    assert gated["options"][0]["id"] == "c2"
    assert gated["options"][0]["taste_confidence"] == "stable"
    assert gated["taste_ab"]["before_top3"] == ["c1", "c2", "c3"]
    assert gated["taste_ab"]["after_top3"] == ["c2", "c1", "c3"]

    selected = mod.hitl_node({**state, **gated, "approval_status": "approved", "selection": ["c2"]})
    assert selected["taste_ab"]["after_hit_rate"] == 1.0
    assert selected["taste_ab"]["before_hit_rate"] == 1.0
