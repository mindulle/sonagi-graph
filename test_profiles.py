"""profiles 프레임워크(v1) — 레지스트리·web 위임·print 게이트/팩 (GRAPH-17).

전부 오프라인이다. 외부 프로세스는 `core.exec` 훅 monkeypatch로만 흉내 낸다.
gs·chromium·네트워크·폰트 설치를 요구하지 않는다.
"""

import json
import re
from pathlib import Path

import pytest

from graphs import entry_pack_graph as entry
from graphs.core import artifacts as core_artifacts
from graphs.profiles import base
from graphs.profiles import print as print_profile
from graphs.profiles.web import WebProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _package(**overrides):
    package = {
        "type": "label",
        "brand": "SONAGI",
        "product": "비 오는 날의 차",
        "net": "50g · 10 티백",
        "ingredients": "녹차 100% (국산) · 보관: 서늘한 곳",
        "origin": "MADE IN KOREA",
        "size_mm": [90, 60],
        "barcode": "8801234567890",
        "colors": {
            "ink": "#161616",
            "paper": "#fbf9f5",
            "accent": "#b4551f",
            "calm": "#1f4536",
        },
    }
    package.update(overrides)
    return package


def _print_spec(**overrides):
    spec = {
        "profile": "print",
        "package": _package(),
        "image_width": 400,
        "max_height": 600,
        "jpg_quality": 90,
    }
    spec.update(overrides)
    return spec


def _pdffonts(*rows, name_width=36):
    header = (
        f"{'name':{name_width}s} {'type':17s} {'encoding':16s} {'emb':3s} "
        f"{'sub':3s} {'uni':3s} object ID"
    )
    separator = (
        f"{'-' * name_width} {'-' * 17} {'-' * 16} {'-' * 3} "
        f"{'-' * 3} {'-' * 3} {'-' * 9}"
    )
    lines = [header, separator]
    for name, emb in rows:
        lines.append(
            f"{name:{name_width}s} {'TrueType':17s} {'WinAnsi':16s} {emb:3s} "
            f"{'no':3s} {'no':3s} 8 0"
        )
    return "\n".join(lines) + "\n"


def _fake_print_run(
    *,
    pdf_bytes=b"%PDF-1.4 print",
    fonts=None,
    ink=" 0.00000  0.00000  0.00000  0.16700 CMYK\n",
    fail_export=False,
    fail_check=False,
    calls=None,
):
    """export/check/pdffonts/gs를 흉내 내는 ctx.run 훅."""

    def fake_run(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        if calls is not None:
            calls.append(argv)
        program = Path(argv[0]).name
        script = Path(argv[1]).name if len(argv) > 1 else ""
        if script == "export_print.py":
            if "--check" in argv:
                if fail_check:
                    raise RuntimeError("도련 부족: 1mm")
                return ""
            if fail_export:
                raise RuntimeError("chromium not found")
            out = Path(argv[argv.index("--out") + 1])
            src = Path(argv[argv.index("--src") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{src.stem}.pdf").write_bytes(pdf_bytes)
            return ""
        if program == "pdffonts":
            return fonts if fonts is not None else _pdffonts(("ABCDEF+Archivo", "yes"))
        if program == "gs":
            if ink is None:
                raise RuntimeError("gs: invalid pdf")
            return ink
        return ""

    return fake_run


def _fake_convert(svg, out, width, max_height=3000):
    from PIL import Image

    Image.new("RGB", (400, 267), (250, 249, 245)).save(out, "PNG")


def _fake_jpg(png, out, quality=90):
    Path(out).write_bytes(b"jpg")


# ---------------------------------------------------------------------------
# Framework — 레지스트리·해석 순서
# ---------------------------------------------------------------------------


def test_registry_has_builtin_profiles():
    assert base.get_profile("web").id == "web"
    assert base.get_profile("print").id == "print"
    assert isinstance(base.get_profile("WEB"), WebProfile)
    assert {"print", "web"} <= set(base.known_profiles())


def test_profile_resolution_order():
    assert base.resolve_profile_name({}) == "web"
    assert base.resolve_profile_name({"profile": "print"}) == "print"
    assert base.resolve_profile_name({"contest_type": "print"}) == "print"
    # profile이 contest_type보다 우선한다.
    assert (
        base.resolve_profile_name({"profile": "web", "contest_type": "print"}) == "web"
    )
    assert base.profile_for_spec({"contest_type": "print"}).id == "print"


def test_unknown_profile_error_is_actionable():
    with pytest.raises(base.ProfileError) as exc:
        base.get_profile("brand")
    message = str(exc.value)
    assert "brand" in message
    assert "web" in message and "print" in message
    assert "spec['profile']" in message


def test_unknown_profile_stops_spec_node():
    with pytest.raises(base.ProfileError):
        entry.spec_node({"brief": "x", "spec": {"contest_type": "brand"}})


def test_print_spec_node_fails_fast_without_package():
    """knowledge·LLM 호출 전에 package 계약을 확인한다."""
    with pytest.raises(base.ProfileError):
        entry.spec_node({"brief": "라벨 인쇄", "spec": {"profile": "print"}})


def test_print_defaults_are_merged_by_spec_node():
    result = entry.spec_node(
        {
            "brief": "차분한 인쇄 라벨",
            "spec": {"profile": "print", "package": _package()},
        }
    )
    assert result["spec"]["allow_print_checks"] is False
    assert result["spec"]["bleed_mm"] == 3.0
    # web 기본값이 print spec에 새지 않는다.
    assert "platform" not in result["spec"]


# ---------------------------------------------------------------------------
# Web — 동작 불변 + 위임
# ---------------------------------------------------------------------------


def test_web_defaults_match_entry_pack_defaults():
    assert base.get_profile("web").defaults() == entry.DEFAULT_SPEC


def test_web_plan_matches_entry_pack_node(monkeypatch):
    """노드를 거친 변주 id가 profile 직접 호출과 같다(동작 불변)."""
    styles = [
        {"id": f"s{i}", "name": f"style-{i}", "axes": {"palette": "editorial"}}
        for i in range(6)
    ]
    monkeypatch.setattr(entry.craft, "load_registry", lambda _dir: styles)
    state = {
        "brief": "차분한 편집 디자인",
        "ir": {"tones": ["차분"], "media": "web", "purpose": "랜딩"},
        "directions": [
            {"title": "A", "description": "a", "motifs": []},
            {"title": "B", "description": "b", "motifs": []},
        ],
        "spec": {
            "seed": "phase4",
            "styles_per_direction": 2,
            "breakpoints": ["desktop"],
            "themes": ["light", "dark"],
            "variant_cap": 24,
        },
    }
    via_node = entry.variants_node(state)["variants"]
    via_profile = base.get_profile("web").plan(
        {**state["ir"], "directions": state["directions"]}, state["spec"], styles
    )
    assert [v["id"] for v in via_node] == [v["id"] for v in via_profile]
    assert via_node


def test_entry_pack_delegates_to_resolved_profile(monkeypatch, tmp_path):
    """entry_pack 노드가 profile에만 위임하는지(중복 로직 없음) 확인한다."""
    seen = {}

    class Stub:
        id = "web"

        def defaults(self):
            return {}

        def plan(self, ir, spec, styles):
            seen["plan"] = (spec.get("seed"), len(styles))
            return [{"id": "stub-variant"}]

        def render(self, variant, ctx):
            seen["render"] = variant["id"]
            return None

        def gates(self):
            return []

        def pack(self, items, spec, state):
            seen["pack"] = len(items)
            return {
                "dir": "x",
                "items": [],
                "meta": "",
                "manifest": "",
                "manifest_digest": "",
            }

        def mockups(self, items, spec):
            return []

    monkeypatch.setattr(base, "_REGISTRY", {"web": Stub()})
    monkeypatch.setattr(entry, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(
        entry.craft, "load_registry", lambda _dir: [{"id": "s", "name": "s"}]
    )

    variants = entry.variants_node(
        {
            "brief": "차분한",
            "ir": {"tones": ["차분"]},
            "directions": [{"title": "A", "description": "a", "motifs": []}],
            "spec": {"seed": "s1"},
        }
    )["variants"]
    assert variants == [{"id": "stub-variant"}]
    assert seen["plan"] == ("s1", 1)

    assert (
        entry.render_node({"spec": {}, "variants": [{"id": "stub-variant"}]})["renders"]
        == []
    )
    assert seen["render"] == "stub-variant"

    result = entry.pack_node(
        {
            "spec": {},
            "run_id": "delegated",
            "selection": ["stub-variant"],
            "renders": [{"id": "stub-variant", "svg": "/tmp/x.svg"}],
        }
    )
    assert result["status"] == "packed"
    assert seen["pack"] == 1


def test_print_candidate_survives_critic_gate_and_hitl(monkeypatch):
    """print는 자체 게이트가 있으므로 web 린트로 후보를 떨어뜨리지 않는다."""
    monkeypatch.setattr(
        entry,
        "_run",
        lambda *a, **k: pytest.fail("print 변주에 web 린트를 돌리면 선택지가 사라진다"),
    )
    render = {
        "id": "label-sonagi-90x60",
        "svg": "/tmp/label.svg",
        "direction": "SONAGI",
        "style": "print",
        "theme": "print",
    }
    spec = _print_spec()
    critiques = entry.critic_node({"spec": spec, "renders": [render]})["critiques"]
    assert [c["verdict"] for c in critiques] == ["pass"]
    assert critiques[0]["findings"] == []

    options = entry.gate_node(
        {"critiques": critiques, "renders": [render], "spec": spec}
    )["options"]
    assert [o["id"] for o in options] == ["label-sonagi-90x60"]

    resumed = entry.hitl_node({"options": options, "approval_status": "approved"})
    assert resumed["selection"] == ["label-sonagi-90x60"]


# ---------------------------------------------------------------------------
# Print — plan·render
# ---------------------------------------------------------------------------


def test_print_plan_is_deterministic():
    profile = base.get_profile("print")
    spec = _print_spec()
    first = profile.plan({"tones": ["차분"]}, spec, [])
    second = profile.plan({"tones": ["차분"]}, spec, [])
    assert [v["id"] for v in first] == [v["id"] for v in second]
    variant = first[0]
    assert variant["id"] == "label-sonagi-90x60"
    assert variant["kind"] == "label"
    assert variant["size_mm"] == [90.0, 60.0]


def test_print_plan_pouch_id_changes_with_size():
    profile = base.get_profile("print")
    spec = _print_spec(package=_package(type="pouch", size_mm=[120, 180]))
    variant = profile.plan({}, spec, [])[0]
    assert variant["id"] == "pouch-sonagi-120x180"
    assert variant["kind"] == "pouch"


def test_non_ascii_brand_gets_stable_distinct_slug():
    profile = base.get_profile("print")
    first = profile.plan({}, _print_spec(package=_package(brand="소나기")), [])[0]
    second = profile.plan({}, _print_spec(package=_package(brand="소나기")), [])[0]
    other = profile.plan({}, _print_spec(package=_package(brand="민들레")), [])[0]
    assert first["id"] == second["id"]
    assert first["id"].startswith("label-brand-")
    assert first["id"] != other["id"]


@pytest.mark.parametrize(
    "package",
    [
        None,
        _package(type="box"),
        _package(size_mm=[90]),
        _package(size_mm=[0, 60]),
        _package(mark_path="/tmp/does-not-exist.svg"),
    ],
)
def test_print_package_validation(package):
    with pytest.raises(base.ProfileError):
        print_profile.package_spec({"package": package} if package else {})


def test_print_render_label_delegates_to_assets_script(tmp_path):
    calls = []

    def fake_run(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        calls.append(argv)
        out = Path(argv[argv.index("--out") + 1])
        name = argv[argv.index("--name") + 1]
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{name}.svg").write_text(
            '<svg width="340.2" height="226.8"></svg>', encoding="utf-8"
        )
        (out / f"{name}.manifest.json").write_text("{}", encoding="utf-8")
        return ""

    profile = base.get_profile("print")
    variant = profile.plan({}, _print_spec(), [])[0]
    ctx = base.ProfileContext(run=fake_run, run_dir=tmp_path / "run")
    rendered = profile.render(variant, ctx)

    assert Path(calls[0][1]).name == "render_label.py"
    assert Path(rendered["svg"]).is_file()
    assert Path(rendered["spec"]).is_file()
    spec_payload = json.loads(Path(rendered["spec"]).read_text(encoding="utf-8"))
    assert spec_payload["brand"] == "SONAGI"


def test_print_render_pouch_hits_no_process_and_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setattr(
        print_profile, "_run", lambda *a, **k: pytest.fail("파우치는 실행이 필요 없다")
    )
    profile = base.get_profile("print")
    spec = _print_spec(package=_package(type="pouch"))
    variant = profile.plan({}, spec, [])[0]
    ctx = base.ProfileContext(run=print_profile._run, run_dir=tmp_path / "run")

    first = profile.render(variant, ctx)
    first_bytes = Path(first["svg"]).read_bytes()
    second = profile.render(variant, ctx)
    second_bytes = Path(second["svg"]).read_bytes()

    assert first_bytes == second_bytes
    text = first_bytes.decode("utf-8")
    for layer in ('id="dieline"', 'id="fold"', 'id="art"', 'id="barcode"'):
        assert layer in text
    assert 'id="barcode-quiet"' in text
    manifest = json.loads(Path(second["manifest"]).read_text(encoding="utf-8"))
    assert manifest["layers"] == ["page", "dieline", "fold", "art", "barcode"]


def test_pouch_svg_keeps_safe_margins_and_min_font():
    package = print_profile.package_spec(_print_spec(package=_package(type="pouch")))
    svg, manifest = print_profile.build_pouch_svg(package)
    # 최소 폰트 8px = 6pt (export_print P005 기준)
    assert min(float(v) for v in re.findall(r'font-size="([\d.]+)"', svg)) == 8.0
    # 재단 크기(mm)가 캔버스 px로 정확히 환산된다.
    assert manifest["canvas_px"] == [
        round(90 * print_profile.MM, 1),
        round(60 * print_profile.MM, 1),
    ]


def test_pouch_svg_escapes_user_text():
    import xml.etree.ElementTree as ET

    package = print_profile.package_spec(
        _print_spec(
            package=_package(brand="Salt & Pepper <Tea>", product='He said "hi"')
        )
    )
    svg, _ = print_profile.build_pouch_svg(package)
    ET.fromstring(svg)  # 깨진 XML이면 여기서 실패한다.
    assert "Salt &amp; Pepper &lt;Tea&gt;" in svg
    assert "<Tea>" not in svg


def test_label_spec_is_xml_escaped_before_script(tmp_path):
    """render_label.py가 이스케이프를 안 하므로 경계에서 한 번 처리한다."""
    import xml.etree.ElementTree as ET

    captured = {}

    def fake_run(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        payload = json.loads(Path(argv[argv.index("--spec") + 1]).read_text())
        captured["brand"] = payload["brand"]
        out = Path(argv[argv.index("--out") + 1])
        name = argv[argv.index("--name") + 1]
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{name}.svg").write_text(
            '<svg width="340.2" height="226.8"><g id="art">'
            f'<text x="10" y="20" font-size="8">{payload["brand"]}</text></g></svg>',
            encoding="utf-8",
        )
        (out / f"{name}.manifest.json").write_text("{}", encoding="utf-8")
        return ""

    spec = _print_spec(package=_package(brand="Salt & Pepper <Tea>"))
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    ctx = base.ProfileContext(run=fake_run, run_dir=tmp_path / "run")
    rendered = profile.render(variant, ctx)

    ET.fromstring(Path(rendered["svg"]).read_text(encoding="utf-8"))
    assert captured["brand"] == "Salt &amp; Pepper &lt;Tea&gt;"
    # provenance spec은 원문을 유지한다(해시·meta와 일치).
    raw = json.loads(Path(rendered["spec"]).read_text(encoding="utf-8"))
    assert raw["brand"] == "Salt & Pepper <Tea>"


def test_label_mark_is_injected_and_manifest_hash_updated(tmp_path):
    mark = tmp_path / "mark.svg"
    mark.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")

    def fake_run(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        out = Path(argv[argv.index("--out") + 1])
        name = argv[argv.index("--name") + 1]
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{name}.svg").write_text(
            '<svg width="340.2" height="226.8"><g id="art">'
            '<text x="10" y="20" font-size="8">x</text></g></svg>',
            encoding="utf-8",
        )
        (out / f"{name}.manifest.json").write_text(
            json.dumps({"sha256": "old"}), encoding="utf-8"
        )
        return ""

    spec = _print_spec(package=_package(mark_path=str(mark)))
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    ctx = base.ProfileContext(run=fake_run, run_dir=tmp_path / "run")
    rendered = profile.render(variant, ctx)

    svg = Path(rendered["svg"]).read_text(encoding="utf-8")
    assert '<image id="mark"' in svg
    data = json.loads(Path(rendered["manifest"]).read_text(encoding="utf-8"))
    assert data["sha256"] != "old"
    assert data["mark"] is True


def test_print_rejects_size_too_small_for_barcode():
    with pytest.raises(base.ProfileError) as exc:
        print_profile.package_spec(_print_spec(package=_package(size_mm=[20, 12])))
    assert "너무 작다" in str(exc.value)


# ---------------------------------------------------------------------------
# Print — 게이트 (fail closed)
# ---------------------------------------------------------------------------


def test_print_gates_pass_with_embedded_fonts_and_cmyk(tmp_path):
    pdf = tmp_path / "label.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ctx = base.ProfileContext(run=_fake_print_run(), run_dir=tmp_path / "run")
    results = print_profile.run_gates(
        ctx, svg=tmp_path / "label.svg", pdf=pdf, size_mm=[90, 60], bleed=3.0
    )
    assert [item["id"] for item in results] == [
        "print.export_check",
        "print.pdf",
        "print.fonts",
        "print.inkcov",
    ]
    assert all(item["ok"] for item in results)
    assert all(item["skipped"] is False for item in results)


def test_print_gates_fail_closed_on_unembedded_font(tmp_path):
    pdf = tmp_path / "label.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    fonts = _pdffonts(("ABCDEF+Archivo", "yes"), ("GHIJKL+Noto", "no"))
    ctx = base.ProfileContext(
        run=_fake_print_run(fonts=fonts), run_dir=tmp_path / "run"
    )
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_gates(
            ctx, svg=tmp_path / "label.svg", pdf=pdf, size_mm=[90, 60], bleed=3.0
        )
    message = str(exc.value)
    assert "print.fonts" in message
    assert "GHIJKL+Noto" in message
    assert "allow_print_checks" in message


def test_print_gates_fail_closed_when_inkcov_is_not_cmyk(tmp_path):
    pdf = tmp_path / "label.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ctx = base.ProfileContext(
        run=_fake_print_run(ink="Page 1\n"), run_dir=tmp_path / "run"
    )
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_gates(
            ctx, svg=tmp_path / "label.svg", pdf=pdf, size_mm=[90, 60], bleed=3.0
        )
    assert "print.inkcov" in str(exc.value)
    assert "CMYK" in str(exc.value)


def test_print_gates_fail_on_missing_or_empty_pdf(tmp_path):
    ctx = base.ProfileContext(run=_fake_print_run(), run_dir=tmp_path / "run")
    missing = tmp_path / "nope.pdf"
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_gates(
            ctx, svg=tmp_path / "label.svg", pdf=missing, size_mm=[90, 60], bleed=3.0
        )
    assert "print.pdf" in str(exc.value)

    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_gates(
            ctx, svg=tmp_path / "label.svg", pdf=empty, size_mm=[90, 60], bleed=3.0
        )
    assert "0바이트" in str(exc.value)


def test_print_gates_fail_on_export_check(tmp_path):
    pdf = tmp_path / "label.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ctx = base.ProfileContext(
        run=_fake_print_run(fail_check=True), run_dir=tmp_path / "run"
    )
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_gates(
            ctx, svg=tmp_path / "label.svg", pdf=pdf, size_mm=[90, 60], bleed=3.0
        )
    assert "print.export_check" in str(exc.value)
    assert "도련" in str(exc.value)


def test_allow_print_checks_marks_failures_skipped(tmp_path):
    pdf = tmp_path / "label.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ctx = base.ProfileContext(
        run=_fake_print_run(ink="Page 1\n"), run_dir=tmp_path / "run"
    )
    results = print_profile.run_gates(
        ctx,
        svg=tmp_path / "label.svg",
        pdf=pdf,
        size_mm=[90, 60],
        bleed=3.0,
        allow=True,
    )
    ink = next(item for item in results if item["id"] == "print.inkcov")
    assert ink["ok"] is False
    assert ink["skipped"] is True


def test_print_gate_descriptors():
    gates = base.get_profile("print").gates()
    assert [gate.id for gate in gates] == [
        "print.export_check",
        "print.pdf",
        "print.fonts",
        "print.inkcov",
    ]
    assert all(gate.required for gate in gates)


def test_unembedded_fonts_parser_handles_wide_names():
    long_name = "ABCDEF+" + "x" * 60
    text = _pdffonts((long_name, "no"), name_width=72)
    assert print_profile._unembedded_fonts(text) == [long_name]


def test_unembedded_fonts_parser_fails_closed_on_unknown_format(tmp_path):
    assert print_profile._unembedded_fonts("") is None
    assert print_profile._unembedded_fonts("garbage without a table") is None

    pdf = tmp_path / "label.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    ctx = base.ProfileContext(
        run=_fake_print_run(fonts="garbage without a table"), run_dir=tmp_path / "run"
    )
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_gates(
            ctx, svg=tmp_path / "label.svg", pdf=pdf, size_mm=[90, 60], bleed=3.0
        )
    assert "해석할 수 없다" in str(exc.value)


# ---------------------------------------------------------------------------
# Print — pack·mockups
# ---------------------------------------------------------------------------


def test_print_pack_writes_front_png_jpg_meta_and_entry(tmp_path):
    spec = _print_spec()
    svg = tmp_path / "label.svg"
    svg.write_text('<svg width="340.2" height="226.8"></svg>', encoding="utf-8")
    ctx = base.ProfileContext(
        run=_fake_print_run(),
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    items = [{**variant, "svg": str(svg)}]

    result = profile.pack(items, spec, {"profile_ctx": ctx, "spec": spec, "brief": "b"})
    pack_dir = Path(result["dir"])

    assert (pack_dir / "meta.json").is_file()
    assert (pack_dir / "entry.md").is_file()
    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["package_type"] == "label"
    assert meta["allow_print_checks"] is False
    assert meta["gates_ok"] is True
    assert [gate["id"] for gate in meta["gates"]] == [
        "print.export_check",
        "print.pdf",
        "print.fonts",
        "print.inkcov",
    ]
    assert meta["inputs_sha256"] == core_artifacts.sha256_json(
        print_profile.package_spec(spec)
    )

    item = result["items"][0]
    assert Path(item["png"]).is_file() and Path(item["jpg"]).is_file()
    assert Path(item["pdf"]).is_file()
    assert item["pdf_size"] > 0
    assert item["sha256"]

    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["kind"] == "print"
    assert manifest["status"] == "ok"
    assert len(manifest["gates"]) == 4
    assert manifest["artifacts"]


def test_print_pack_fails_closed_before_writing_meta(tmp_path):
    spec = _print_spec()
    svg = tmp_path / "label.svg"
    svg.write_text('<svg width="340.2" height="226.8"></svg>', encoding="utf-8")
    ctx = base.ProfileContext(
        run=_fake_print_run(ink="Page 1\n"),
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    with pytest.raises(print_profile.PrintGateError):
        profile.pack(
            [{**variant, "svg": str(svg)}],
            spec,
            {"profile_ctx": ctx, "spec": spec, "brief": "b"},
        )
    assert not (tmp_path / "run" / "pack" / "meta.json").exists()


def test_print_pack_allow_print_checks_completes_without_gates(tmp_path):
    spec = _print_spec(allow_print_checks=True)
    svg = tmp_path / "label.svg"
    svg.write_text('<svg width="340.2" height="226.8"></svg>', encoding="utf-8")
    ctx = base.ProfileContext(
        run=_fake_print_run(ink="Page 1\n"),
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    result = profile.pack(
        [{**variant, "svg": str(svg)}],
        spec,
        {"profile_ctx": ctx, "spec": spec, "brief": "b"},
    )
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["gates_ok"] is False
    assert meta["allow_print_checks"] is True
    ink = next(gate for gate in meta["gates"] if gate["id"] == "print.inkcov")
    assert ink["skipped"] is True
    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "warn"


def test_print_pack_refuses_empty_or_stale_selection(tmp_path):
    spec = _print_spec()
    ctx = base.ProfileContext(
        run=_fake_print_run(),
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )
    profile = base.get_profile("print")
    with pytest.raises(base.ProfileError):
        profile.pack([], spec, {"profile_ctx": ctx, "spec": spec})

    svg = tmp_path / "label.svg"
    svg.write_text('<svg width="340.2" height="226.8"></svg>', encoding="utf-8")
    variant = profile.plan({}, spec, [])[0]
    with pytest.raises(base.ProfileError) as exc:
        profile.pack(
            [{**variant, "svg": str(svg)}],
            spec,
            {
                "profile_ctx": ctx,
                "spec": spec,
                "selection": [variant["id"], "gone-variant"],
            },
        )
    assert "selection" in str(exc.value)


def test_allow_print_checks_requires_strict_true(tmp_path):
    spec = _print_spec(allow_print_checks="false")
    svg = tmp_path / "label.svg"
    svg.write_text('<svg width="340.2" height="226.8"></svg>', encoding="utf-8")
    ctx = base.ProfileContext(
        run=_fake_print_run(ink="Page 1\n"),
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    with pytest.raises(print_profile.PrintGateError):
        profile.pack(
            [{**variant, "svg": str(svg)}],
            spec,
            {"profile_ctx": ctx, "spec": spec},
        )


def test_entry_pack_routes_print_pack_through_entry_hooks(tmp_path, monkeypatch):
    """entry_pack의 ctx가 print에 전달돼 _run/convert 훅이 그대로 쓰인다."""
    spec = _print_spec()
    svg = tmp_path / "label.svg"
    svg.write_text('<svg width="340.2" height="226.8"></svg>', encoding="utf-8")
    calls = []
    monkeypatch.setattr(entry, "OUT_ROOT", tmp_path)
    monkeypatch.setattr(entry, "_run", _fake_print_run(calls=calls))
    monkeypatch.setattr(
        entry, "_convert_png", lambda s, o, w, m=3000: _fake_convert(s, o, w, m)
    )
    monkeypatch.setattr(entry, "_to_jpg", lambda p, o, q=90: _fake_jpg(p, o, q))

    variant = base.get_profile("print").plan({}, spec, [])[0]
    result = entry.pack_node(
        {
            "run_id": "print-entry",
            "brief": "인쇄 팩",
            "spec": spec,
            "renders": [{**variant, "svg": str(svg)}],
            "options": [{"id": variant["id"]}],
            "selection": [variant["id"]],
        }
    )
    meta = json.loads(Path(result["entry"]["meta"]).read_text(encoding="utf-8"))
    assert meta["gates_ok"] is True
    programs = {Path(argv[0]).name for argv in calls}
    assert "pdffonts" in programs and "gs" in programs


def test_print_mockups_are_deterministic(tmp_path):
    from PIL import Image

    page = tmp_path / "page.png"
    Image.new("RGB", (400, 267), (250, 249, 245)).save(page, "PNG")
    spec = _print_spec(mockup_dir=str(tmp_path / "mockups"))
    profile = base.get_profile("print")
    items = [{"id": "label-sonagi-90x60", "png": str(page)}]

    first = profile.mockups(items, spec)
    second = profile.mockups(items, spec)

    assert first[0]["flat_sha256"] == second[0]["flat_sha256"]
    assert first[0]["pouch_sha256"] == second[0]["pouch_sha256"]
    assert Path(first[0]["flat"]).is_file()
    assert Path(first[0]["pouch"]).is_file()
    assert Image.open(first[0]["flat"]).size == (1400, 900)


def test_context_from_state_prefers_explicit_context(tmp_path):
    ctx = base.ProfileContext(run=lambda *a, **k: "", run_dir=tmp_path)
    assert base.context_from_state({"profile_ctx": ctx}) is ctx
    assert isinstance(base.context_from_state({}), base.ProfileContext)
