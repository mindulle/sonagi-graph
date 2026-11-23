"""Craft 판단 로직 검증 — LLM·서비스 없이 도는 부분."""

import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import craft  # noqa: E402

ASSETS = os.environ.get("SONAGI_ASSETS_DIR", "/home/ubuntu/sonagi-assets")
needs_assets = pytest.mark.skipif(
    not os.path.isfile(os.path.join(ASSETS, "registry/styles.json")),
    reason="sonagi-assets 명부 없음",
)

STYLES = [
    {
        "id": "minimal@1",
        "name": "minimal",
        "axes": {"palette": "minimal", "type": "archivo-airy"},
    },
    {
        "id": "brutalist@2",
        "name": "brutalist",
        "axes": {"palette": "brutalist", "type": "archivo-caps-loud"},
    },
    {
        "id": "victorian@3",
        "name": "victorian",
        "axes": {"palette": "victorian", "type": "cormorant-gowun"},
    },
]


def test_parse_brief_string():
    ir = craft.parse_brief("따뜻하고 고급스러운 인쇄물. 금지: 네온")
    assert "따뜻" in ir["tones"] and "고급" in ir["tones"]
    assert ir["media"] == "print"
    assert any("네온" in item for item in ir["must_not"])


def test_parse_brief_dict_fills_blanks():
    ir = craft.parse_brief({"purpose": "랜딩", "tones": ["차분"]})
    assert ir["tones"] == ["차분"]
    assert ir["media"] == "web"
    assert ir["must_not"] == []


def test_plan_variants_scores_tones():
    ir = craft.parse_brief("미니멀한 화면")
    variants = craft.plan_variants(
        ir, STYLES, limit=1, breakpoints=("mobile",), themes=("light",)
    )
    assert len(variants) == 1
    assert variants[0]["style"] == "minimal"
    assert variants[0]["id"] == "minimal-mobile-light"


def test_plan_variants_falls_back_when_no_match():
    ir = {"purpose": "우주", "tones": ["없는톤"], "media": "web"}
    variants = craft.plan_variants(ir, STYLES, limit=2)
    assert len(variants) == 2
    assert all(v["breakpoint"] == "desktop" for v in variants)


def test_rank_candidates_orders_and_penalizes():
    rows = craft.rank_candidates(
        [
            {"id": "a", "verdict": "pass", "findings": []},
            {
                "id": "b",
                "verdict": "warn",
                "findings": [{"rule": "text.overflow", "severity": "warn"}],
            },
            {
                "id": "c",
                "verdict": "fail",
                "findings": [{"rule": "block.overlap", "severity": "fail"}],
            },
        ]
    )
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert rows[0]["score"] == 100 and rows[1]["score"] == 95 and rows[2]["score"] == 75


def test_pick_excludes_failures():
    picked = craft.pick(
        [
            {"id": "a", "verdict": "pass", "findings": []},
            {
                "id": "c",
                "verdict": "fail",
                "findings": [{"rule": "block.overlap", "severity": "fail"}],
            },
        ]
    )
    assert [row["id"] for row in picked] == ["a"]


@needs_assets
def test_registry_is_usable():
    styles = craft.load_registry(ASSETS)
    assert len(styles) > 10
    assert all(style["id"].count("@") == 1 for style in styles)


@needs_assets
def test_minimal_brief_picks_brand_neutral_styles():
    styles = craft.load_registry(ASSETS)
    ir = craft.parse_brief("미니멀하고 차분한 랜딩")
    names = {v["style"] for v in craft.plan_variants(ir, styles, limit=3)}
    assert names & {"minimal", "swiss", "mint-swiss", "ocean-swiss", "forest-paper"}


def test_graph_compiles_with_interrupt():
    pytest.importorskip("langgraph")
    from graphs.asset_craft_graph import build_graph

    graph = build_graph()
    assert graph is not None
