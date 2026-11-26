"""T6 (GRAPH-40) 변주 다양성 · 상한 자동 산출 · 자산 반복 억제 · 콘택트시트 표기 테스트."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from graphs.profiles import contact_sheet
from graphs.profiles import diversity
from graphs.profiles import web as web_profile


def _make_style(
    idx: int,
    *,
    layout: str = "editorial",
    palette: str = "warm",
    ornament: str = "minimal",
    typ: str = "serif",
    asset: str = "",
):
    style = {
        "id": f"s{idx}",
        "name": f"style-{idx}",
        "axes": {
            "layout": layout,
            "palette": palette,
            "ornament": ornament,
            "type": typ,
        },
    }
    if asset:
        style["asset"] = asset
    return style


def test_resolve_styles_per_direction_scales_with_variant_cap():
    assert diversity.resolve_styles_per_direction(60, 3, combos_per_style=2) == 10
    assert diversity.resolve_styles_per_direction(24, 3, combos_per_style=2) == 4
    assert diversity.resolve_styles_per_direction(60, 3, combos_per_style=2, explicit=2) == 2


def test_feature_distance_distinguishes_near_duplicates():
    a = diversity.variant_feature_vector(
        {"style": "s1", "theme": "light", "breakpoint": "desktop", "asset": "seal-a"},
        _make_style(1, layout="grid", palette="orange", ornament="line", typ="sans"),
    )
    b_dup = diversity.variant_feature_vector(
        {"style": "s2", "theme": "light", "breakpoint": "desktop", "asset": "seal-a"},
        _make_style(2, layout="grid", palette="orange", ornament="line", typ="sans"),
    )
    c_diff = diversity.variant_feature_vector(
        {"style": "s3", "theme": "dark", "breakpoint": "mobile", "asset": "photo-b"},
        _make_style(3, layout="split", palette="forest", ornament="dense", typ="serif"),
    )
    assert diversity.feature_distance(a, b_dup) == 0.0
    assert diversity.feature_distance(a, c_diff) >= 0.75


def test_select_diverse_variants_replaces_duplicates_and_caps_assets():
    # 앞 3개는 같은 레이아웃·색·자산(seal-orange)인 중복 후보, 뒤 3개는 서로 다른 후보
    styles = [
        _make_style(1, layout="grid", palette="orange", ornament="line", asset="seal-orange"),
        _make_style(2, layout="grid", palette="orange", ornament="line", asset="seal-orange"),
        _make_style(3, layout="grid", palette="orange", ornament="line", asset="seal-orange"),
        _make_style(4, layout="split", palette="forest", ornament="none", asset="photo-leaf"),
        _make_style(5, layout="poster", palette="mono", ornament="frame", asset="type-block"),
        _make_style(6, layout="card", palette="navy", ornament="badge", asset="seal-blue"),
    ]
    candidates = [
        {
            "id": f"style-{s['id'][1:]}-desktop-light",
            "style": s["name"],
            "style_id": s["id"],
            "breakpoint": "desktop",
            "theme": "light",
        }
        for s in styles
    ]
    lookup = {s["id"]: s for s in styles}

    picked = diversity.select_diverse_variants(
        candidates,
        cap=4,
        style_lookup=lookup,
        min_distance=0.25,
        max_asset_reuse=1,
    )
    assert len(picked) == 4
    picked_styles = [p["style"] for p in picked]
    # 중복인 style-2, style-3 대신 서로 다른 style-4, style-5, style-6이 선택돼야 한다.
    assert picked_styles == ["style-1", "style-4", "style-5", "style-6"]


def test_select_diverse_variants_rotates_asset_pool_when_over_cap():
    styles = [
        _make_style(1, layout="grid", palette="warm", asset="seal-orange"),
        _make_style(2, layout="split", palette="cool", asset="seal-orange"),
        _make_style(3, layout="poster", palette="mono", asset="seal-orange"),
    ]
    candidates = [
        {
            "id": f"style-{i}-desktop-light",
            "style": f"style-{i}",
            "style_id": f"s{i}",
            "breakpoint": "desktop",
            "theme": "light",
        }
        for i in (1, 2, 3)
    ]
    lookup = {s["id"]: s for s in styles}
    picked = diversity.select_diverse_variants(
        candidates,
        cap=3,
        style_lookup=lookup,
        min_distance=0.25,
        max_asset_reuse=1,
        asset_pool=["seal-orange", "photo-tea", "line-art"],
    )
    assets = [p["asset"] for p in picked]
    assert Counter(assets)["seal-orange"] <= 1
    assert len(set(assets)) == 3


def test_web_plan_auto_scales_styles_per_direction_from_variant_cap():
    layouts = ["grid", "split", "poster", "editorial", "card", "column"]
    palettes = ["warm", "cool", "mono", "forest"]
    styles = [
        _make_style(
            i,
            layout=layouts[i % len(layouts)],
            palette=palettes[i % len(palettes)],
            ornament=f"orn-{i % 3}",
            asset=f"asset-{i}",
        )
        for i in range(24)
    ]
    directions = [
        {"title": "A", "description": "a", "motifs": []},
        {"title": "B", "description": "b", "motifs": []},
        {"title": "C", "description": "c", "motifs": []},
    ]
    variants = web_profile.WebProfile().plan(
        {"directions": directions, "tones": ["편집"]},
        {
            "seed": "t6-seed",
            "variant_cap": 18,
            "breakpoints": ["desktop"],
            "themes": ["light"],
        },
        styles,
    )
    assert len(variants) == 18


def test_diversity_summary_counts_duplicate_groups_and_contact_sheet_records_it(tmp_path: Path):
    items = [
        {"id": "v1", "style": "s1", "theme": "light", "axes": {"layout": "grid", "palette": "warm"}, "asset": "a1"},
        {"id": "v2", "style": "s2", "theme": "light", "axes": {"layout": "grid", "palette": "warm"}, "asset": "a1"},
        {"id": "v3", "style": "s3", "theme": "dark", "axes": {"layout": "split", "palette": "mono"}, "asset": "a2"},
    ]
    summary = diversity.diversity_summary(items)
    assert summary["total"] == 3
    assert summary["unique_groups"] == 2
    assert summary["duplicate_groups"] == 1
    assert summary["diversity_score"] == 67

    row = contact_sheet.compose(
        items,
        out=tmp_path / "sheet.png",
        run_id="t6-run",
        seed="t6-seed",
        brief="다양성 테스트",
    )
    assert row["diversity"]["duplicate_groups"] == 1
    assert row["diversity"]["diversity_score"] == 67
