"""콘택트 시트·변주 상한·탐색 스크립트 검증 (GRAPH-32).

전부 오프라인이다. PNG는 Pillow로 만들고, 그래프 렌더·변환은 스텁으로 바꾼다.
"""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from graphs.profiles import base
from graphs.profiles import contact_sheet
from graphs.profiles import web as web_profile

ROOT = Path(__file__).resolve().parent


def _items(tmp_path, count):
    """작은 PNG를 가진 렌더 dict 목록. id는 실제 변주 id 모양을 흉내 낸다."""
    items = []
    for index in range(count):
        png = tmp_path / f"thumb-{index}.png"
        Image.new("RGB", (120, 160), (40 + index % 200, 60, 70)).save(png, "PNG")
        items.append(
            {
                "id": f"style-{index}-desktop-light",
                "png": str(png),
                "style": f"style-{index}",
                "theme": "light",
                "direction": "차분한 여백",
            }
        )
    return items


@pytest.mark.parametrize(
    ("count", "rows", "height"),
    [
        (1, 1, 700),
        (6, 1, 700),
        (24, 4, 2032),
        (61, 11, 5140),
    ],
)
def test_sheet_dimensions_grow_by_rows(tmp_path, count, rows, height):
    out = tmp_path / "sheet.png"
    row = contact_sheet.compose(
        _items(tmp_path, count),
        out=out,
        run_id="entry-abcd1234",
        seed="abcd1234",
        brief="비 오는 날의 차\n둘째 줄은 쓰지 않는다",
    )

    assert row["columns"] == 6
    assert row["rows"] == rows
    assert row["count"] == count
    assert (row["width"], row["height"]) == (2016, height)
    assert row["rendered"] == count
    assert Image.open(out).size == (2016, height)


def test_sheet_is_byte_deterministic(tmp_path):
    items = _items(tmp_path, 7)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"

    contact_sheet.compose(items, out=first, run_id="r1", seed="s1", brief="브리프")
    contact_sheet.compose(items, out=second, run_id="r1", seed="s1", brief="브리프")

    assert first.read_bytes() == second.read_bytes()


def test_title_block_uses_first_brief_line_and_meta(tmp_path):
    row = contact_sheet.compose(
        _items(tmp_path, 2),
        out=tmp_path / "sheet.png",
        run_id="entry-1234",
        seed="seed-9",
        brief="첫 줄만 남는다\n두 번째 줄",
    )
    assert row["run_id"] == "entry-1234"
    assert row["seed"] == "seed-9"
    image = Image.open(row["path"])
    assert image.size == (row["width"], row["height"])


def test_label_truncation_caps_length():
    assert contact_sheet.truncate_text("짧다", 10) == "짧다"
    truncated = contact_sheet.truncate_text("가" * 80, 20)
    assert len(truncated) == 20
    assert truncated.endswith("…")
    assert contact_sheet.truncate_text("   여러   공백  ", 30) == "여러 공백"
    assert contact_sheet.truncate_text("아무거나", 0) == ""


def test_long_id_and_style_are_truncated_in_labels(tmp_path):
    png = tmp_path / "one.png"
    Image.new("RGB", (30, 40), (10, 20, 30)).save(png, "PNG")
    row = contact_sheet.compose(
        [
            {
                "id": "style-" + "x" * 90,
                "png": str(png),
                "style": "s" * 80,
                "theme": "dark",
            }
        ],
        out=tmp_path / "sheet.png",
    )
    label = row["labels"][0]
    assert len(label["id"]) <= contact_sheet.ID_LABEL_LIMIT
    assert label["id"].endswith("…")
    assert len(label["meta"]) <= contact_sheet.META_LABEL_LIMIT
    assert "…" in label["meta"]


def test_missing_png_gets_placeholder_not_crash(tmp_path):
    row = contact_sheet.compose(
        [{"id": "v1", "png": str(tmp_path / "nope.png"), "style": "s"}],
        out=tmp_path / "sheet.png",
        run_id="r",
    )
    assert row["count"] == 1
    assert row["rendered"] == 0
    assert row["missing"] == 1


# ---------------------------------------------------------------------------
# 변주 상한 (24 기본, 120 상한)
# ---------------------------------------------------------------------------


def test_variant_cap_defaults_and_clamps():
    assert web_profile.clamp_variant_cap(None) == (24, None)
    assert web_profile.clamp_variant_cap(24) == (24, None)
    assert web_profile.clamp_variant_cap(120) == (120, None)

    cap, warning = web_profile.clamp_variant_cap(500)
    assert cap == 120
    assert warning and "120" in warning

    cap, warning = web_profile.clamp_variant_cap("nope")
    assert cap == 24
    assert warning and "24" in warning


def test_web_validate_clamps_and_warns(capsys):
    spec = {"variant_cap": 999}
    web_profile.WebProfile().validate(spec)

    assert spec["variant_cap"] == 120
    assert spec["warnings"]
    assert "120" in capsys.readouterr().err


def test_web_plan_uses_clamped_cap():
    styles = [
        {"id": f"s{i}", "name": f"style-{i}", "axes": {"palette": "editorial"}}
        for i in range(20)
    ]
    profile = web_profile.WebProfile()
    variants = profile.plan(
        {"directions": [{"title": "A", "description": "a", "motifs": []}]},
        {
            "seed": "s",
            "styles_per_direction": 20,
            "breakpoints": ["desktop"],
            "themes": ["light", "dark"],
            "variant_cap": 500,
        },
        styles,
    )
    assert len(variants) == 40  # 20 styles × 2 themes, 상한 120 이내


# ---------------------------------------------------------------------------
# web profile 연결 — 모든 렌더 뒤 contact-sheet.png
# ---------------------------------------------------------------------------


class _StubContext(base.ProfileContext):
    def __init__(self, run_dir):
        super().__init__(run_dir=Path(run_dir))

    def convert(self, svg, out, width, max_height=3000):
        Image.new("RGB", (60, 80), (245, 244, 240)).save(out, "PNG")


def test_web_contact_sheet_writes_thumbs_and_sheet(tmp_path):
    renders = _items(tmp_path, 3)
    for render in renders:
        svg = tmp_path / f"{render['id']}.svg"
        svg.write_text('<svg width="1440" height="1924"></svg>', encoding="utf-8")
        render["svg"] = str(svg)

    run_dir = tmp_path / "run"
    row = web_profile.WebProfile().contact_sheet(
        renders,
        {"seed": "seed-1"},
        {"run_id": "entry-run", "brief": "브리프 첫 줄"},
        _StubContext(run_dir),
    )

    assert Path(row["path"]) == run_dir / "contact-sheet.png"
    assert Path(row["path"]).is_file()
    assert row["count"] == 3
    assert len(list((run_dir / "thumbs").glob("*.png"))) == 3


def test_render_node_sets_contact_sheet(monkeypatch, tmp_path):
    from graphs import entry_pack_graph as mod

    monkeypatch.setattr(mod, "OUT_ROOT", tmp_path)

    def fake_run(args, timeout=300):
        argv = [str(a) for a in args]
        script = Path(argv[1]).name
        if script not in ("import_content.py", "render_recipe.py"):
            return ""  # vectorcraft convert 스텁 — PNG를 만들지 않는다
        out = Path(argv[argv.index("--out") + 1])
        if script == "import_content.py":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"items": []}', encoding="utf-8")
        else:
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{out.name}.svg").write_text(
                '<svg width="1440" height="1924"></svg>', encoding="utf-8"
            )
        return ""

    monkeypatch.setattr(mod, "_run", fake_run)
    state = {
        "brief": "차분한 티 브랜드 랜딩",
        "run_id": "entry-sheet",
        "spec": {
            "platform": "99designs",
            "seed": "sheet-seed",
            "variant_cap": 3,
            "styles_per_direction": 2,
            "breakpoints": ["desktop"],
            "themes": ["light"],
        },
        "directions": [{"title": "A", "description": "a", "motifs": ["m"]}],
        "variants": [
            {
                "id": f"v{i}",
                "style": f"style-{i}",
                "breakpoint": "desktop",
                "theme": "light",
                "direction": "A",
            }
            for i in range(3)
        ],
    }

    result = mod.render_node(state)

    assert result["contact_sheet"] == str(
        tmp_path / "entry-sheet" / "contact-sheet.png"
    )
    assert Path(result["contact_sheet"]).is_file()
    assert result["timings"]["render_seconds"] >= 0
    assert result["timings"]["contact_sheet_seconds"] >= 0


# ---------------------------------------------------------------------------
# scripts/explore.sh — dry-run은 서버 없이 계획만 낸다
# ---------------------------------------------------------------------------


def _write_spec(tmp_path, **spec):
    payload = {
        "brief": "비 오는 날의 차 — 차분한 티 브랜드",
        "spec": {"platform": "99designs", "variant_cap": 6, **spec},
    }
    path = tmp_path / "web.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_explore_dry_run_derives_seeds_without_server(tmp_path):
    spec = _write_spec(tmp_path, seed="rain")
    out_dir = tmp_path / "sheets"

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/explore.sh"),
            "--dry-run",
            "--count",
            "2",
            "--out",
            str(out_dir),
            str(spec),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("run entry_pack") == 2
    assert "seed=rain-1" in result.stdout
    assert "seed=rain-2" in result.stdout
    assert not out_dir.exists()


def test_explore_dry_run_accepts_explicit_seeds(tmp_path):
    spec = _write_spec(tmp_path)

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/explore.sh"),
            "--dry-run",
            "--seeds",
            "a,b",
            str(spec),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("run entry_pack") == 2
    assert "seed=a" in result.stdout
    assert "seed=b" in result.stdout


def test_explore_rejects_non_web_profile(tmp_path):
    spec = tmp_path / "print.json"
    spec.write_text(
        json.dumps({"brief": "포스터", "spec": {"profile": "print"}}), encoding="utf-8"
    )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/explore.sh"), "--dry-run", str(spec)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "web profile" in result.stderr


def test_explore_script_passes_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/explore.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
