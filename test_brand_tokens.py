"""브랜드 토큰 로더 검증 — 색이 코드가 아니라 SSOT에서 오는지."""

import hashlib
import os
import re
import sys

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import brand  # noqa: E402

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

needs_assets = pytest.mark.skipif(
    not brand.available(), reason="sonagi-assets SSOT가 없는 환경(SONAGI_ASSETS_DIR)"
)


def test_mix_is_deterministic_without_assets():
    assert brand.mix("#000000", "#ffffff", 0.5) == "#808080"
    assert brand.tint("#000000", 0.25) == "#404040"
    assert brand.shade("#ffffff", 0.5, onto="#000000") == "#808080"


def test_missing_source_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("SONAGI_ASSETS_DIR", str(tmp_path))
    with pytest.raises(brand.BrandSourceMissing) as exc:
        brand.tokens()
    assert "SONAGI_ASSETS_DIR" in str(exc.value)


@needs_assets
def test_palette_roles_are_hex():
    palette = brand.design_palette()
    assert len(palette) == 12
    for role, value in palette.items():
        assert HEX.match(value), f"{role} = {value}"


@needs_assets
def test_accent_comes_from_tokens():
    palette = brand.design_palette("architecture")
    base = brand.semantic("architecture")
    assert palette["ice_blue"].lower() == base["accent"].lower()
    assert palette["bg_obsidian"].lower() == base["background"].lower()
    assert palette["gold_dandelion"].lower() == base["accent-strong"].lower()


@needs_assets
def test_tokens_change_flows_into_palette(monkeypatch, tmp_path):
    """토큰 사본의 accent를 바꾸면 팔레트도 따라온다(하드코딩이 아니라는 증거)."""
    import json

    (tmp_path / "tokens").mkdir()
    data = brand.tokens()
    data["architecture"]["accent"] = "#123456"
    (tmp_path / "tokens/semantic.json").write_text(json.dumps(data))
    monkeypatch.setenv("SONAGI_ASSETS_DIR", str(tmp_path))
    palette = brand.design_palette("architecture")
    assert palette["ice_blue"] == "#123456"
    assert palette["cyan_soft"] != palette["ice_blue"]


@needs_assets
def test_contract_matches_token_file():
    path = brand.assets_dir() / "tokens/semantic.json"
    expected = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    assert brand.contract()["tokens_version"] == expected


@needs_assets
def test_style_lookup_by_name_and_id():
    entry = brand.registry()["styles"][0]
    assert brand.style(entry["name"])["id"] == entry["id"]
    assert brand.style(entry["id"])["name"] == entry["name"]
    with pytest.raises(KeyError):
        brand.style("없는-스타일")


def test_design_ops_graph_has_no_hex_literals():
    """그래프 소스에 hex 리터럴이 남아 있지 않은지."""
    root = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(root, "graphs/design_ops_graph.py")).read()
    assert not re.search(r'"#[0-9a-fA-F]{6}"', source)
