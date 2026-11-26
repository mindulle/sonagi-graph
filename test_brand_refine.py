"""마크 생성 v2 (refine) — 기존 인장 정련의 계약·결정론·제작 규칙 테스트.

전부 오프라인이다. 원본 인장은 이 파일의 픽스처 SVG를 쓰고(실제 마스터는 CI에
없다), VectorCraft는 `ctx.run` 훅으로만 흉내 낸다. 좌표·SVG·게이트는 순수
함수라서 바이트 단위로 검증할 수 있다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from graphs import brand_marks_v2 as v2
from graphs import entry_pack_graph as entry
from graphs.profiles import base
from graphs.profiles import brand as brand_profile
from graphs.profiles.brand import CandidateGateError

# ---------------------------------------------------------------------------
# 픽스처 — 인장 구조(링 4겹·방사 8줄·코어·텍스트·그라디언트)
# ---------------------------------------------------------------------------

PALETTE = {
    "ink": "#161616",
    "paper": "#fbf9f5",
    "accent": "#b4551f",
    "calm": "#1f4536",
}

BRIEF = "민들레 — 차분하고 고급스러운 스튜디오. 여백과 인쇄물."


def _seal_svg() -> str:
    rays = []
    for index in range(8):
        angle = index * 45
        import math

        radians = math.radians(angle)
        x = 200 + 68 * math.cos(radians)
        y = 200 + 68 * math.sin(radians)
        rays.append(f'<line x1="200" y1="200" x2="{x:.2f}" y2="{y:.2f}" />')
        rays.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#d97706" />')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" '
        'width="400" height="400" fill="none">'
        "<defs><style>.a{fill:#1c1514}</style>"
        '<linearGradient id="g"><stop offset="0%" stop-color="#f59e0b"/></linearGradient>'
        "</defs>"
        '<circle cx="200" cy="200" r="192" fill="#faf8f5"/>'
        '<circle cx="200" cy="200" r="188" stroke="#1c1514" stroke-width="2.5"/>'
        '<circle cx="200" cy="200" r="176" stroke="#2563eb" stroke-width="1.5"/>'
        '<circle cx="200" cy="200" r="126" stroke="#1c1514" stroke-width="2"/>'
        '<circle cx="200" cy="200" r="120" stroke="#d6cfc7" stroke-width="1"/>'
        '<text class="a">MINDULLE STUDIO</text>'
        + "".join(rays)
        + '<circle cx="200" cy="200" r="14" stroke="#9a3412" stroke-width="1.8"/>'
        '<circle cx="200" cy="200" r="10" fill="url(#g)"/>'
        '<circle cx="200" cy="200" r="4" fill="#ffffff"/>'
        "</svg>"
    )


def _source(tmp_path: Path, text: str | None = None) -> Path:
    path = tmp_path / "master-seal.svg"
    path.write_text(text if text is not None else _seal_svg(), encoding="utf-8")
    return path


def _spec(tmp_path: Path, **brand_overrides):
    brand = {
        "mode": "refine",
        "name": "mindulle",
        "brief": BRIEF,
        "source_mark": str(_source(tmp_path)),
        "candidates": 8,
        "palette": dict(PALETTE),
        "variants": {"sizes": [16, 64], "backgrounds": ["light", "dark"]},
    }
    brand.update(brand_overrides)
    return {"profile": "brand", "brief": BRIEF, "brand": brand}


def _candidates(tmp_path: Path, **brand_overrides):
    spec = _spec(tmp_path, **brand_overrides)
    brand = brand_profile.brand_spec(spec)
    model = v2.analyze_source(Path(brand["source_mark"]).read_text(encoding="utf-8"))
    return v2.plan_candidates(
        model,
        brand["brief"],
        name=brand["display_name"],
        families=brand["families"],
        count=brand["candidates"],
        seed_hint=brand["seed"],
    )


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


def _fake_run(calls=None):
    def fake(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        if calls is not None:
            calls.append(argv)
        if len(argv) < 2:
            return ""
        if argv[1] == "convert":
            source, target = Path(argv[2]), Path(argv[3])
            if target.suffix.lower() == ".svg":
                text = source.read_text(encoding="utf-8")
                text = re.sub(
                    r"<text\b[^>]*>.*?</text>",
                    '<path id="wordmark-outline" d="M0 0h1v1z"/>',
                    text,
                    flags=re.S,
                )
                target.write_text(text, encoding="utf-8")
            elif target.suffix.lower() == ".pdf":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"%PDF-1.4 brand")
            elif target.suffix.lower() == ".png":
                scale = 1.0
                if "--scale" in argv:
                    scale = float(argv[argv.index("--scale") + 1])
                _probe_png(target, max(1, round(512 * scale)))
            return ""
        return ""

    return fake


def _fake_convert(svg, out, width, max_height=3000):
    _probe_png(Path(out), max(1, int(width)))


def _fake_jpg(png, out, quality=90):
    Path(out).write_bytes(b"jpg")


def _ctx(tmp_path: Path, *, run=None):
    return base.ProfileContext(
        run=run or _fake_run(),
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )


# ---------------------------------------------------------------------------
# 원본 해석
# ---------------------------------------------------------------------------


def test_analyze_source_reads_rings_rays_and_core():
    model = v2.analyze_source(_seal_svg())
    assert model.ring_ratios[0] == pytest.approx(1.0)
    assert model.ring_ratios[1] == pytest.approx(176 / 188, rel=1e-3)
    assert model.ray_count == 8
    assert model.ray_step == pytest.approx(45.0)
    assert model.ray_length_ratio == pytest.approx(68 / 188, rel=1e-2)
    assert model.core_ratio == pytest.approx(14 / 188, rel=1e-2)
    assert model.seed_dot_ratio == pytest.approx(2.5 / 188, rel=1e-2)
    assert model.has_gradient is True
    assert model.text_count == 1


def test_analyze_source_excludes_dome_diagonals_from_rays():
    text = _seal_svg().replace(
        "</svg>",
        '<line x1="121" y1="121" x2="200" y2="200" stroke-dasharray="2 3" /></svg>',
    )
    model = v2.analyze_source(text)
    # 길이가 다른 대각선 하나가 섞여도 방사선 8개 군집을 고른다.
    assert model.ray_count == 8
    assert model.ray_step == pytest.approx(45.0)


def test_analyze_source_falls_back_without_rings_or_rays():
    model = v2.analyze_source(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" fill="#000"/></svg>'
    )
    assert model.ray_count == 12
    assert model.ring_ratios[0] == pytest.approx(1.0)
    assert model.core_ratio > 0


def test_analyze_source_rejects_broken_svg():
    with pytest.raises(v2.RefineSourceError):
        v2.analyze_source("<svg><broken>")


# ---------------------------------------------------------------------------
# spec 계약
# ---------------------------------------------------------------------------


def test_refine_requires_source_mark(tmp_path):
    spec = _spec(tmp_path)
    spec["brand"].pop("source_mark")
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.brand_spec(spec)
    assert "source_mark" in str(exc.value)


def test_refine_rejects_missing_and_non_svg_source(tmp_path):
    spec = _spec(tmp_path, source_mark=str(tmp_path / "nope.svg"))
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.brand_spec(spec)
    assert "source_mark" in str(exc.value)

    text = tmp_path / "seal.txt"
    text.write_text(_seal_svg(), encoding="utf-8")
    spec = _spec(tmp_path, source_mark=str(text))
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.brand_spec(spec)
    assert "SVG" in str(exc.value)


def test_refine_defaults_eight_candidates_and_all_families(tmp_path):
    brand = brand_profile.brand_spec(_spec(tmp_path))
    assert brand["mode"] == "refine"
    assert brand["candidates"] == 8
    assert tuple(brand["families"]) == v2.FAMILIES
    assert brand["source_sha256"]
    assert brand["brief_sha256"]


def test_refine_candidate_bounds_are_eight_to_twelve(tmp_path):
    for count in (2, 7, 13, 0, "many", True):
        with pytest.raises(base.ProfileError):
            brand_profile.brand_spec(_spec(tmp_path, candidates=count))
    assert brand_profile.brand_spec(_spec(tmp_path, candidates=12))["candidates"] == 12


def test_refine_families_validation(tmp_path):
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.brand_spec(_spec(tmp_path, families=["star"]))
    assert "가족" in str(exc.value)
    with pytest.raises(base.ProfileError):
        brand_profile.brand_spec(_spec(tmp_path, families=[]))
    with pytest.raises(base.ProfileError):
        brand_profile.brand_spec(_spec(tmp_path, families="seal-monogram"))

    brand = brand_profile.brand_spec(_spec(tmp_path, families=["seal-monogram"]))
    assert brand["families"] == ["seal-monogram"]


def test_refine_requires_brief(tmp_path):
    spec = _spec(tmp_path)
    spec["brand"].pop("brief")
    spec.pop("brief")
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.brand_spec(spec)
    assert "brief" in str(exc.value)


def test_enrich_spec_injects_brief_and_top_n(tmp_path):
    spec = _spec(tmp_path)
    spec["brand"].pop("brief")
    spec.pop("brief")
    updated = brand_profile.BrandProfile().enrich_spec(spec, "여백의 인장")
    assert updated["brand"]["brief"] == "여백의 인장"
    assert updated["top_n"] == v2.MAX_CANDIDATES


# ---------------------------------------------------------------------------
# 계획 — 결정론·가족 분포·원본 파생
# ---------------------------------------------------------------------------


def test_refine_plan_is_deterministic(tmp_path):
    first = _candidates(tmp_path)
    second = _candidates(tmp_path)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    for candidate in first:
        assert v2.candidate_svg(candidate, PALETTE) == v2.candidate_svg(
            second[first.index(candidate)], PALETTE
        )


def test_refine_plan_changes_with_brief(tmp_path):
    first = _candidates(tmp_path)
    spec = _spec(tmp_path, brief="다른 브리프 — 대비와 각도")
    brand = brand_profile.brand_spec(spec)
    model = v2.analyze_source(Path(brand["source_mark"]).read_text(encoding="utf-8"))
    second = v2.plan_candidates(model, brand["brief"], name="mindulle", count=8)
    assert first[0]["id"] != second[0]["id"]
    assert first[0]["params"] != second[0]["params"]


def test_refine_plan_covers_every_family(tmp_path):
    candidates = _candidates(tmp_path)
    assert len(candidates) == 8
    assert len(v2.FAMILIES) == 3
    assert {row["family"] for row in candidates} == set(v2.FAMILIES)
    assert len({row["id"] for row in candidates}) == 8


def test_negative_cut_is_a_modifier_not_a_family(tmp_path):
    assert "negative-cut" not in v2.FAMILIES
    assert tuple(v2.MODIFIERS) == ("none", "negative-cut")
    candidates = _candidates(tmp_path)
    cut = [row for row in candidates if row["params"]["modifier"] == "negative-cut"]
    assert cut
    assert {row["family"] for row in cut} == set(v2.FAMILIES)
    for row in cut:
        assert "-cut-" in row["id"]
        assert row["params"]["cut"]["modifier"] == "negative-cut"


def test_every_candidate_keeps_the_seal_frame(tmp_path):
    for candidate in _candidates(tmp_path):
        rings = [
            shape
            for shape in candidate["params"]["shapes"]
            if shape["kind"] == "circle"
            and shape.get("stroke")
            and float(shape["r"]) >= v2.OUTER_RADIUS * 0.55
        ]
        assert len(rings) >= 2, candidate["id"]
        svg = v2.candidate_svg(candidate, PALETTE)
        assert svg.count("stroke-width=") >= 2
        # 글자만·방사선만 남은 마크는 없다 — 모든 후보가 인장 링을 갖는다.
        assert candidate["params"]["ring_weights"]


def test_negative_cut_keeps_frame_and_carves_the_family_motif(tmp_path):
    cut = [
        row
        for row in _candidates(tmp_path)
        if row["params"]["modifier"] == "negative-cut"
    ]
    assert cut
    for candidate in cut:
        params = candidate["params"]
        carved = [
            shape
            for shape in params["shapes"]
            if shape["kind"] == "path" and shape.get("fill_rule") == "nonzero"
        ]
        assert len(carved) == 1
        assert carved[0]["d"].count("M ") >= 3
        # 바깥 인장 링은 그대로다 — 음각은 중심부만 바꾼다.
        assert len(params["ring_weights"]) >= 2
        assert params["cut"]["holes"] >= 1
    shapes = {row["family"]: row["params"]["cut"]["shape"] for row in cut}
    assert shapes == {
        "seal-refined": "sunburst",
        "seal-monogram": "monogram",
        "seal-dandelion": "seed-rays",
    }


def test_refine_plan_respects_family_subset(tmp_path):
    candidates = _candidates(tmp_path, families=["seal-dandelion", "seal-refined"])
    assert {row["family"] for row in candidates} == {"seal-dandelion", "seal-refined"}


def test_refine_dandelion_derives_angles_from_source(tmp_path):
    candidates = _candidates(tmp_path, families=["seal-dandelion"])
    model = v2.analyze_source(_seal_svg())
    for candidate in candidates:
        phase = candidate["params"]["phase_shift"]
        angles = v2._ray_angles(model, candidate["params"]["seed_count"], phase)
        assert len(angles) == candidate["params"]["seed_count"]
        # 첫 각도는 원본 위상(0°) + 시드 오프셋이다.
        assert angles[0] == pytest.approx(phase % 360.0, abs=1e-6)
        assert angles[1] - angles[0] == pytest.approx(360 / len(angles), abs=1e-6)


# ---------------------------------------------------------------------------
# SVG·제작 규칙
# ---------------------------------------------------------------------------


def test_candidate_svg_has_no_text_style_or_gradient(tmp_path):
    for candidate in _candidates(tmp_path):
        svg = v2.candidate_svg(candidate, PALETTE)
        assert "<text" not in svg
        assert "<style" not in svg
        assert "url(#" not in svg
        assert "Gradient" not in svg
        assert "<mask" not in svg
        assert 'viewBox="0 0 512 512"' in svg


def test_candidate_palette_is_ink_accent_paper(tmp_path):
    allowed = {PALETTE["ink"], PALETTE["accent"], PALETTE["paper"]}
    for candidate in _candidates(tmp_path):
        svg = v2.candidate_svg(candidate, PALETTE)
        assert set(v2.paint_colors(svg)) <= allowed
        gate = v2.check_palette_roles(svg, PALETTE)
        assert gate["ok"], gate["detail"]


def test_stroke_widths_are_quantized_within_each_mark(tmp_path):
    for candidate in _candidates(tmp_path):
        svg = v2.candidate_svg(candidate, PALETTE)
        unit = candidate["params"]["stroke_unit"]
        allowed = {round(unit * ratio, 1) for ratio in v2.OPTICAL_SCALE}
        widths = v2.stroke_widths(svg)
        assert set(widths) <= allowed
        assert len(widths) <= len(allowed)
        assert v2.check_stroke_scale(candidate["params"], svg)["ok"]


def test_stroke_scale_gate_reports_outside_weight(tmp_path):
    candidate = _candidates(tmp_path)[0]
    svg = v2.candidate_svg(candidate, PALETTE).replace(
        'stroke-width="', 'stroke-width="7" data-w="', 1
    )
    # 첫 도형의 굵기를 7로 바꾼 셈이다(중복 속성은 파서가 마지막 값을 쓴다).
    gate = v2.check_stroke_scale(candidate["params"], svg)
    assert gate["ok"] is False
    assert "7" in gate["detail"]


def test_optical_overshoot_is_applied_and_bounded(tmp_path):
    for candidate in _candidates(tmp_path):
        svg = v2.candidate_svg(candidate, PALETTE)
        params = candidate["params"]
        if params["ring_weights"]:
            low, high = v2.OVERSHOOT_RANGE
            assert (
                low * v2.OUTER_RADIUS
                <= params["overshoot_px"]
                <= high * v2.OUTER_RADIUS
            )
            for radius in params["ring_radii"]:
                assert f'r="{radius:g}"' in svg or f'r="{radius}"' in svg
        assert v2.check_overshoot(params, svg)["ok"], v2.check_overshoot(params, svg)


def test_overshoot_gate_fails_when_zeroed(tmp_path):
    candidate = _candidates(tmp_path)[0]
    params = dict(candidate["params"])
    params["overshoot_px"] = 0.0
    svg = v2.candidate_svg({**candidate, "params": params}, PALETTE)
    gate = v2.check_overshoot(params, svg)
    assert gate["ok"] is False
    assert "오버슈트" in gate["detail"]


def test_monogram_is_optically_centered(tmp_path):
    for candidate in _candidates(tmp_path, families=["seal-monogram"]):
        mono = candidate["params"]["monogram"]
        segments, bounds = v2._monogram_box(mono["glyph"], mono["box"], mono["weight"])
        assert len(segments) >= 3
        offset_x, offset_y = mono["center_offset"]
        center_x = (bounds[0] + bounds[2]) / 2 + offset_x
        center_y = (bounds[1] + bounds[3]) / 2 + offset_y
        assert abs(center_x - v2.CENTER) <= 0.5
        assert abs(center_y - v2.CENTER) <= 0.5


def test_counters_stay_open_at_16px():
    gap = v2.counter_gap_16("A", 150, 24)
    assert gap is not None and gap >= v2.COUNTER_MIN_PX
    candidate = {
        "params": {
            "monogram": {"glyph": "A", "box": 30, "weight": 20},
        }
    }
    assert v2.check_counters(candidate)["ok"] is False
    assert v2.check_counters({"params": {}})["ok"] is True


def test_negative_cut_is_a_real_hole(tmp_path):
    cut = [
        row
        for row in _candidates(tmp_path)
        if row["params"]["modifier"] == "negative-cut"
    ]
    assert cut
    for candidate in cut:
        svg = v2.candidate_svg(candidate, PALETTE)
        assert 'fill-rule="nonzero"' in svg
        assert candidate["params"]["modifier"] == "negative-cut"
        assert candidate["params"]["ring_weights"]


def test_craft_gates_cover_all_refine_rule_ids(tmp_path):
    candidate = _candidates(tmp_path)[0]
    svg = v2.candidate_svg(candidate, PALETTE)
    gates = v2.craft_gates(candidate, svg, PALETTE)
    assert {gate["id"] for gate in gates} == set(v2.REFINE_GATE_IDS)
    assert all(gate["ok"] for gate in gates)


def test_palette_gate_rejects_outside_color(tmp_path):
    candidate = _candidates(tmp_path)[0]
    svg = v2.candidate_svg(candidate, PALETTE).replace("#161616", "#123456", 1)
    gate = v2.check_palette_roles(svg, PALETTE)
    assert gate["ok"] is False
    assert "#123456" in gate["detail"]


# ---------------------------------------------------------------------------
# 후보 게이트 요약·표
# ---------------------------------------------------------------------------


def _passing_gates(ids):
    return [
        {"id": gate_id, "ok": True, "detail": "ok", "skipped": False} for gate_id in ids
    ]


def test_refine_required_gates_include_craft_rules():
    assert v2.REFINE_GATE_IDS <= brand_profile.REQUIRED_REFINE_GATE_IDS
    assert (
        brand_profile.REQUIRED_CANDIDATE_GATE_IDS
        <= brand_profile.REQUIRED_REFINE_GATE_IDS
    )


def test_check_candidates_reports_missing_craft_gates():
    renders = [
        {"id": "a", "gates": _passing_gates(brand_profile.REQUIRED_CANDIDATE_GATE_IDS)},
        {"id": "b", "gates": _passing_gates(brand_profile.REQUIRED_CANDIDATE_GATE_IDS)},
    ]
    with pytest.raises(CandidateGateError) as exc:
        brand_profile.check_candidates(
            renders, required=brand_profile.REQUIRED_REFINE_GATE_IDS
        )
    assert "게이트 누락" in str(exc.value)
    assert "candidate.stroke_scale" in str(exc.value)


def test_check_candidates_passes_all_refine_gates():
    rows = _passing_gates(brand_profile.REQUIRED_REFINE_GATE_IDS)
    result = brand_profile.check_candidates(
        [{"id": "a", "gates": rows}, {"id": "b", "gates": rows}],
        required=brand_profile.REQUIRED_REFINE_GATE_IDS,
    )
    assert result["passing"] == 2
    assert result["failures"] == []


def test_gate_table_markdown_marks_failures_and_notes():
    rows = [
        {
            "id": "refine-a",
            "family": "seal-refined",
            "note": "고리 2겹",
            "gates": [
                {"id": "candidate.palette_roles", "ok": True, "detail": "ok"},
                {"id": "candidate.stroke_scale", "ok": False, "detail": "획 7"},
            ],
        },
        {
            "id": "refine-b",
            "family": "seal-dandelion",
            "note": "씨앗을 파냄",
            "gates": [{"id": "candidate.palette_roles", "ok": True, "detail": "ok"}],
        },
    ]
    table = v2.gate_table_markdown(rows)
    assert "| refine-a |" in table
    assert "✓" in table and "✗" in table and "누락" in table
    assert "획 7" in table
    assert "씨앗을 파냄" in table


# ---------------------------------------------------------------------------
# 프레젠테이션
# ---------------------------------------------------------------------------


def _synthetic_presentation(tmp_path: Path, count: int = 4):
    rows = []
    for index in range(count):
        base_png = tmp_path / f"c{index}.png"
        _probe_png(base_png, 512)
        pres = v2.presentation_images(base_png, tmp_path / f"c{index}", PALETTE)
        rows.append(
            {
                "id": f"refine-{index}",
                "family": v2.FAMILIES[index % len(v2.FAMILIES)],
                "note": "고리 2겹 · 방사 9줄. 원본에서 미세선을 덜었다.",
                **pres,
                "gates": _passing_gates(["candidate.palette_roles"]),
            }
        )
    return rows


def test_presentation_images_are_paper_dark_and_16px(tmp_path):
    png = tmp_path / "mark.png"
    _probe_png(png, 512)
    pres = v2.presentation_images(png, tmp_path / "out", PALETTE)
    from PIL import Image

    with Image.open(pres["light"]) as light:
        assert light.size == (512, 512)
        assert light.getpixel((2, 2)) == (251, 249, 245)
    with Image.open(pres["dark"]) as dark:
        assert dark.getpixel((2, 2)) == (22, 22, 22)
    with Image.open(pres["small"]) as small:
        assert small.size == (16, 16)


def test_contact_sheet_is_deterministic_and_sized(tmp_path):
    rows = _synthetic_presentation(tmp_path)
    first = v2.compose_candidate_sheet(
        rows,
        out=tmp_path / "one.png",
        palette=PALETTE,
        brief=BRIEF,
        run_id="run-1",
    )
    second = v2.compose_candidate_sheet(
        rows,
        out=tmp_path / "two.png",
        palette=PALETTE,
        brief=BRIEF,
        run_id="run-1",
    )
    assert first["sha256"] == second["sha256"]
    assert first["count"] == 4
    assert first["width"] > 0 and first["height"] > 0
    assert Path(first["path"]).is_file()


# ---------------------------------------------------------------------------
# HITL — 자동 선택 금지
# ---------------------------------------------------------------------------


def test_refine_never_auto_selects(tmp_path):
    options = [
        {
            "id": "refine-a",
            "family": "seal-refined",
            "gates": [{"id": "x", "ok": True}],
        },
        {
            "id": "refine-b",
            "family": "seal-dandelion",
            "gates": [{"id": "x", "ok": True}],
        },
    ]
    result = entry.hitl_node(
        {
            "options": options,
            "approval_status": "approved",
            "spec": _spec(tmp_path),
        }
    )
    assert result["status"] == "awaiting_selection"
    assert result["approval_status"] == "pending"
    assert "selection" not in result


def test_refine_still_accepts_explicit_selection(tmp_path):
    options = [{"id": "refine-a"}, {"id": "refine-b"}]
    result = entry.hitl_node(
        {
            "options": options,
            "approval_status": "approved",
            "selection": ["refine-b"],
            "spec": _spec(tmp_path),
        }
    )
    assert result == {"status": "selected"}


def test_auto_select_allowed_only_outside_refine(tmp_path):
    profile = brand_profile.BrandProfile()
    assert profile.auto_select_allowed(_spec(tmp_path)) is False
    explore = _spec(tmp_path, mode="explore")
    explore["brand"].pop("source_mark")
    assert profile.auto_select_allowed(explore) is True


# ---------------------------------------------------------------------------
# profile.render 통합 (오프라인 fake runner)
# ---------------------------------------------------------------------------


def test_profile_render_refine_returns_gates_note_and_presentation(tmp_path):
    spec = _spec(tmp_path)
    profile = brand_profile.BrandProfile()
    ctx = _ctx(tmp_path)
    variants = profile.plan({}, spec, [])
    assert len(variants) == 8
    rendered = profile.render(variants[0], ctx)
    assert rendered["family"] == variants[0]["family"]
    assert rendered["note"]
    assert len(rendered["gates"]) == 12
    assert all(gate["ok"] for gate in rendered["gates"]), [
        gate for gate in rendered["gates"] if not gate["ok"]
    ]
    for key in ("light", "dark", "small"):
        assert Path(rendered[key]).is_file()

    sheet = profile.contact_sheet([rendered], spec, {"run_id": "run"}, ctx)
    assert Path(sheet["path"]).name == "candidate-contact.png"
    assert Path(sheet["gate_table"]).is_file()
    table = Path(sheet["gate_table"]).read_text(encoding="utf-8")
    assert "candidate.stroke_scale" in table
    assert "candidate.optical_overshoot" in table
