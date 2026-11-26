"""플랫폼 프리셋·엔트리 보드 단위 테스트 (GRAPH-25).

전부 오프라인이다. 렌더 변환만 monkeypatch로 대체하고, 보드는 Pillow로 실제
1920×1080 PNG를 만든다(외부 프로세스·네트워크 없음).
"""

import json
from pathlib import Path

from graphs import entry_pack_graph as entry
from graphs.profiles import board as board_module
from graphs.profiles import presets
from PIL import Image


def _fake_convert(svg, out, width, max_height=3000):
    Image.new("RGB", (600, 800), (245, 242, 236)).save(out, "PNG")


def _fake_jpg(png, out, quality=90):
    Path(out).write_bytes(b"jpg")


def _pack(tmp_path, monkeypatch, platform, **spec_overrides):
    monkeypatch.setattr(entry, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(entry, "_convert_png", _fake_convert)
    monkeypatch.setattr(entry, "_to_jpg", _fake_jpg)
    spec = {"platform": platform, "skip_llm": True, **spec_overrides}
    variant_id = "style-1-desktop-light"
    state = {
        "run_id": f"entry-{platform}",
        "brief": "차분한 티 브랜드 랜딩",
        "spec": spec,
        "renders": [
            {
                "id": variant_id,
                "svg": str(tmp_path / "a.svg"),
                "style": "style-1",
                "theme": "light",
                "direction": "차분한 여백",
            }
        ],
        "options": [
            {
                "id": variant_id,
                "direction": "차분한 여백",
                "style": "style-1",
                "theme": "light",
            }
        ],
        "selection": [variant_id],
        "directions": [
            {
                "title": "차분한 여백",
                "description": "여백을 살린 편집 구성",
                "motifs": ["얇은 선"],
            }
        ],
    }
    result = entry.pack_node(state)
    pack_dir = Path(result["entry"]["dir"])
    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    return pack_dir, meta


# ---------------------------------------------------------------------------
# 프리셋 해석
# ---------------------------------------------------------------------------


def test_presets_table_covers_required_platforms():
    assert {"99designs", "loudsourcing", "generic"} <= set(presets.PRESETS)
    for name, preset in presets.PRESETS.items():
        assert preset.id == name
        assert preset.image_width > 0 and preset.max_height > 0
        assert set(preset.formats) <= set(presets.KNOWN_FORMATS)
        assert 1 <= preset.jpg_quality <= 100
        assert isinstance(preset.board, bool)
        assert preset.naming


def test_spec_values_override_preset_and_missing_keys_are_filled():
    merged, preset, warnings = presets.apply(
        {
            "platform": "loudsourcing",
            "image_width": 1234,
            "formats": ["png"],
            "board": False,
        }
    )
    assert warnings == []
    assert preset.id == "loudsourcing"
    # spec이 프리셋보다 우선한다.
    assert merged["image_width"] == 1234
    assert merged["formats"] == ["png"]
    assert merged["board"] is False
    # 비어 있는 키만 프리셋 값으로 채운다.
    assert merged["max_height"] == preset.max_height
    assert merged["jpg_quality"] == preset.jpg_quality
    assert merged["naming"] == preset.naming
    assert merged["preset"] == "loudsourcing"


def test_spec_without_platform_is_untouched():
    original = {"image_width": 800, "profile": "web"}
    merged, preset, warnings = presets.apply(original)
    assert merged == original
    assert preset.id == "generic"
    assert warnings == []


def test_unknown_platform_falls_back_to_generic_with_warning():
    preset, warnings = presets.resolve("figma")
    assert preset.id == "generic"
    assert warnings and "generic" in warnings[0]

    # spec_node도 조용히 넘어가지 않고 경고를 spec에 남긴다.
    result = entry.spec_node(
        {"brief": "차분한 랜딩", "spec": {"platform": "figma", "skip_llm": True}}
    )
    assert result["spec"]["preset"] == "generic"
    assert result["spec"]["preset_warnings"]

    # pack meta에도 같은 경고가 남는다.
    merged, _, _ = presets.apply({"platform": "figma"})
    assert merged["preset"] == "generic"
    assert merged["preset_warnings"]


def test_filename_pattern_is_safe_and_supports_index():
    assert presets.filename("{id}", "style-1", 1) == "style-1"
    assert (
        presets.filename("99designs-{index:02d}-{id}", "style-1", 1)
        == "99designs-01-style-1"
    )
    # 경로 구분자·공백은 파일명으로 새지 않는다.
    assert presets.filename("../{id} x", "a", 1) == "a-x"


# ---------------------------------------------------------------------------
# 엔트리 보드
# ---------------------------------------------------------------------------


def test_board_is_written_with_fixed_dimensions(tmp_path, monkeypatch):
    pack_dir, meta = _pack(tmp_path, monkeypatch, "99designs")

    board_path = pack_dir / "board.png"
    assert board_path.is_file()
    assert Image.open(board_path).size == (1920, 1080)
    assert board_path.stat().st_size > 1000

    row = meta["board"]
    assert row["width"] == 1920 and row["height"] == 1080
    assert row["bytes"] == board_path.stat().st_size
    assert row["direction"] == "차분한 여백"
    assert "font_fallback" in row

    entry_md = (pack_dir / "entry.md").read_text(encoding="utf-8")
    assert "board.png" in entry_md
    assert "1920×1080" in entry_md

    # 파일 이름도 플랫폼 패턴을 따른다(generic은 기존과 같은 id.png).
    assert (pack_dir / "99designs-01-style-1-desktop-light.png").is_file()


def test_board_is_skipped_when_preset_board_is_false(tmp_path, monkeypatch):
    pack_dir, meta = _pack(tmp_path, monkeypatch, "generic")

    assert meta["board"] is None
    assert not (pack_dir / "board.png").exists()
    assert "board.png" not in (pack_dir / "entry.md").read_text(encoding="utf-8")
    # generic은 기존 이름 규칙 그대로다.
    assert (pack_dir / "style-1-desktop-light.png").is_file()


def test_board_font_fallback_is_recorded(tmp_path, monkeypatch):
    image = tmp_path / "entry.png"
    Image.new("RGB", (400, 300), (245, 242, 236)).save(image, "PNG")
    monkeypatch.setattr(board_module, "BOLD_FONTS", ())
    monkeypatch.setattr(board_module, "REGULAR_FONTS", ())

    result = board_module.compose(
        image=image,
        out=tmp_path / "board.png",
        platform="generic",
        direction="차분한 여백",
        style="style-1",
        theme="light",
        description="여백을 살린 편집 구성",
        entry_id="style-1-desktop-light",
    )
    assert result["font_fallback"] is True
    assert result["font"] == "pillow-default"
    assert Image.open(tmp_path / "board.png").size == (1920, 1080)


def test_board_is_deterministic_for_same_input(tmp_path):
    image = tmp_path / "entry.png"
    Image.new("RGB", (640, 480), (245, 242, 236)).save(image, "PNG")

    kwargs = dict(
        image=image,
        platform="loudsourcing",
        direction="차분한 여백",
        style="style-1",
        theme="light",
        description="같은 입력이면 같은 보드",
        entry_id="style-1-desktop-light",
    )
    first = board_module.compose(out=tmp_path / "board-a.png", **kwargs)
    second = board_module.compose(out=tmp_path / "board-b.png", **kwargs)
    assert first["bytes"] == second["bytes"]
    assert (tmp_path / "board-a.png").read_bytes() == (
        tmp_path / "board-b.png"
    ).read_bytes()
