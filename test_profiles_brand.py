"""brand profile (GRAPH-18, P4b) — 정규화·게이트·팩·목업.

전부 오프라인이다. vectorcraft-cli·PDF·네트워크를 요구하지 않고, 외부 프로세스는
`ctx.run` 훅 monkeypatch로만 흉내 낸다. 정규화는 순수 함수라 픽스처 SVG만 쓴다.
"""

import json
import re
from pathlib import Path

import pytest

from graphs import entry_pack_graph as entry
from graphs.core import artifacts as core_artifacts
from graphs.profiles import base
from graphs.profiles import brand as brand_profile
from graphs.profiles.brand import BrandProfile

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: `<style>`·var()·class·`<text>`·다크 @media를 모두 가진 마크.
MARK_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <style type="text/css"><![CDATA[
    :root { --ink: #161616; --paper: #fbf9f5; --accent: #b4551f; }
    .frame { fill: var(--paper); stroke: var(--ink); stroke-width: 4; }
    path.accent { fill: var(--accent); }
    .wordmark {
      fill: var(--ink);
      font-family: "Archivo, sans-serif";
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 2px;
    }
    @media (prefers-color-scheme: dark) { :root { --ink: #ffffff; } }
  ]]></style>
  <rect class="frame" width="512" height="512"/>
  <path class="accent" d="M0 0h64v64z"/>
  <text class="wordmark" x="40" y="300">Sonagi</text>
</svg>
"""


def _mark(tmp_path, text=MARK_SVG, name="mark.svg"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _brand_spec(tmp_path, **overrides):
    spec = {
        "profile": "brand",
        "brand": {
            "name": "sonagi",
            "mark_path": str(_mark(tmp_path)),
            "variants": {"sizes": [16, 64], "backgrounds": ["light", "dark"]},
        },
        "image_width": 2000,
        "max_height": 3000,
        "jpg_quality": 90,
    }
    spec.update(overrides)
    return spec


def _fake_brand_run(
    *,
    paint=True,
    pdf_bytes=b"%PDF-1.4 brand",
    outline_keeps_text=False,
    fail_outline=False,
    calls=None,
):
    """vectorcraft-cli convert(SVG/PNG/PDF)를 흉내 내는 ctx.run 훅."""

    def fake_run(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        if calls is not None:
            calls.append(argv)
        out = Path(argv[3])
        suffix = out.suffix.lower()
        if suffix == ".svg":
            if fail_outline:
                raise FileNotFoundError("vectorcraft-cli: command not found")
            text = Path(argv[2]).read_text(encoding="utf-8")
            if not outline_keeps_text:
                text = re.sub(
                    r"<text\b[^>]*>.*?</text>",
                    '<path id="wordmark-outline" d="M0 0h1v1z"/>',
                    text,
                    flags=re.S,
                )
            out.write_text(text, encoding="utf-8")
            return ""
        if suffix == ".pdf":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(pdf_bytes)
            return ""
        if suffix == ".png":
            from PIL import Image, ImageDraw

            out.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            if paint:
                ImageDraw.Draw(image).ellipse(
                    (16, 16, 240, 240), fill=(22, 22, 22, 255)
                )
            image.save(out)
            return ""
        return ""

    return fake_run


def _fake_convert(svg, out, width, max_height=3000):
    from PIL import Image, ImageDraw

    height = min(int(width), int(max_height)) // 2
    image = Image.new("RGBA", (int(width), height), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse(
        (10, 10, height - 10, height - 10), fill=(22, 22, 22, 255)
    )
    image.save(out, "PNG")


def _fake_jpg(png, out, quality=90):
    Path(out).write_bytes(b"jpg")


def _fail_jpg(png, out, quality=90):
    raise AssertionError("JPG 훅이 호출되면 안 된다")


def _ctx(tmp_path, run=None, convert=None):
    return base.ProfileContext(
        run=run or _fake_brand_run(),
        convert_png=convert or _fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )


def _collect(item, spec, tmp_path, **kwargs):
    profile = base.get_profile("brand")
    variant = profile.plan({}, spec, [])[0]
    ctx = _ctx(tmp_path, **kwargs)
    rendered = profile.render(variant, ctx)
    result = profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})
    return profile, rendered, result


# ---------------------------------------------------------------------------
# Registry / spec 계약
# ---------------------------------------------------------------------------


def test_registry_resolves_brand_profile():
    assert isinstance(base.get_profile("brand"), BrandProfile)
    assert base.get_profile("BRAND").id == "brand"
    assert "brand" in base.known_profiles()
    assert base.profile_for_spec({"profile": "brand"}).id == "brand"
    assert base.profile_for_spec({"contest_type": "brand"}).id == "brand"


def test_brand_defaults_are_deterministic_offline():
    defaults = base.get_profile("brand").defaults()
    assert defaults["allow_brand_checks"] is False
    assert defaults["skip_knowledge"] is True and defaults["skip_llm"] is True
    assert base.get_profile("brand").defaults() == defaults


def test_brand_spec_node_fails_fast_without_mark(tmp_path):
    with pytest.raises(base.ProfileError) as exc:
        entry.spec_node({"brief": "브랜드", "spec": {"profile": "brand"}})
    assert "mark_path" in str(exc.value)

    spec = _brand_spec(tmp_path, brand={"mark_path": str(tmp_path / "nope.svg")})
    with pytest.raises(base.ProfileError) as exc:
        entry.spec_node({"brief": "브랜드", "spec": spec})
    assert "없다" in str(exc.value)


def test_brand_spec_node_merges_defaults(tmp_path):
    result = entry.spec_node({"brief": "브랜드 마크", "spec": _brand_spec(tmp_path)})
    spec = result["spec"]
    assert spec["allow_brand_checks"] is False
    assert spec["image_width"] == 2000
    assert "platform" not in spec  # web 기본값이 새지 않는다


@pytest.mark.parametrize(
    "brand",
    [
        {"name": "x"},
        {"mark_path": "not-a-file.svg"},
        {"mark_path": "/tmp/mark.pdf"},
        {"mark_path": "MARK", "palette": {"noise": "#fff"}},
        {"mark_path": "MARK", "palette": {"ink": "black"}},
        {"mark_path": "MARK", "variants": {"sizes": []}},
        {"mark_path": "MARK", "variants": {"sizes": ["big"]}},
        {"mark_path": "MARK", "variants": {"sizes": [8, 9999]}},
        {"mark_path": "MARK", "variants": {"backgrounds": ["sepia"]}},
        {"mark_path": "MARK", "variants": "all"},
    ],
)
def test_brand_spec_validation(tmp_path, brand):
    if brand.get("mark_path") == "MARK":
        brand = {**brand, "mark_path": str(_mark(tmp_path))}
    with pytest.raises(base.ProfileError):
        brand_profile.brand_spec({"brand": brand})


def test_brand_spec_normalizes_palette_and_sizes(tmp_path):
    spec = _brand_spec(
        tmp_path,
        brand={
            "name": "소나기 로고",
            "mark_path": str(_mark(tmp_path)),
            "palette": {"ink": "#ABC", "quiz": None},
            "variants": {"sizes": [64, 16, 64], "backgrounds": ["dark", "light"]},
        },
    )
    # 알 수 없는 palette 키는 조용히 버리지 않는다.
    with pytest.raises(base.ProfileError):
        brand_profile.brand_spec(spec)

    spec["brand"]["palette"] = {"ink": "#ABC"}
    brand = brand_profile.brand_spec(spec)
    assert brand["palette"]["ink"] == "#aabbcc"
    assert brand["palette"]["paper"] == brand_profile.DEFAULT_PALETTE["paper"]
    assert brand["sizes"] == [16, 64]
    assert brand["backgrounds"] == ["dark", "light"]
    assert brand["name"].startswith("brand-")
    assert brand["sha256"] == core_artifacts.sha256_file(Path(brand["mark_path"]))


def test_brand_plan_is_one_deterministic_variant(tmp_path):
    profile = base.get_profile("brand")
    spec = _brand_spec(
        tmp_path,
        brand={
            "name": "sonagi",
            "mark_path": str(_mark(tmp_path)),
        },
    )
    first = profile.plan({}, spec, [])
    second = profile.plan({}, spec, [])
    assert [v["id"] for v in first] == [v["id"] for v in second] == ["brand-sonagi"]
    assert first[0]["brand"]["sizes"] == list(brand_profile.DEFAULT_SIZES)


# ---------------------------------------------------------------------------
# normalize — 순수 파이썬
# ---------------------------------------------------------------------------


def test_normalize_strips_style_and_var_and_expands_classes():
    out = brand_profile.normalize(MARK_SVG)
    assert "<style" not in out
    assert "var(" not in out
    assert 'class="' not in out

    rect = re.search(r"<rect[^>]*>", out).group(0)
    assert 'fill="#fbf9f5"' in rect
    assert 'stroke="#161616"' in rect
    assert 'stroke-width="4"' in rect
    assert "data-" not in rect

    text = re.search(r"<text[^>]*>", out).group(0)
    assert 'fill="#161616"' in text
    assert 'font-size="28px"' in text
    assert 'font-weight="700"' in text
    assert 'letter-spacing="2px"' in text
    assert "Archivo" in text

    path = re.search(r"<path[^>]*>", out).group(0)
    assert 'fill="#b4551f"' in path  # path.accent 태그+클래스 규칙


def test_normalize_keeps_text_for_outline_step():
    """정규화는 텍스트를 지우지 않는다 — 아웃라인은 별도 단계다."""
    assert brand_profile.normalize(MARK_SVG).count("<text") == 1


def test_normalize_report_counts_before_and_after():
    out = brand_profile.normalize(MARK_SVG)
    report = brand_profile.normalize_report(MARK_SVG, out)
    assert report["before"]["text_elements"] == 1
    assert report["before"]["style_elements"] == 1
    assert report["before"]["css_var_refs"] == 4
    assert report["after"]["text_elements"] == 1
    assert report["after"]["style_elements"] == 0
    assert report["after"]["css_var_refs"] == 0
    assert report["after"]["class_attributes"] == 0


def test_normalize_ignores_dark_root_values():
    out = brand_profile.normalize(MARK_SVG)
    assert "#ffffff" not in out.lower()
    assert "#161616" in out


def test_normalize_inline_style_beats_class_and_is_idempotent():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        "<style>.a { fill: #111111; }</style>"
        '<rect class="a" style="fill: #222222" width="10" height="10"/>'
        "</svg>"
    )
    out = brand_profile.normalize(svg)
    assert 'fill="#222222"' in out
    assert brand_profile.normalize(out) == out


def test_normalize_leaves_unresolved_var_for_the_gate():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        "<style>.a { fill: var(--missing); }</style>"
        '<rect class="a"/></svg>'
    )
    out = brand_profile.normalize(svg)
    assert "var(--missing)" in out
    assert brand_profile.svg_stats(out)["css_var_refs"] == 1


def test_normalize_uses_fallback_and_resolves_var_chains():
    variables, rules = brand_profile.parse_css(
        ":root { --a: var(--b); --b: #010203; } .x { fill: var(--a, #ff0000); }"
    )
    assert variables["--a"] == "#010203"
    assert rules[("*", "x")]["fill"] == "var(--a, #ff0000)"
    assert brand_profile._resolve_var("var(--a, #ff0000)", variables) == "#010203"
    assert brand_profile._resolve_var("var(--gone, #ff0000)", variables) == "#ff0000"


# ---------------------------------------------------------------------------
# outline — CLI 누락 시 실행형 안내
# ---------------------------------------------------------------------------


def test_outline_reports_missing_cli_actionably(tmp_path):
    normalized = _mark(tmp_path, name="normalized.svg")
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.outline(
            normalized,
            tmp_path / "outlined.svg",
            run=_fake_brand_run(fail_outline=True),
        )
    message = str(exc.value)
    assert "vectorcraft-cli" in message
    assert "VECTORCRAFT_BIN" in message


def test_outline_requires_output_file(tmp_path):
    normalized = _mark(tmp_path, name="normalized.svg")
    with pytest.raises(base.ProfileError) as exc:
        brand_profile.outline(
            normalized, tmp_path / "outlined.svg", run=lambda *a, **k: ""
        )
    assert "아웃라인 결과가 없다" in str(exc.value)


# ---------------------------------------------------------------------------
# 게이트 (fail closed)
# ---------------------------------------------------------------------------


def _gate_svg(tmp_path, text):
    path = tmp_path / f"mark-{abs(hash(text))}.svg"
    path.write_text(text, encoding="utf-8")
    return path


def test_brand_gates_pass_on_clean_outlined_svg(tmp_path):
    svg = _gate_svg(
        tmp_path, '<svg width="512" height="512"><path d="M0 0h1v1z"/></svg>'
    )
    pdf = tmp_path / "mark.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    calls = []
    results = brand_profile.run_gates(
        base.ProfileContext(run=_fake_brand_run(calls=calls)),
        svg=svg,
        pdf=pdf,
        workdir=tmp_path / "gate",
    )
    assert [item["id"] for item in results] == [
        "brand.no_text",
        "brand.no_style",
        "brand.raster",
        "brand.pdf",
    ]
    assert all(item["ok"] for item in results)
    assert "%" in results[2]["detail"]  # 불투명 픽셀 비율
    # 256px 상자를 넘지 않는 배율로 래스터화한다.
    assert calls[0][0].endswith("vectorcraft-cli") or "vectorcraft" in calls[0][0]
    assert calls[0][-1] == "0.5000"


def test_brand_gates_fail_on_leftover_text(tmp_path):
    svg = _gate_svg(tmp_path, '<svg><text x="1" y="1">Sonagi</text></svg>')
    pdf = tmp_path / "mark.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(brand_profile.BrandGateError) as exc:
        brand_profile.run_gates(
            base.ProfileContext(run=_fake_brand_run()),
            svg=svg,
            pdf=pdf,
            workdir=tmp_path / "gate",
        )
    assert "brand.no_text" in str(exc.value)
    assert "outline-text" in str(exc.value)


def test_brand_gates_fail_on_leftover_style_and_var(tmp_path):
    svg = _gate_svg(
        tmp_path,
        '<svg><style>.a{fill:var(--x)}</style><rect class="a"/></svg>',
    )
    pdf = tmp_path / "mark.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(brand_profile.BrandGateError) as exc:
        brand_profile.run_gates(
            base.ProfileContext(run=_fake_brand_run()),
            svg=svg,
            pdf=pdf,
            workdir=tmp_path / "gate",
        )
    message = str(exc.value)
    assert "brand.no_style" in message
    assert "<style> 1개" in message and "var() 1개" in message


def test_brand_gates_fail_on_blank_render_e015(tmp_path):
    svg = _gate_svg(
        tmp_path, '<svg width="512" height="512"><path d="M0 0h1v1z"/></svg>'
    )
    pdf = tmp_path / "mark.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    with pytest.raises(brand_profile.BrandGateError) as exc:
        brand_profile.run_gates(
            base.ProfileContext(run=_fake_brand_run(paint=False)),
            svg=svg,
            pdf=pdf,
            workdir=tmp_path / "gate",
        )
    assert "brand.raster" in str(exc.value)
    assert "E015" in str(exc.value)


def test_brand_gates_fail_on_missing_or_empty_pdf(tmp_path):
    svg = _gate_svg(
        tmp_path, '<svg width="512" height="512"><path d="M0 0h1v1z"/></svg>'
    )
    ctx = base.ProfileContext(run=_fake_brand_run())
    with pytest.raises(brand_profile.BrandGateError) as exc:
        brand_profile.run_gates(
            ctx, svg=svg, pdf=tmp_path / "nope.pdf", workdir=tmp_path / "gate"
        )
    assert "brand.pdf" in str(exc.value)

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(brand_profile.BrandGateError) as exc:
        brand_profile.run_gates(
            base.ProfileContext(run=_fake_brand_run(pdf_bytes=b"")),
            svg=svg,
            pdf=empty,
            workdir=tmp_path / "gate",
        )
    assert "0바이트" in str(exc.value)


def test_brand_gates_list_every_failed_check_at_once(tmp_path):
    svg = _gate_svg(
        tmp_path,
        '<svg><style>.a{fill:var(--x)}</style><text x="1" y="1">x</text></svg>',
    )
    with pytest.raises(brand_profile.BrandGateError) as exc:
        brand_profile.run_gates(
            base.ProfileContext(run=_fake_brand_run(paint=False)),
            svg=svg,
            pdf=tmp_path / "missing.pdf",
            workdir=tmp_path / "gate",
            pdf_error="chromium not found",
        )
    message = str(exc.value)
    assert "4건" in message
    for gate_id in ("brand.no_text", "brand.no_style", "brand.raster", "brand.pdf"):
        assert gate_id in message
    assert "chromium not found" in message
    assert "allow_brand_checks" in message


def test_allow_brand_checks_marks_failures_skipped(tmp_path):
    svg = _gate_svg(tmp_path, '<svg><text x="1" y="1">x</text></svg>')
    results = brand_profile.run_gates(
        base.ProfileContext(run=_fake_brand_run(paint=False)),
        svg=svg,
        pdf=tmp_path / "missing.pdf",
        workdir=tmp_path / "gate",
        allow=True,
    )
    failed = [item for item in results if not item["ok"]]
    assert failed and all(item["skipped"] for item in failed)


def test_brand_pdf_gate_is_skipped_when_not_requested(tmp_path):
    svg = _gate_svg(tmp_path, '<svg><path d="M0 0h1v1z"/></svg>')
    results = brand_profile.run_gates(
        base.ProfileContext(run=_fake_brand_run()),
        svg=svg,
        pdf=tmp_path / "missing.pdf",
        workdir=tmp_path / "gate",
        pdf_required=False,
    )
    pdf = next(item for item in results if item["id"] == "brand.pdf")
    assert pdf["ok"] is True and pdf["skipped"] is True


def test_brand_gate_descriptors():
    gates = base.get_profile("brand").gates()
    assert [gate.id for gate in gates] == [
        "brand.no_text",
        "brand.no_style",
        "brand.raster",
        "brand.pdf",
    ]
    assert all(gate.required for gate in gates)


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------


def test_brand_pack_writes_every_deliverable(tmp_path):
    spec = _brand_spec(tmp_path)
    profile, rendered, result = _collect(None, spec, tmp_path)
    pack_dir = Path(result["dir"])
    item = result["items"][0]

    for name in (
        "normalized.svg",
        "outlined.svg",
        "mark.pdf",
        "mark-2000.png",
        "mark.jpg",
    ):
        assert (pack_dir / name).is_file(), name
    assert (pack_dir / "meta.json").is_file()
    assert (pack_dir / "entry.md").is_file()

    assert "<text" not in (pack_dir / "outlined.svg").read_text(encoding="utf-8")
    assert "<style" not in (pack_dir / "normalized.svg").read_text(encoding="utf-8")

    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["profile"] == "brand"
    assert meta["name"] == "sonagi"
    assert meta["gates_ok"] is True
    assert meta["allow_brand_checks"] is False
    assert meta["mark_sha256"] == core_artifacts.sha256_file(
        Path(spec["brand"]["mark_path"])
    )
    assert meta["inputs_sha256"] == core_artifacts.sha256_json(
        {
            "mark_sha256": meta["mark_sha256"],
            "palette": meta["palette"],
            "sizes": meta["sizes"],
            "backgrounds": meta["backgrounds"],
            "image_width": 2000,
            "max_height": 3000,
            "jpg_quality": 90,
            "pdf": True,
        }
    )
    assert meta["normalization"]["before"]["style_elements"] == 1
    assert meta["normalization"]["after"]["style_elements"] == 0
    assert meta["normalization"]["after"]["css_var_refs"] == 0
    assert meta["normalization"]["outlined"]["text_elements"] == 0
    for gate in meta["gates"]:
        assert gate["ok"] is True and gate["skipped"] is False
    assert all(row["sha256"] for row in meta["artifacts"].values())
    assert item["pdf_size"] > 0

    icons = meta["icons"]
    assert len(icons) == len(meta["sizes"]) * len(meta["backgrounds"])
    assert {row["background"] for row in icons} == {"light", "dark"}
    for row in icons:
        assert Path(row["path"]).is_file()
        assert Path(row["path"]).stat().st_size > 0

    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["kind"] == "brand"
    assert manifest["status"] == "ok"
    assert len(manifest["gates"]) == 4
    assert manifest["artifacts"]


def test_brand_pack_fails_closed_before_writing_meta(tmp_path):
    spec = _brand_spec(tmp_path)
    profile = base.get_profile("brand")
    variant = profile.plan({}, spec, [])[0]
    ctx = _ctx(tmp_path, run=_fake_brand_run(paint=False))
    rendered = profile.render(variant, ctx)

    with pytest.raises(brand_profile.BrandGateError):
        profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})
    assert not (tmp_path / "run" / "pack" / "meta.json").exists()


def test_brand_pack_allow_checks_completes_with_warn_status(tmp_path):
    spec = _brand_spec(tmp_path, allow_brand_checks=True)
    profile = base.get_profile("brand")
    variant = profile.plan({}, spec, [])[0]
    ctx = _ctx(tmp_path, run=_fake_brand_run(paint=False))
    rendered = profile.render(variant, ctx)
    result = profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})

    meta = json.loads(Path(result["meta"]).read_text(encoding="utf-8"))
    assert meta["gates_ok"] is False
    assert meta["allow_brand_checks"] is True
    raster = next(gate for gate in meta["gates"] if gate["id"] == "brand.raster")
    assert raster["skipped"] is True
    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "warn"


def test_allow_brand_checks_requires_strict_true(tmp_path):
    spec = _brand_spec(tmp_path, allow_brand_checks="false")
    profile = base.get_profile("brand")
    variant = profile.plan({}, spec, [])[0]
    ctx = _ctx(tmp_path, run=_fake_brand_run(paint=False))
    rendered = profile.render(variant, ctx)
    with pytest.raises(brand_profile.BrandGateError):
        profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})


def test_brand_pack_is_deterministic(tmp_path):
    """L3 — 같은 입력 두 번이면 산출물 해시가 같다(경로만 다르다)."""
    spec = _brand_spec(tmp_path)
    profile = base.get_profile("brand")
    variant = profile.plan({}, spec, [])[0]
    digests = []
    for run_name in ("run-a", "run-b"):
        ctx = base.ProfileContext(
            run=_fake_brand_run(),
            convert_png=_fake_convert,
            to_jpg=_fake_jpg,
            run_dir=tmp_path / run_name,
        )
        rendered = profile.render(variant, ctx)
        result = profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})
        meta = json.loads(Path(result["meta"]).read_text(encoding="utf-8"))
        digests.append({name: row["sha256"] for name, row in meta["artifacts"].items()})
    assert digests[0] == digests[1]
    assert digests[0]


def test_brand_pack_refuses_empty_or_stale_selection(tmp_path):
    spec = _brand_spec(tmp_path)
    profile = base.get_profile("brand")
    ctx = _ctx(tmp_path)
    with pytest.raises(base.ProfileError):
        profile.pack([], spec, {"profile_ctx": ctx, "spec": spec})

    variant = profile.plan({}, spec, [])[0]
    rendered = profile.render(variant, ctx)
    with pytest.raises(base.ProfileError) as exc:
        profile.pack(
            [rendered],
            spec,
            {
                "profile_ctx": ctx,
                "spec": spec,
                "selection": [variant["id"], "gone-variant"],
            },
        )
    assert "selection" in str(exc.value)


def test_brand_pack_pdf_is_optional(tmp_path):
    spec = _brand_spec(
        tmp_path,
        brand={
            "name": "sonagi",
            "mark_path": str(_mark(tmp_path)),
            "pdf": False,
            "variants": {"sizes": [16], "backgrounds": ["light"]},
        },
    )
    profile, rendered, result = _collect(None, spec, tmp_path)
    pack_dir = Path(result["dir"])
    assert not (pack_dir / "mark.pdf").exists()
    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["pdf_requested"] is False
    pdf = next(gate for gate in meta["gates"] if gate["id"] == "brand.pdf")
    assert pdf["ok"] is True and pdf["skipped"] is True
    assert meta["gates_ok"] is True


# ---------------------------------------------------------------------------
# 목업
# ---------------------------------------------------------------------------


def test_brand_mockups_are_deterministic(tmp_path):
    from PIL import Image

    spec = _brand_spec(tmp_path)
    profile, rendered, result = _collect(None, spec, tmp_path)
    items = result["items"]
    mock_spec = {**spec, "mockup_dir": str(tmp_path / "mockups")}

    first = profile.mockups(items, mock_spec)
    second = profile.mockups(items, mock_spec)

    assert first[0]["card_sha256"] == second[0]["card_sha256"]
    assert first[0]["tile_sha256"] == second[0]["tile_sha256"]
    for key in ("card", "tile"):
        assert Path(first[0][key]).is_file()
        assert Image.open(first[0][key]).size == (1400, 900)


def test_brand_mockups_empty_items():
    assert base.get_profile("brand").mockups([], {}) == []


# ---------------------------------------------------------------------------
# entry_pack 위임 — 노드 → profile 훅 전파
# ---------------------------------------------------------------------------


def test_brand_runs_through_entry_pack_nodes(tmp_path, monkeypatch):
    spec = _brand_spec(tmp_path)
    monkeypatch.setattr(entry, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(entry, "_run", _fake_brand_run())
    monkeypatch.setattr(
        entry, "_convert_png", lambda s, o, w, m=3000: _fake_convert(s, o, w, m)
    )
    monkeypatch.setattr(entry, "_to_jpg", lambda p, o, q=90: _fake_jpg(p, o, q))

    state = entry.spec_node(
        {"brief": "브랜드 마크 납품", "spec": spec, "run_id": "brand-run"}
    )
    assert state["spec"]["skip_llm"] is True

    profile = base.profile_for_spec(state["spec"])
    variants = profile.plan(state["ir"], state["spec"], [])
    rendered = entry.render_node(
        {"spec": state["spec"], "variants": variants, "run_id": "brand-run"}
    )["renders"]
    assert rendered and Path(rendered[0]["normalized"]).is_file()

    critiques = entry.critic_node({"spec": state["spec"], "renders": rendered})[
        "critiques"
    ]
    assert [row["verdict"] for row in critiques] == ["pass"]

    options = entry.gate_node(
        {"critiques": critiques, "renders": rendered, "spec": state["spec"]}
    )["options"]
    assert [o["id"] for o in options] == ["brand-sonagi"]

    selection = entry.hitl_node({"options": options, "approval_status": "approved"})[
        "selection"
    ]
    assert selection == ["brand-sonagi"]

    packed = entry.pack_node(
        {
            "spec": state["spec"],
            "run_id": "brand-run",
            "renders": rendered,
            "options": options,
            "selection": selection,
        }
    )
    meta = json.loads(Path(packed["entry"]["meta"]).read_text(encoding="utf-8"))
    assert meta["gates_ok"] is True

    mocked = entry.mockup_node({"spec": state["spec"], "entry": packed["entry"]})
    assert mocked["mockups"] and Path(mocked["mockups"][0]["card"]).is_file()
