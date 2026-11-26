"""취향 순위(Graph-35) 테스트 — 적은 표본에서 통계를 꾸며내지 않는지 본다."""

from __future__ import annotations

from graphs.taste import rank


OPTIONS = [
    {"id": "paper", "style": "forest-paper"},
    {"id": "star", "style": "star-editorial"},
    {"id": "line", "style": "line-minimal"},
]


def test_style_winrate_orders_candidates():
    result = rank.rank_options(
        OPTIONS,
        profile={
            "decisions": 4,
            "style_winrate": {
                "forest-paper": 0.75,
                "star-editorial": 0.25,
                "line-minimal": 0.0,
            },
        },
        decisions=[],
    )
    assert result["confidence"] == "early"
    assert result["ranked"] == ["paper", "star", "line"]
    text = "\n".join(result["explanations"])
    assert "초기(early)" in text
    assert "forest-paper" in text
    assert "승률 0.75" in text


def test_explicit_tags_and_style_share_the_signal(tmp_path):
    decisions = [
        {
            "ts": "2026-11-01T00:00:00Z",
            "options": [
                {"id": "old-a", "style": "forest-paper"},
                {"id": "old-b", "style": "star-editorial"},
            ],
            "selected": ["old-a"],
            "reason_tags": ["여백", "차분함"],
            "agreement_top1": True,
        },
        {
            "ts": "2026-11-02T00:00:00Z",
            "options": [
                {"id": "old-c", "style": "forest-paper"},
                {"id": "old-d", "style": "star-editorial"},
            ],
            "selected": ["old-c"],
            "reason_tags": ["여백"],
            "agreement_top1": False,
        },
    ]
    options = [
        {"id": "a", "style": "star-editorial", "tags": ["여백"]},
        {"id": "b", "style": "star-editorial"},
        {"id": "c", "style": "forest-paper"},
    ]
    result = rank.rank_options(options, profile=None, decisions=decisions)

    # a: 태그 1.0 + 스타일 0.0 → 0.5, b: 0.0, c: 스타일 1.0 → 1.0
    assert result["ranked"] == ["c", "a", "b"]
    text = "\n".join(result["explanations"])
    assert "최근 선택 2건에서 '여백' 2번" in text
    assert "forest-paper 스타일은 2번 중 2번 선택" in text


def test_before_thirty_decisions_feature_bias_is_ignored():
    options = [
        {"id": "a", "features": {"whitespace_ratio": 0.9}},
        {"id": "b", "features": {"whitespace_ratio": 0.1}},
    ]
    profile = {
        "decisions": 3,
        "feature_bias": {"whitespace_ratio": {"prefer": "low", "weight": 1.0}},
    }
    result = rank.rank_options(options, profile=profile, decisions=[])

    # 특징 편향을 썼다면 b가 1순위다. 30건 미만에서는 무시하고 id 순으로 둔다.
    assert result["confidence"] == "early"
    assert result["ranked"] == ["a", "b"]
    assert "태그와 스타일 승률만" in "\n".join(result["explanations"])


def test_thirty_decisions_unlock_feature_bias():
    options = [
        {"id": "a", "features": {"whitespace_ratio": 0.9}},
        {"id": "b", "features": {"whitespace_ratio": 0.1}},
    ]
    profile = {
        "decisions": 30,
        "feature_bias": {"whitespace_ratio": {"prefer": "low", "weight": 1.0}},
    }
    result = rank.rank_options(options, profile=profile, decisions=[])

    assert result["confidence"] == "stable"
    assert result["ranked"] == ["b", "a"]
    assert "특징 편향까지" in "\n".join(result["explanations"])


def test_ties_break_by_id_deterministically():
    options = [{"id": "z"}, {"id": "a"}, {"id": "m"}]
    first = rank.rank_options(options, profile={"decisions": 0}, decisions=[])
    second = rank.rank_options(
        list(reversed(options)), profile={"decisions": 0}, decisions=[]
    )
    assert first["ranked"] == ["a", "m", "z"]
    assert second["ranked"] == first["ranked"]
    assert first["scores"] == {"a": 0.0, "m": 0.0, "z": 0.0}


def test_no_history_explains_plainly():
    result = rank.rank_options([{"id": "a"}, {"id": "b"}], profile=None, decisions=[])
    assert result["confidence"] == "early"
    assert result["decisions"] == 0
    assert result["ranked"] == ["a", "b"]
    assert result["explanations"] == ["아직 취향 기록이 없어 id 순으로 정렬했습니다."]


def test_top_choice_explanation_names_the_signal():
    options = [
        {"id": "paper", "style": "forest-paper", "tags": ["여백"]},
        {"id": "star", "style": "star-editorial"},
    ]
    profile = {
        "decisions": 2,
        "tag_weights": {"여백": 1.0},
        "style_winrate": {"forest-paper": 0.7},
    }
    result = rank.rank_options(options, profile=profile, decisions=[])
    top_line = result["explanations"][-1]
    assert "1순위는 'paper'" in top_line
    assert "'여백' 태그와 맞습니다" in top_line
    assert "forest-paper 승률 0.70" in top_line
