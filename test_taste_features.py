"""취향 특징 추출(Graph-35) 테스트 — 네트워크 없음, 같은 입력은 같은 숫자."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from graphs.taste import features


FIXTURE_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200">
  <rect x="0" y="0" width="400" height="200" fill="#fbf9f5"/>
  <rect x="32" y="32" width="120" height="60" fill="#161616"/>
  <rect x="32" y="120" width="60" height="24" fill="#b4551f"/>
  <text x="32" y="190" font-size="28" fill="#161616">Earl Grey</text>
  <text x="32" y="60" font-size="14" fill="#7a7a7a">50 g</text>
</svg>
"""

FEATURE_KEYS = {
    "whitespace_ratio",
    "distinct_color_count",
    "max_contrast",
    "type_size_steps",
    "element_density",
    "alignment_edges",
    "source",
}


def write_svg(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.svg"
    path.write_text(FIXTURE_SVG, encoding="utf-8")
    return path


def test_svg_features_are_deterministic_and_documented(tmp_path):
    path = write_svg(tmp_path)
    first = features.extract_features(path)
    second = features.extract_features(path)

    assert first == second
    assert set(first) == FEATURE_KEYS
    assert first["source"] == "svg"
    assert 0.0 < first["whitespace_ratio"] < 1.0
    assert first["distinct_color_count"] >= 3  # 종이·검정·강조색
    assert first["type_size_steps"] == 2  # 28px, 14px
    assert first["element_density"] > 0
    assert first["alignment_edges"] >= 1  # 왼쪽 x=32에 세 요소가 맞춰져 있다
    # 글자색 #161616과 종이 #fbf9f5의 대비는 15:1을 넘는다.
    assert first["max_contrast"] > 15


def test_svg_features_do_not_count_full_canvas_rect_as_painted(tmp_path):
    path = write_svg(tmp_path)
    result = features.extract_features(path)
    # 배경을 내용으로 세면 여백이 0이 된다 — 실제로는 넓은 여백이 있다.
    assert result["whitespace_ratio"] > 0.5


def test_svg_features_survive_missing_background(tmp_path):
    path = tmp_path / "plain.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<circle cx="50" cy="50" r="20" fill="#000000"/></svg>',
        encoding="utf-8",
    )
    result = features.extract_features(path)
    assert result["whitespace_ratio"] > 0.5
    assert result["max_contrast"] > 15


def make_png(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.png"
    image = Image.new("RGB", (200, 100), (251, 249, 245))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 80, 60), fill=(22, 22, 22))
    draw.rectangle((20, 70, 60, 80), fill=(180, 85, 31))
    image.save(path)
    return path


def test_png_features_are_deterministic_and_pixel_based(tmp_path):
    path = make_png(tmp_path)
    first = features.extract_features(path)
    second = features.extract_features(path)

    assert first == second
    assert set(first) == FEATURE_KEYS
    assert first["source"] == "png"
    assert 0.5 < first["whitespace_ratio"] < 1.0
    assert first["distinct_color_count"] >= 2
    assert first["max_contrast"] > 10
    assert first["element_density"] > 0
    # 픽셀만으로 글자 크기 단계는 알 수 없다 — 0을 만들지 않고 None으로 둔다.
    assert first["type_size_steps"] is None


def test_unknown_extension_is_an_error_and_safe_wrapper_returns_empty(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not an image", encoding="utf-8")
    with pytest.raises(features.FeatureError):
        features.extract_features(path)
    assert features.safe_extract_features(path) == {}


def test_safe_wrapper_swallows_broken_files(tmp_path):
    path = tmp_path / "broken.svg"
    path.write_text("<svg", encoding="utf-8")
    assert features.safe_extract_features(path) == {}
