"""T4 (GRAPH-38) 자산 취향 원장 + 월간 성장 갤러리 테스트."""

from __future__ import annotations

import json
from pathlib import Path

from graphs.taste import asset_prefs
from graphs.taste import gallery


def _decision(
    idx: int,
    *,
    chosen_id: str = "opt-a",
    reason_tags: list[str] | None = None,
    reason: str = "시인성이 좋음",
):
    return {
        "thread_id": f"t-{idx}",
        "brief": "민들레 브랜드 인장",
        "selected": [chosen_id],
        "reason_tags": reason_tags if reason_tags is not None else ["시인성"],
        "reason": reason,
        "ts": "2026-11-26T00:00:00Z",
        "options": [
            {
                "id": "opt-a",
                "style": "forest-paper",
                "png": f"/tmp/chosen-{idx}.png",
                "axes": {"palette": "warm"},
            },
            {
                "id": "opt-b",
                "style": "cold-grid",
                "png": f"/tmp/rejected-{idx}.png",
                "axes": {"palette": "cool"},
            },
        ],
    }


def test_asset_prefs_accumulates_preferred_and_demoted_without_touching_catalog(tmp_path: Path):
    catalog_path = tmp_path / "catalog.json"
    catalog_raw = '{"styles": ["forest-paper", "cold-grid"]}\n'
    catalog_path.write_text(catalog_raw, encoding="utf-8")

    records = [_decision(i) for i in range(3)]
    prefs = asset_prefs.build_asset_prefs(records)
    saved = asset_prefs.save_asset_prefs(prefs, tmp_path / "asset_prefs.json")

    assert saved.is_file()
    assert catalog_path.read_text(encoding="utf-8") == catalog_raw

    forest = prefs["assets"]["style:forest-paper"]
    cold = prefs["assets"]["style:cold-grid"]
    assert forest["proposed"] == 3
    assert forest["chosen"] == 3
    assert forest["rejected"] == 0
    assert forest["status"] == "preferred"
    assert forest["reason_tags"]["시인성"] == 3
    assert "민들레" in forest["context_keywords"]

    assert cold["proposed"] == 3
    assert cold["chosen"] == 0
    assert cold["rejected"] == 3
    assert cold["status"] == "demoted"

    assert asset_prefs.preference_boost("style:forest-paper", prefs) == 0.2
    assert asset_prefs.preference_boost("style:cold-grid", prefs) == -0.2


def test_monthly_gallery_renders_side_by_side_html_and_png(tmp_path: Path):
    records = [_decision(1), _decision(2, reason="여백이 차분함", reason_tags=["여백"])]
    prefs = asset_prefs.build_asset_prefs(records)

    out = gallery.build_monthly_gallery(
        records, out_dir=tmp_path, month="2026-11", prefs=prefs
    )
    html_file = Path(out["html_path"])
    png_file = Path(out["png_path"])

    assert out["pairs_count"] == 2
    assert html_file.is_file()
    assert png_file.is_file()

    html_text = html_file.read_text(encoding="utf-8")
    assert "선택 (Chosen)" in html_text
    assert "거절 (Rejected)" in html_text
    assert "시인성이 좋음" in html_text
    assert "여백이 차분함" in html_text
