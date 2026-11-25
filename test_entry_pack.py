"""EntryPackGraph 단위 테스트 — 렌더·린트·변환은 monkeypatch로 대체한다."""

import json
from pathlib import Path

import pytest

from graphs import entry_pack_graph as mod


def test_graph_compiles():
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import MemorySaver

    g = mod.build_entry_pack_graph(checkpointer=MemorySaver())
    assert g is not None


def test_spec_defaults_and_seed_stability():
    state = {"brief": "핸드메이드 티 브랜드 랜딩 — 차분하고 고급스러운, 웹"}
    first = mod.spec_node(state)
    second = mod.spec_node(state)
    assert first["spec"]["seed"] == second["spec"]["seed"]
    assert first["spec"]["themes"] == ["light", "dark"]
    assert first["spec"]["breakpoints"] == ["desktop"]
    assert first["ir"]["media"] == "web"


def test_spec_print_defaults():
    result = mod.spec_node({"brief": "포스터 인쇄 — 대담한"})
    assert result["spec"]["media"] == "print"
    assert result["spec"]["breakpoints"] == ["mobile"]
    assert result["spec"]["themes"] == ["light"]


def test_variants_are_reproducible_with_seed(monkeypatch):
    styles = [
        {"id": f"s{i}", "name": f"style-{i}", "axes": {"palette": "editorial"}}
        for i in range(6)
    ]
    monkeypatch.setattr(mod.craft, "load_registry", lambda _dir: styles)
    state = {
        "brief": "차분한 편집 디자인",
        "spec": {
            "seed": "abc123",
            "styles_per_direction": 2,
            "breakpoints": ["desktop"],
            "themes": ["light", "dark"],
            "variant_cap": 24,
        },
        "directions": [{"title": "A", "description": "a", "motifs": ["m"]}],
    }
    first = mod.variants_node(state)["variants"]
    second = mod.variants_node(state)["variants"]
    assert [v["id"] for v in first] == [v["id"] for v in second]
    assert {v["theme"] for v in first} == {"light", "dark"}


def test_variant_cap_is_respected(monkeypatch):
    styles = [
        {"id": f"s{i}", "name": f"style-{i}", "axes": {"palette": "editorial"}}
        for i in range(10)
    ]
    monkeypatch.setattr(mod.craft, "load_registry", lambda _dir: styles)
    state = {
        "brief": "차분한",
        "spec": {
            "seed": "s",
            "styles_per_direction": 5,
            "breakpoints": ["desktop"],
            "themes": ["light", "dark"],
            "variant_cap": 4,
        },
        "directions": [{"title": "A", "description": "a", "motifs": []}],
    }
    variants = mod.variants_node(state)["variants"]
    assert len(variants) == 4


def test_knowledge_skips_and_survives_failure(monkeypatch):
    skipped = mod.knowledge_node({"brief": "x", "spec": {"skip_knowledge": True}})
    assert skipped["knowledge"]["skipped"] is True

    def boom(_query, limit=5):
        raise RuntimeError("offline")

    monkeypatch.setattr(mod, "_searxng", boom)
    monkeypatch.setattr(mod, "_wiki_notes", lambda *a, **k: [])
    result = mod.knowledge_node({"brief": "차분한 랜딩", "spec": {}})
    assert result["status"] == "knowledge"
    assert any("searxng" in e for e in result["knowledge"]["errors"])


def test_direction_fallback_when_llm_skipped():
    result = mod.direction_node(
        {
            "brief": "차분한",
            "spec": {"skip_llm": True},
            "ir": {"tones": ["차분"], "media": "web", "purpose": "x"},
        }
    )
    assert result["status"] == "directions_fallback"
    assert len(result["directions"]) == 1


def test_gate_excludes_fail_and_limits():
    critiques = [
        {"id": "a", "verdict": "pass", "findings": [], "plan_score": 1},
        {"id": "b", "verdict": "fail", "findings": [{"severity": "fail", "rule": "x"}]},
        {
            "id": "c",
            "verdict": "pass",
            "findings": [{"severity": "warn", "rule": "y"}],
            "plan_score": 0,
        },
    ]
    result = mod.gate_node(
        {
            "critiques": critiques,
            "renders": [
                {"id": "a", "svg": "/tmp/a.svg"},
                {"id": "c", "svg": "/tmp/c.svg"},
            ],
            "spec": {"top_n": 9},
        }
    )
    ids = [o["id"] for o in result["options"]]
    assert "b" not in ids
    assert ids[0] == "a"


def test_hitl_pauses_then_auto_selects():
    pending = mod.hitl_node({"options": [{"id": "a"}, {"id": "b"}]})
    assert pending["status"] == "awaiting_selection"

    resumed = mod.hitl_node(
        {
            "options": [{"id": "a", "direction": "A"}, {"id": "b", "direction": "B"}],
            "approval_status": "approved",
        }
    )
    assert resumed["status"] == "selected"
    assert resumed["selection"] == ["a", "b"]


def test_pack_writes_png_jpg_meta_and_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "OUT_ROOT", tmp_path)

    def fake_convert(svg, out, width, max_height=3000):
        from PIL import Image

        Image.new("RGB", (100, 140), (250, 249, 245)).save(out, "PNG")

    def fake_jpg(png, out, quality=90):
        Path(out).write_bytes(b"jpg")

    monkeypatch.setattr(mod, "_convert_png", fake_convert)
    monkeypatch.setattr(mod, "_to_jpg", fake_jpg)

    state = {
        "run_id": "entry-test",
        "brief": "차분한 티 브랜드",
        "spec": {"platform": "99designs", "image_width": 2000, "skip_llm": True},
        "renders": [
            {
                "id": "style-1-desktop-light",
                "svg": str(tmp_path / "a.svg"),
                "style": "style-1",
                "theme": "light",
                "direction": "A",
            }
        ],
        "options": [
            {
                "id": "style-1-desktop-light",
                "direction": "A",
                "style": "style-1",
                "theme": "light",
            }
        ],
        "selection": ["style-1-desktop-light"],
        "directions": [{"title": "A", "description": "a", "motifs": ["모티프"]}],
    }
    result = mod.pack_node(state)
    pack_dir = Path(result["entry"]["dir"])
    assert (pack_dir / "meta.json").is_file()
    assert (pack_dir / "entry.md").is_file()
    meta = json.loads((pack_dir / "meta.json").read_text())
    assert meta["selected_total"] == 1
    assert meta["platform"] == "99designs"
    assert result["entry"]["items"][0]["jpg_size"] == 3


def test_mockup_creates_card_and_poster(tmp_path, monkeypatch):
    from PIL import Image

    page = tmp_path / "page.png"
    Image.new("RGB", (120, 160), (30, 30, 30)).save(page, "PNG")
    monkeypatch.setattr(mod, "OUT_ROOT", tmp_path / "root")
    entry_dir = tmp_path / "root" / "entry-test" / "pack"
    entry_dir.mkdir(parents=True)

    result = mod.mockup_node(
        {
            "spec": {"mockups": 1},
            "entry": {
                "dir": str(entry_dir),
                "items": [{"id": "v1", "png": str(page), "direction": "A"}],
            },
        }
    )
    assert result["status"] == "mocked"
    card = Path(result["mockups"][0]["card"])
    poster = Path(result["mockups"][0]["poster"])
    assert card.is_file() and poster.is_file()
    assert Image.open(card).size == (1400, 900)


def test_svg_size_parsing(tmp_path):
    svg = tmp_path / "a.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="1924"></svg>',
        encoding="utf-8",
    )
    assert mod._svg_size(svg) == (1440.0, 1924.0)
    assert mod._svg_width(svg) == 1440.0


def test_scale_caps_height(tmp_path, monkeypatch):
    """긴 페이지는 높이 상한에 맞춰 축소한다(폭 2000 고정이 아님)."""
    svg = tmp_path / "page.svg"
    svg.write_text('<svg width="1440" height="6000"></svg>', encoding="utf-8")
    seen = {}

    def fake_run(args, timeout=300):
        seen["scale"] = float(args[args.index("--scale") + 1])
        Path(args[2]).write_bytes(b"png")
        return ""

    monkeypatch.setattr(mod, "_run", fake_run)
    mod._convert_png(svg, tmp_path / "out.png", 2000, 3000)
    assert seen["scale"] == 0.5


def test_directions_use_disjoint_styles(monkeypatch):
    """방향마다 스타일을 겹치지 않게 배분한다(변주 폭 확보)."""
    styles = [
        {"id": f"s{i}", "name": f"style-{i}", "axes": {"palette": "editorial"}}
        for i in range(6)
    ]
    monkeypatch.setattr(mod.craft, "load_registry", lambda _dir: styles)
    state = {
        "brief": "차분한",
        "spec": {
            "seed": "s",
            "styles_per_direction": 2,
            "breakpoints": ["desktop"],
            "themes": ["light"],
            "variant_cap": 24,
        },
        "directions": [
            {"title": "A", "description": "a", "motifs": []},
            {"title": "B", "description": "b", "motifs": []},
            {"title": "C", "description": "c", "motifs": []},
        ],
    }
    variants = mod.variants_node(state)["variants"]
    assert len({v["style"] for v in variants}) == 6
    assert {v["direction"] for v in variants} == {"A", "B", "C"}


def test_hitl_auto_select_spreads_directions():
    options = [
        {"id": "a1", "direction": "A"},
        {"id": "a2", "direction": "A"},
        {"id": "b1", "direction": "B"},
        {"id": "c1", "direction": "C"},
    ]
    result = mod.hitl_node({"options": options, "approval_status": "approved"})
    assert result["selection"] == ["a1", "b1", "c1"]


def test_l3_determinism_same_seed(monkeypatch, tmp_path):
    """L3: 같은 입력·같은 시드 2회 → 변주 id와 렌더 manifest 해시가 동일하다."""
    styles = [
        {"id": f"s{i}", "name": f"style-{i}", "axes": {"palette": "editorial"}}
        for i in range(4)
    ]
    monkeypatch.setattr(mod.craft, "load_registry", lambda _dir: styles)
    monkeypatch.setattr(mod, "OUT_ROOT", tmp_path)

    def fake_run(args, timeout=300):
        """렌더·임포트 스크립트 대체 — 산출물만 결정론적으로 만든다."""
        argv = [str(a) for a in args]
        script = Path(argv[1]).name
        out = Path(argv[argv.index("--out") + 1])
        if script == "import_content.py":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('{"items": []}', encoding="utf-8")
        elif script == "render_recipe.py":
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{out.name}.svg").write_text(
                f'<svg width="1440" height="1924"><!-- {out.name} --></svg>',
                encoding="utf-8",
            )
            (out / f"{out.name}.manifest.json").write_text(
                json.dumps({"id": out.name}), encoding="utf-8"
            )
        return ""

    monkeypatch.setattr(mod, "_run", fake_run)
    state = {
        "brief": "차분한 티 브랜드 랜딩",
        "content": "차분한 티 브랜드 랜딩 — 결정론 확인",
        "run_id": "entry-l3",
        "spec": {
            "platform": "99designs",
            "seed": "l3-seed",
            "styles_per_direction": 2,
            "breakpoints": ["desktop"],
            "themes": ["light", "dark"],
            "variant_cap": 8,
        },
        "directions": [{"title": "A", "description": "a", "motifs": ["m"]}],
    }

    def once():
        variants = mod.variants_node(state)["variants"]
        rendered = mod.render_node({**state, "variants": variants})
        return [v["id"] for v in variants], rendered["manifest_digest"]

    ids_first, digest_first = once()
    ids_second, digest_second = once()

    assert ids_first == ids_second
    assert digest_first == digest_second
    manifest = json.loads(
        (tmp_path / "entry-l3" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["inputs"]["seed"] == "l3-seed"
    assert manifest["artifacts"]
    assert all(row["sha256"] for row in manifest["artifacts"])
