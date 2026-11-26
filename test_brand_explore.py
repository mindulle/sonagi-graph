"""brand explore 모드 (GRAPH-33) — 후보 계획·게이트·HITL·킷.

전부 오프라인이다. VectorCraft는 `ctx.run` 훅으로만 흉내 내고, 실제 좌표·명령은
순수 함수(`brand_marks.plan_candidates`/`candidate_commands`)로 검증한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from graphs import brand_marks
from graphs import entry_pack_graph as entry
from graphs.profiles import base
from graphs.profiles import brand as brand_profile
from graphs.profiles.brand import CandidateGateError

# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" '
    'viewBox="0 0 512 512"><rect width="512" height="512" fill="#fbf9f5"/>'
    '<path d="M128 128 L384 128 L384 384 L128 384 Z" fill="#161616"/></svg>'
)

BRIEF = "민들레 — 차분하고 고급스러운 스튜디오. 여백과 인쇄물."
PALETTE = {
    "ink": "#161616",
    "paper": "#fbf9f5",
    "accent": "#b4551f",
    "calm": "#1f4536",
}


def _mark(tmp_path, name="mark.svg", text=MARK_SVG):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _explore_spec(tmp_path, **brand_overrides):
    brand = {
        "mode": "explore",
        "name": "mindulle",
        "brief": BRIEF,
        "candidates": 6,
        "palette": dict(PALETTE),
        "variants": {"sizes": [16, 64], "backgrounds": ["light", "dark"]},
    }
    brand.update(brand_overrides)
    return {
        "profile": "brand",
        "brief": BRIEF,
        "image_width": 2000,
        "max_height": 3000,
        "jpg_quality": 90,
        "brand": brand,
    }


def _existing_spec(tmp_path, *, mode=None):
    brand = {
        "name": "sonagi",
        "mark_path": str(_mark(tmp_path)),
        "variants": {"sizes": [16, 64], "backgrounds": ["light", "dark"]},
    }
    if mode is not None:
        brand["mode"] = mode
    return {"profile": "brand", "brand": brand}


def _probe_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" '
        'viewBox="0 0 512 512"><path d="M128 128 L384 128 L384 384 L128 384 Z" '
        'fill="#161616"/></svg>'
    )


def _probe_png(path: Path, size: int, *, blank: bool = False) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if not blank:
        margin = max(1, round(size * 0.15))
        ImageDraw.Draw(image).ellipse(
            (margin, margin, size - margin, size - margin),
            fill=(22, 22, 22, 255),
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def _fake_explore_run(*, blank=False, blank_ids=(), calls=None):
    """`run`(후보 생성)과 `convert`(아웃라인·래스터·PDF)를 흉내 낸다."""

    def fake_run(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        if calls is not None:
            calls.append(argv)
        if len(argv) < 2:
            return ""
        if argv[1] == "run":
            exports = [
                Path(argv[index + 1])
                for index, token in enumerate(argv)
                if token == "--export"
            ]
            if not exports:
                return ""
            svg = exports[0]
            svg.parent.mkdir(parents=True, exist_ok=True)
            svg.write_text(_probe_svg(), encoding="utf-8")
            if len(exports) > 1:
                empty = blank or svg.parent.name in set(blank_ids)
                _probe_png(exports[1], 512, blank=empty)
            return ""
        if argv[1] == "convert":
            source, target = Path(argv[2]), Path(argv[3])
            suffix = target.suffix.lower()
            if suffix == ".svg":
                text = source.read_text(encoding="utf-8")
                text = re.sub(
                    r"<text\b[^>]*>.*?</text>",
                    '<path id="wordmark-outline" d="M0 0h1v1z"/>',
                    text,
                    flags=re.S,
                )
                target.write_text(text, encoding="utf-8")
            elif suffix == ".pdf":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"%PDF-1.4 brand")
            elif suffix == ".png":
                scale = 1.0
                if "--scale" in argv:
                    scale = float(argv[argv.index("--scale") + 1])
                size = max(1, round(512 * scale))
                empty = blank or source.parent.name in set(blank_ids)
                _probe_png(target, size, blank=empty)
            return ""
        return ""

    return fake_run


def _fake_convert(svg, out, width, max_height=3000):
    _probe_png(Path(out), max(1, int(width)))


def _fake_jpg(png, out, quality=90):
    Path(out).write_bytes(b"jpg")


def _ctx(tmp_path, *, run=None, name="run"):
    return base.ProfileContext(
        run=run or _fake_explore_run(),
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / name,
    )


def _render_all(profile, spec, ctx):
    return [profile.render(variant, ctx) for variant in profile.plan({}, spec, [])]


# ---------------------------------------------------------------------------
# spec 계약
# ---------------------------------------------------------------------------


def test_explore_requires_brief_or_concept(tmp_path):
    spec = _explore_spec(tmp_path)
    spec["brand"].pop("brief")
    spec.pop("brief")
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.brand_spec(spec)
    assert "brief" in str(exc.value)

    # concept만 있어도 브리프 자리로 쓴다.
    spec["brand"].pop("brief", None)
    spec["brand"]["concept"] = "겹친 이니셜과 여백"
    brand = brand_profile.brand_spec(spec)
    assert brand["brief"] == "겹친 이니셜과 여백"
    assert brand["mode"] == "explore"


def test_explore_candidates_bounds_and_mode_validation(tmp_path):
    for count in (0, 1, 13, "many", True):
        spec = _explore_spec(tmp_path, candidates=count)
        with pytest.raises(base.ProfileError):
            brand_profile.brand_spec(spec)
    spec = _explore_spec(tmp_path, mode="surprise")
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.brand_spec(spec)
    assert "mode" in str(exc.value)


def test_explore_mark_path_is_optional_but_validated_when_given(tmp_path):
    spec = _explore_spec(tmp_path)
    assert brand_profile.brand_spec(spec)["mark_path"] is None

    spec["brand"]["mark_path"] = str(_mark(tmp_path))
    assert brand_profile.brand_spec(spec)["mark_path"].endswith("mark.svg")

    spec["brand"]["mark_path"] = str(tmp_path / "nope.svg")
    with pytest.raises(base.ProfileError):
        brand_profile.brand_spec(spec)


def test_explore_spec_node_injects_top_level_brief(tmp_path):
    spec = {"profile": "brand", "brand": {"mode": "explore", "candidates": 3}}
    state = entry.spec_node({"brief": BRIEF, "spec": spec, "run_id": "explore-node"})
    assert state["spec"]["brand"]["brief"] == BRIEF
    assert state["spec"]["brand"]["mode"] == "explore"
    assert state["spec"]["skip_llm"] is True


def test_existing_mode_without_mark_still_fails(tmp_path):
    with pytest.raises(base.ProfileError) as exc:
        entry.spec_node({"brief": "브랜드", "spec": {"profile": "brand", "brand": {}}})
    assert "mark_path" in str(exc.value)
    assert "explore" in str(exc.value)


# ---------------------------------------------------------------------------
# 결정론
# ---------------------------------------------------------------------------


def test_plan_candidates_is_seed_deterministic():
    first = brand_marks.plan_candidates(BRIEF, count=6)
    second = brand_marks.plan_candidates(BRIEF, count=6)
    assert first == second
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert [row["family"] for row in first] == [
        "monogram",
        "seal",
        "radial",
        "monogram",
        "seal",
        "radial",
    ]
    assert len({row["seed"] for row in first}) == 6

    other = brand_marks.plan_candidates(BRIEF + " (다른 톤)", count=6)
    assert [row["seed"] for row in other] != [row["seed"] for row in first]

    commands_a = brand_marks.candidate_commands(first[0], PALETTE)
    commands_b = brand_marks.candidate_commands(first[0], PALETTE)
    assert commands_a == commands_b
    assert commands_a[0]["cmd"] == "file.new"
    assert any(command["cmd"] == "path.create" for command in commands_a)


def test_plan_candidates_seed_hint_changes_geometry():
    plain = brand_marks.plan_candidates(BRIEF, count=3)
    hinted = brand_marks.plan_candidates(BRIEF, count=3, seed_hint="fixed-1")
    assert [row["seed"] for row in plain] != [row["seed"] for row in hinted]


def test_candidate_commands_use_only_palette_colors():
    plans = brand_marks.plan_candidates(BRIEF, count=6)
    allowed = set(PALETTE.values())
    for plan in plans:
        commands = brand_marks.candidate_commands(plan, PALETTE)
        for command in commands:
            if command["cmd"] in ("paint.setFill", "paint.setStroke"):
                color = command["params"].get("color")
                if color:
                    assert color in allowed


def test_explore_render_is_deterministic(tmp_path):
    spec = _explore_spec(tmp_path, candidates=3)
    profile = base.get_profile("brand")
    digests = []
    for name in ("run-a", "run-b"):
        ctx = _ctx(tmp_path, name=name)
        renders = _render_all(profile, spec, ctx)
        digests.append(
            [
                {
                    "id": render["id"],
                    "svg": Path(render["svg"]).read_text(encoding="utf-8"),
                    "single": Path(render["single_color"]).read_text(encoding="utf-8"),
                }
                for render in renders
            ]
        )
    assert digests[0] == digests[1]


# ---------------------------------------------------------------------------
# 게이트 실패 보고 · 최소 2개
# ---------------------------------------------------------------------------


def test_failed_candidates_are_reported_not_dropped(tmp_path):
    spec = _explore_spec(tmp_path, candidates=3)
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path, run=_fake_explore_run(blank=True))
    renders = _render_all(profile, spec, ctx)

    assert len(renders) == 3
    for render in renders:
        assert render["gates"]
        assert "실패" in render["gate_summary"]
        failed = {gate["id"] for gate in render["gates"] if not gate["ok"]}
        assert "brand.raster" in failed
        assert "candidate.legible_16" in failed

    with pytest.raises(CandidateGateError) as exc:
        profile.check_candidates(renders, spec)
    message = str(exc.value)
    assert "최소 2개" in message
    assert "brand.raster" in message
    assert all(render["id"] in message for render in renders)


def test_two_passing_candidates_satisfy_the_minimum(tmp_path):
    spec = _explore_spec(tmp_path, candidates=3)
    profile = base.get_profile("brand")
    plans = profile.plan({}, spec, [])
    blank_id = plans[-1]["id"]
    ctx = _ctx(tmp_path, run=_fake_explore_run(blank_ids={blank_id}))
    renders = _render_all(profile, spec, ctx)

    result = profile.check_candidates(renders, spec)
    assert result["passing"] == 2
    assert result["total"] == 3
    assert [row["candidate"] for row in result["failures"]] == [blank_id]


def test_gate_node_refuses_fewer_than_two_passing(tmp_path):
    spec = _explore_spec(tmp_path, candidates=2)
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path, run=_fake_explore_run(blank=True))
    renders = _render_all(profile, spec, ctx)
    critiques = [{"id": render["id"], "verdict": "pass"} for render in renders]
    with pytest.raises(CandidateGateError):
        entry.gate_node({"critiques": critiques, "renders": renders, "spec": spec})


# ---------------------------------------------------------------------------
# spec 추가 검증
# ---------------------------------------------------------------------------


def test_explore_candidates_rejects_non_integral_numbers(tmp_path):
    spec = _explore_spec(tmp_path, candidates=2.9)
    with pytest.raises(base.ProfileError):
        brand_profile.brand_spec(spec)
    spec = _explore_spec(tmp_path, candidates="3.0")
    with pytest.raises(base.ProfileError):
        brand_profile.brand_spec(spec)


def test_explore_alpha_palette_is_stripped_for_vectorcraft():
    palette = {**PALETTE, "ink": "#16161680"}
    plans = brand_marks.plan_candidates(BRIEF, count=3)
    commands = brand_marks.candidate_commands(plans[0], palette)
    colors = {
        command["params"]["color"]
        for command in commands
        if command["cmd"] == "paint.setFill" and command["params"].get("color")
    }
    assert colors <= {"#161616", PALETTE["accent"], PALETTE["calm"]}
    assert "#16161680" not in colors


# ---------------------------------------------------------------------------
# HITL · 킷
# ---------------------------------------------------------------------------


def test_recolor_svg_only_maps_known_colors():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><path fill="#161616" d="M0 0h1v1z"/>'
        '<path stroke="#b4551f" fill="none" d="M0 0h1v1z"/></svg>'
    )
    out = brand_marks.recolor_svg(svg, {"#161616": "#fbf9f5"})
    assert 'fill="#fbf9f5"' in out
    assert 'stroke="#b4551f"' in out
    assert 'fill="none"' in out


def test_build_icon_variants_uses_dark_page(tmp_path):
    from PIL import Image

    light_page = tmp_path / "light.png"
    dark_page = tmp_path / "dark.png"
    Image.new("RGBA", (64, 64), (22, 22, 22, 255)).save(light_page)
    Image.new("RGBA", (64, 64), (251, 249, 245, 255)).save(dark_page)
    rows = brand_profile.build_icon_variants(
        light_page,
        tmp_path / "icons",
        name="x",
        sizes=[16],
        backgrounds=["light", "dark"],
        palette=PALETTE,
        dark_page=dark_page,
    )
    dark = next(row for row in rows if row["background"] == "dark")
    with Image.open(dark["path"]) as image:
        assert image.convert("RGB").getpixel((8, 8))[0] > 200


def test_hitl_auto_selection_skips_failed_and_picks_one_explore_candidate():
    options = [
        {
            "id": "radial-a",
            "family": "radial",
            "direction": "radial",
            "gates": [{"id": "brand.raster", "ok": False, "detail": "blank"}],
        },
        {
            "id": "monogram-b",
            "family": "monogram",
            "direction": "monogram",
            "gates": [{"id": "brand.raster", "ok": True, "detail": "ok"}],
        },
        {
            "id": "seal-c",
            "family": "seal",
            "direction": "seal",
            "gates": [{"id": "brand.raster", "ok": True, "detail": "ok"}],
        },
    ]
    selection = entry.hitl_node({"options": options, "approval_status": "approved"})[
        "selection"
    ]
    assert selection == ["monogram-b"]


def test_explore_pack_requires_candidate_metadata_and_gates(tmp_path):
    spec = _explore_spec(tmp_path, candidates=2)
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path)
    renders = _render_all(profile, spec, ctx)

    no_gates = dict(renders[0])
    no_gates.pop("gates")
    with pytest.raises(brand_profile.BrandGateError):
        profile.pack(
            [no_gates],
            spec,
            {"profile_ctx": ctx, "spec": spec, "selection": [no_gates["id"]]},
        )

    no_shapes = dict(renders[0])
    no_shapes["candidate"] = {"id": renders[0]["id"], "params": {}}
    with pytest.raises(base.ProfileError):
        profile.pack(
            [no_shapes],
            spec,
            {"profile_ctx": ctx, "spec": spec, "selection": [no_shapes["id"]]},
        )


def test_explore_pack_rejects_multi_selection(tmp_path):
    spec = _explore_spec(tmp_path, candidates=2)
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path)
    renders = _render_all(profile, spec, ctx)
    with pytest.raises(base.ProfileError) as exc:
        profile.pack(
            renders,
            spec,
            {
                "profile_ctx": ctx,
                "spec": spec,
                "selection": [render["id"] for render in renders],
            },
        )
    assert "후보 1개" in str(exc.value)


def test_explore_pack_keeps_final_pack_time_gates(tmp_path):
    spec = {
        **_explore_spec(tmp_path, candidates=2),
        "allow_brand_checks": True,
    }
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path)
    renders = _render_all(profile, spec, ctx)
    bad = dict(renders[0])
    bad_outlined = Path(bad["outlined"])
    bad_outlined.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" '
        'viewBox="0 0 512 512"><text x="0" y="100">leftover</text></svg>',
        encoding="utf-8",
    )
    result = profile.pack(
        [bad],
        spec,
        {"profile_ctx": ctx, "spec": spec, "selection": [bad["id"]]},
    )
    meta = json.loads(Path(result["meta"]).read_text(encoding="utf-8"))
    no_text = [gate for gate in meta["gates"] if gate["id"] == "brand.no_text"]
    assert len(no_text) == 1  # 후보 게이트가 아니라 팩 시점 재검사
    assert no_text[0]["ok"] is False and no_text[0]["skipped"] is True


def test_entry_nodes_expose_one_option_per_candidate(tmp_path, monkeypatch):
    spec = _explore_spec(tmp_path, candidates=6)
    monkeypatch.setattr(entry, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(entry, "_run", _fake_explore_run())
    monkeypatch.setattr(entry, "_convert_png", _fake_convert)
    monkeypatch.setattr(entry, "_to_jpg", _fake_jpg)

    state = entry.spec_node({"brief": BRIEF, "spec": spec, "run_id": "explore-hitl"})
    assert state["spec"]["brand"]["brief"] == BRIEF
    variants = base.profile_for_spec(state["spec"]).plan(state["ir"], state["spec"], [])
    renders = entry.render_node(
        {"spec": state["spec"], "variants": variants, "run_id": "explore-hitl"}
    )["renders"]
    critiques = entry.critic_node({"spec": state["spec"], "renders": renders})[
        "critiques"
    ]
    options = entry.gate_node(
        {"critiques": critiques, "renders": renders, "spec": state["spec"]}
    )["options"]

    assert len(options) == 6
    assert {option["id"] for option in options} == {
        variant["id"] for variant in variants
    }
    for option in options:
        assert option["family"] in brand_marks.FAMILIES
        assert len(option["seed"]) == 12
        assert "통과" in option["gate_summary"]
        assert len(option["gates"]) == 7
        assert option["candidate"]["id"] == option["id"]


def test_explore_pack_builds_icons_lockup_and_pdf(tmp_path):
    spec = _explore_spec(tmp_path)
    brand_spec = {
        **spec,
        "brand": {
            **spec["brand"],
            "variants": {
                "sizes": [16, 32, 64, 256, 512],
                "backgrounds": ["light", "dark"],
            },
        },
    }
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path)
    renders = _render_all(profile, brand_spec, ctx)
    chosen = renders[0]
    result = profile.pack(
        [chosen],
        brand_spec,
        {
            "profile_ctx": ctx,
            "spec": brand_spec,
            "selection": [chosen["id"]],
        },
    )

    pack_dir = Path(result["dir"])
    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "explore"
    assert meta["gates_ok"] is True
    assert meta["candidate"]["id"] == chosen["id"]
    assert meta["candidate_gate_summary"].startswith("7/7")
    assert meta["lockup"]["svg"].endswith("lockup.svg")
    assert (pack_dir / "lockup.svg").is_file()
    assert (pack_dir / "lockup.pdf").is_file()
    assert (pack_dir / "mark.pdf").is_file()
    assert "<text" not in (pack_dir / "lockup.svg").read_text(encoding="utf-8")
    assert len(meta["icons"]) == 10
    assert {row["size"] for row in meta["icons"]} == {16, 32, 64, 256, 512}
    assert {row["background"] for row in meta["icons"]} == {"light", "dark"}
    assert (pack_dir / "entry.md").read_text(encoding="utf-8").count("explore") >= 1
    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "ok"
    assert len(manifest["gates"]) == 9  # 후보 7 + PDF + 락업


def test_explore_pack_fails_closed_when_chosen_candidate_failed(tmp_path):
    spec = _explore_spec(tmp_path, candidates=2)
    profile = base.get_profile("brand")
    plans = profile.plan({}, spec, [])
    ctx = _ctx(tmp_path, run=_fake_explore_run(blank_ids={plans[0]["id"]}))
    renders = _render_all(profile, spec, ctx)
    with pytest.raises(brand_profile.BrandGateError):
        profile.pack(
            [renders[0]],
            spec,
            {"profile_ctx": ctx, "spec": spec, "selection": [renders[0]["id"]]},
        )


# ---------------------------------------------------------------------------
# existing 모드 회귀 — 명시 모드와 기본 모드가 바이트 동일
# ---------------------------------------------------------------------------


def _existing_artifacts(spec, tmp_path, name):
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path, run=_fake_explore_run(), name=name)
    variant = profile.plan({}, spec, [])[0]
    rendered = profile.render(variant, ctx)
    result = profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})
    pack_dir = Path(result["dir"])
    meta_text = (pack_dir / "meta.json").read_text(encoding="utf-8")
    return {
        # 실행 디렉터리 이름만 다르다 — 그 밖의 바이트는 같아야 한다.
        "meta": meta_text.replace(str(tmp_path / name), "<run>"),
        "entry": (pack_dir / "entry.md").read_text(encoding="utf-8"),
        "artifacts": {
            name: row["sha256"]
            for name, row in json.loads(meta_text)["artifacts"].items()
        },
    }


def test_existing_mode_is_byte_identical_with_explicit_mode(tmp_path):
    default = _existing_artifacts(_existing_spec(tmp_path), tmp_path, "run-default")
    explicit = _existing_artifacts(
        _existing_spec(tmp_path, mode="existing"), tmp_path, "run-explicit"
    )
    assert default == explicit
    assert default["artifacts"]
    # 후보 생성 키가 existing meta에 새지 않는다.
    assert '"mode": "explore"' not in default["meta"]
    assert '"candidate"' not in default["meta"]
