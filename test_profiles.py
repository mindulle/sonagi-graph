"""profiles 프레임워크(v1) — 레지스트리·web 위임·print 게이트/팩 (GRAPH-17·20).

전부 오프라인이다. 외부 프로세스는 `core.exec` 훅 monkeypatch로만 흉내 낸다.
gs·chromium·네트워크·폰트 설치를 요구하지 않는다.

print는 두 단계를 시험한다.

- entry(`print_ready=False`): vectorcraft 변환 외 외부 호출 0회. 게이트는
  순수 파이썬·SVG 구조 검사.
- handoff(`print_ready=True`): export_print·pdffonts·gs까지 흉내 내는 기존 경로.
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
    # 핸드오프(인쇄소 PDF/X) 단계 — 기존 동작 그대로.
    spec = {
        "profile": "print",
        "print_ready": True,
        "package": _package(),
        "image_width": 400,
        "max_height": 600,
        "jpg_quality": 90,
    }
    spec.update(overrides)
    return spec


def _entry_spec(**overrides):
    """엔트리(콘테스트 제출) 단계 — print_ready 기본값 False."""
    return _print_spec(print_ready=False, **overrides)


#: 엔트리 단계에서 절대 실행되면 안 되는 프로그램·스크립트.
FORBIDDEN_ENTRY_PROGRAMS = {
    "gs",
    "ghostscript",
    "chromium",
    "chromium-browser",
    "chrome",
    "google-chrome",
    "pdffonts",
    "export_print.py",
}


def _record_exec(
    monkeypatch, calls, *, png_size=None, label_svg=None, real_python=False
):
    """`core.exec.run`을 기록기로 바꾼다. vectorcraft convert만 실제 PNG를 쓴다.

    엔트리 단계가 어떤 외부 프로세스도 부르지 않는지 argv 전체로 검증하기 위한
    장치다. `png_size`를 주면 변환 결과 크기를 강제로 고정한다(상한 초과 시험).
    `label_svg`를 주면 render_label.py 호출을 그 SVG로 흉내 낸다.
    `real_python=True`면 python 스크립트는 실제 core.exec로 실행한다.
    """
    from graphs.core import exec as core_exec

    real_run = core_exec.run

    def result(argv, stdout=""):
        return core_exec.RunResult(
            argv=tuple(argv),
            returncode=0,
            stdout=stdout,
            stderr="",
            duration_ms=0,
        )

    def fake_exec(args, timeout=300, **kwargs):
        argv = [str(arg) for arg in args]
        calls.append(argv)
        program = Path(argv[0]).name
        script = Path(argv[1]).name if len(argv) > 1 else ""
        if program.startswith("vectorcraft") and len(argv) > 3:
            from PIL import Image

            svg, out = Path(argv[2]), Path(argv[3])
            if png_size is None:
                scale = float(argv[argv.index("--scale") + 1])
                native_w, native_h = base.svg_size(svg)
                size = (
                    max(1, round(native_w * scale)),
                    max(1, round(native_h * scale)),
                )
            else:
                size = png_size
            Image.new("RGB", size, (250, 249, 245)).save(out, "PNG")
            return result(argv)
        if script == "render_label.py":
            if real_python:
                return real_run([str(arg) for arg in args], timeout=timeout, **kwargs)
            if label_svg is not None:
                out = Path(argv[argv.index("--out") + 1])
                name = argv[argv.index("--name") + 1]
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{name}.svg").write_text(label_svg, encoding="utf-8")
                (out / f"{name}.manifest.json").write_text("{}", encoding="utf-8")
            return result(argv)
        return result(argv)

    monkeypatch.setattr(core_exec, "run", fake_exec)
    return calls


def _assert_only_vectorcraft(calls, *, allow_python=False):
    """호출된 프로그램이 vectorcraft(와 허용된 python 스크립트)뿐인지 확인한다."""
    allowed = {"vectorcraft-cli"} | ({"python3", "python"} if allow_python else set())
    names = {Path(argv[0]).name for argv in calls}
    assert names <= allowed, f"예상 밖 프로세스: {sorted(names - allowed)}"
    if not allow_python:
        assert names == {"vectorcraft-cli"}, f"예상 밖 프로세스: {sorted(names)}"
    for argv in calls:
        flat = {Path(str(arg)).name for arg in argv}
        forbidden = flat & FORBIDDEN_ENTRY_PROGRAMS
        assert not forbidden, f"엔트리 단계가 인쇄 도구를 호출했다: {argv}"


def _minimal_entry_svg(*, fold=True, barcode_block="", dieline_tag="g"):
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="340" height="227">']
    if dieline_tag == "g":
        parts.append('<g id="dieline"><rect x="0" y="0" width="340" height="227"/></g>')
    else:
        parts.append('<rect id="dieline" x="0" y="0" width="340" height="227"/>')
    if fold:
        parts.append('<g id="fold"><line x1="10" y1="0" x2="10" y2="227"/></g>')
    parts.append(barcode_block)
    parts.append("</svg>")
    return "".join(parts)


_VALID_BARCODE = (
    '<g id="barcode">'
    '<rect id="barcode-quiet" x="1" y="1" width="10" height="10"/>'
    '<g id="barcode-bars"><rect x="3" y="3" width="1" height="6"/></g>'
    "</g>"
)


def _pouch_svg_text(**package_overrides):
    package = print_profile.package_spec(
        {"package": _package(type="pouch", **package_overrides)}
    )
    return print_profile.build_pouch_svg(package)[0]


def _write_png(path, size=(400, 267)):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (250, 249, 245)).save(path, "PNG")
    return path


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
        base.get_profile("spreadsheet")
    message = str(exc.value)
    assert "spreadsheet" in message
    assert "web" in message and "print" in message and "brand" in message
    assert "spec['profile']" in message


def test_unknown_profile_stops_spec_node():
    with pytest.raises(base.ProfileError):
        entry.spec_node({"brief": "x", "spec": {"contest_type": "spreadsheet"}})


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
    assert result["spec"]["print_ready"] is False
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
    """profile.gates()는 두 단계가 요구할 수 있는 게이트 전체를 문서화한다."""
    gates = base.get_profile("print").gates()
    assert [gate.id for gate in gates] == [
        "print.assets",
        "print.layers",
        "print.barcode",
        "print.render_size",
        "print.export_check",
        "print.pdf",
        "print.fonts",
        "print.inkcov",
    ]
    assert all(gate.required for gate in gates)
    assert [gate.id for gate in print_profile.ENTRY_GATES] == [
        "print.assets",
        "print.layers",
        "print.barcode",
        "print.render_size",
    ]
    assert [gate.id for gate in print_profile.HANDOFF_GATES] == [
        "print.export_check",
        "print.pdf",
        "print.fonts",
        "print.inkcov",
    ]


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
    assert meta["mode"] == "handoff"
    assert meta["print_ready"] is True
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
    assert meta["mode"] == "handoff"
    scripts = {Path(argv[1]).name for argv in calls if len(argv) > 1}
    programs = {Path(argv[0]).name for argv in calls}
    assert "export_print.py" in scripts
    assert "pdffonts" in programs and "gs" in programs


# ---------------------------------------------------------------------------
# Print — entry 단계 (print_ready=False): 외부 인쇄 도구 0회
# ---------------------------------------------------------------------------


def test_print_default_spec_is_entry_stage():
    assert print_profile.DEFAULT_SPEC["print_ready"] is False


def test_layer_contract_covers_every_package_type():
    """접힘선이 없는 형식은 빈 튜플로 사유를 남긴다(계약 누락은 실패)."""
    assert set(print_profile.LAYER_CONTRACT) == set(print_profile.PACKAGE_TYPES)
    for kind, layers in print_profile.LAYER_CONTRACT.items():
        assert isinstance(layers, tuple), kind
        assert "dieline" in layers or layers == (), kind


def test_entry_pack_calls_only_vectorcraft(tmp_path, monkeypatch):
    """엔트리 단계는 gs·chromium·pdffonts·export_print를 한 번도 부르지 않는다."""
    calls = _record_exec(monkeypatch, [])
    spec = _entry_spec(package=_package(type="pouch"))
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    ctx = base.ProfileContext(run_dir=tmp_path / "run")
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    result = profile.pack(
        [{**variant, "svg": str(svg)}],
        spec,
        {"profile_ctx": ctx, "spec": spec, "brief": "b"},
    )

    _assert_only_vectorcraft(calls)
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "entry"
    assert meta["print_ready"] is False
    assert meta["gates_ok"] is True
    assert [gate["id"] for gate in meta["gates"]] == [
        "print.assets",
        "print.layers",
        "print.barcode",
        "print.render_size",
    ]

    item = result["items"][0]
    assert "pdf" not in item and "pdf_size" not in item
    assert Path(item["png"]).stat().st_size > 0
    assert Path(item["jpg"]).stat().st_size > 0
    assert not list(Path(result["dir"]).glob("*.pdf"))

    entry_md = (Path(result["dir"]) / "entry.md").read_text(encoding="utf-8")
    assert "entry" in entry_md
    assert "PDF" not in entry_md
    assert "도련 미적용" in entry_md
    assert "도련 3mm" not in entry_md
    assert meta["bleed_mm"] is None
    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["kind"] == "print" and manifest["status"] == "ok"


def test_entry_label_render_then_pack_stays_offline(tmp_path, monkeypatch):
    """기본 형식(label)의 render→pack 경로도 인쇄 도구를 부르지 않는다."""
    label_svg = _minimal_entry_svg(barcode_block=_VALID_BARCODE)
    calls = _record_exec(monkeypatch, [], label_svg=label_svg)
    spec = _entry_spec()  # type 기본값 = label
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    ctx = base.ProfileContext(run_dir=tmp_path / "run")

    rendered = profile.render(variant, ctx)
    result = profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})

    _assert_only_vectorcraft(calls, allow_python=True)
    python_scripts = {
        Path(argv[1]).name
        for argv in calls
        if Path(argv[0]).name.startswith("python") and len(argv) > 1
    }
    assert python_scripts == {"render_label.py"}
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "entry" and meta["gates_ok"] is True
    assert [gate["id"] for gate in meta["gates"]] == [
        "print.assets",
        "print.layers",
        "print.barcode",
        "print.render_size",
    ]


@pytest.mark.skipif(
    not (base.ProfileContext().assets_dir / "scripts/render_label.py").is_file(),
    reason="sonagi-assets의 render_label.py가 없는 환경",
)
def test_entry_label_real_render_label_output_passes_structural_gates(
    tmp_path, monkeypatch
):
    """실제 render_label.py 산출 SVG가 dieline·fold·바코드 조용영역 계약을 지키는지."""
    calls = _record_exec(monkeypatch, [], real_python=True)
    spec = _entry_spec()
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    ctx = base.ProfileContext(run_dir=tmp_path / "run")

    rendered = profile.render(variant, ctx)
    result = profile.pack([rendered], spec, {"profile_ctx": ctx, "spec": spec})

    _assert_only_vectorcraft(calls, allow_python=True)
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["gates_ok"] is True, meta["gates"]


def test_entry_pack_through_graph_node_never_touches_print_tools(tmp_path, monkeypatch):
    """entry_pack 노드 경로 전체에서도 인쇄 도구 호출이 0회다."""
    calls = _record_exec(monkeypatch, [])
    spec = _entry_spec(package=_package(type="pouch"))
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    monkeypatch.setattr(entry, "OUT_ROOT", tmp_path)

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
    _assert_only_vectorcraft(calls)
    meta = json.loads(Path(result["entry"]["meta"]).read_text(encoding="utf-8"))
    assert meta["mode"] == "entry" and meta["gates_ok"] is True


def test_entry_pack_builds_mockups_after_pack(tmp_path, monkeypatch):
    calls = _record_exec(monkeypatch, [])
    spec = _entry_spec(package=_package(type="pouch"))
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    monkeypatch.setattr(entry, "OUT_ROOT", tmp_path)

    variant = base.get_profile("print").plan({}, spec, [])[0]
    state = {
        "run_id": "print-entry-mock",
        "brief": "인쇄 팩",
        "spec": spec,
        "renders": [{**variant, "svg": str(svg)}],
        "options": [{"id": variant["id"]}],
        "selection": [variant["id"]],
    }
    packed = entry.pack_node(state)
    mocked = entry.mockup_node({**state, "entry": packed["entry"]})
    _assert_only_vectorcraft(calls)
    assert mocked["status"] == "mocked"
    assert Path(mocked["mockups"][0]["flat"]).is_file()
    assert Path(mocked["mockups"][0]["pouch"]).is_file()


def test_entry_gate_missing_fold_layer_fails_closed(tmp_path, monkeypatch):
    _record_exec(monkeypatch, [])
    text = re.sub(r'<g id="fold".*?</g>', "", _pouch_svg_text(), flags=re.S)
    svg = tmp_path / "pouch.svg"
    svg.write_text(text, encoding="utf-8")
    spec = _entry_spec(package=_package(type="pouch"))
    ctx = base.ProfileContext(run_dir=tmp_path / "run")
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    with pytest.raises(print_profile.PrintGateError) as exc:
        profile.pack(
            [{**variant, "svg": str(svg)}],
            spec,
            {"profile_ctx": ctx, "spec": spec},
        )
    assert "print.layers" in str(exc.value)
    assert "fold" in str(exc.value)
    assert not (tmp_path / "run" / "pack" / "meta.json").exists()
    assert not (tmp_path / "run" / "pack" / "entry.md").exists()


def test_entry_gate_missing_barcode_quiet_zone_fails_closed(tmp_path):
    text = re.sub(r'<rect id="barcode-quiet"[^>]*/>', "", _pouch_svg_text(), flags=re.S)
    svg = tmp_path / "pouch.svg"
    svg.write_text(text, encoding="utf-8")
    png = _write_png(tmp_path / "front.png")
    jpg = _write_png(tmp_path / "front.jpg")

    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_entry_gates(
            svg=svg,
            png=png,
            jpg=jpg,
            package={"type": "pouch", "barcode": "8801234567890"},
            width=400,
            max_height=600,
        )
    assert "print.barcode" in str(exc.value)
    assert "조용영역" in str(exc.value)


def test_entry_barcode_gate_documents_absent_barcode(tmp_path):
    """바코드가 없는 사양은 조용영역 검사 대상이 아니다(사유를 남긴다)."""
    svg = tmp_path / "plain.svg"
    svg.write_text(_minimal_entry_svg(), encoding="utf-8")
    results = print_profile.run_entry_gates(
        svg=svg,
        png=_write_png(tmp_path / "front.png"),
        jpg=_write_png(tmp_path / "front.jpg"),
        package={"type": "label", "barcode": ""},
        width=400,
        max_height=600,
    )
    barcode = next(item for item in results if item["id"] == "print.barcode")
    assert barcode["ok"] is True
    assert "대상 아님" in barcode["detail"]


@pytest.mark.parametrize(
    "barcode_block",
    [
        # 그룹 자체가 없다 — 바코드 숫자는 있는데 그리지 않았다.
        "",
        # 조용영역 rect가 그룹 밖에 있다.
        '<g id="barcode"><g id="barcode-bars"/></g>'
        '<rect id="barcode-quiet" x="1" y="1" width="10" height="10"/>',
        # 조용영역 크기가 0이다.
        '<g id="barcode">'
        '<rect id="barcode-quiet" x="1" y="1" width="0" height="10"/>'
        "</g>",
        # quiet id가 rect가 아니다.
        '<g id="barcode"><text id="barcode-quiet" x="1" y="1">q</text></g>',
    ],
)
def test_entry_barcode_gate_rejects_broken_quiet_zones(tmp_path, barcode_block):
    svg = tmp_path / "barcode.svg"
    svg.write_text(_minimal_entry_svg(barcode_block=barcode_block), encoding="utf-8")
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_entry_gates(
            svg=svg,
            png=_write_png(tmp_path / "front.png"),
            jpg=_write_png(tmp_path / "front.jpg"),
            package={"type": "label", "barcode": "8801234567890"},
            width=400,
            max_height=600,
        )
    assert "print.barcode" in str(exc.value)


def test_entry_layer_gate_requires_groups_not_loose_elements(tmp_path):
    """`<rect id="dieline">`는 레이어 그룹이 아니다."""
    svg = tmp_path / "loose.svg"
    svg.write_text(
        _minimal_entry_svg(barcode_block=_VALID_BARCODE, dieline_tag="rect"),
        encoding="utf-8",
    )
    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_entry_gates(
            svg=svg,
            png=_write_png(tmp_path / "front.png"),
            jpg=_write_png(tmp_path / "front.jpg"),
            package={"type": "label", "barcode": "8801234567890"},
            width=400,
            max_height=600,
        )
    assert "print.layers" in str(exc.value)
    assert 'id="dieline"' in str(exc.value)


@pytest.mark.parametrize(
    ("size", "ok"),
    [
        ((400, 600), True),  # 정확히 상한
        ((401, 600), True),  # 1px 반올림 허용
        ((402, 600), False),  # 가로 초과
        ((400, 601), True),
        ((400, 602), False),  # 세로 초과
    ],
)
def test_entry_render_size_gate_boundaries(tmp_path, size, ok):
    svg = tmp_path / "plain.svg"
    svg.write_text(_minimal_entry_svg(barcode_block=_VALID_BARCODE), encoding="utf-8")
    results = print_profile.run_entry_gates(
        svg=svg,
        png=_write_png(tmp_path / "front.png", size=size),
        jpg=_write_png(tmp_path / "front.jpg"),
        package={"type": "label", "barcode": "8801234567890"},
        width=400,
        max_height=600,
        allow=not ok,
    )
    gate = next(item for item in results if item["id"] == "print.render_size")
    assert gate["ok"] is ok
    if not ok:
        assert gate["skipped"] is True


def test_entry_gate_missing_output_files_fail_closed(tmp_path):
    svg = tmp_path / "plain.svg"
    svg.write_text(_minimal_entry_svg(barcode_block=_VALID_BARCODE), encoding="utf-8")
    results = print_profile.run_entry_gates(
        svg=svg,
        png=tmp_path / "missing.png",
        jpg=tmp_path / "missing.jpg",
        package={"type": "label", "barcode": "8801234567890"},
        width=400,
        max_height=600,
        allow=True,
    )
    assets = next(item for item in results if item["id"] == "print.assets")
    assert assets["ok"] is False and assets["skipped"] is True
    assert "없음" in assets["detail"]

    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_entry_gates(
            svg=svg,
            png=tmp_path / "missing.png",
            jpg=tmp_path / "missing.jpg",
            package={"type": "label", "barcode": "8801234567890"},
            width=400,
            max_height=600,
        )
    assert "print.assets" in str(exc.value)


def test_entry_allow_print_checks_survives_missing_outputs(tmp_path):
    """allow 우회는 게이트 실패(파일 없음 포함)를 건너뛰고 meta를 쓴다."""

    def noop_convert(svg, out, width, max_height=3000):
        return None

    def noop_jpg(png, out, quality=90):
        return None

    spec = _entry_spec(allow_print_checks=True)
    svg = tmp_path / "plain.svg"
    svg.write_text(_minimal_entry_svg(barcode_block=_VALID_BARCODE), encoding="utf-8")
    ctx = base.ProfileContext(
        convert_png=noop_convert, to_jpg=noop_jpg, run_dir=tmp_path / "run"
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    result = profile.pack(
        [{**variant, "svg": str(svg)}], spec, {"profile_ctx": ctx, "spec": spec}
    )
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["gates_ok"] is False
    assert (Path(result["dir"]) / "entry.md").is_file()
    item = result["items"][0]
    assert item["png_size"] == 0 and item["jpg_size"] == 0
    assert item["sha256"] is None


def test_entry_gate_broken_svg_fails_closed(tmp_path):
    svg = tmp_path / "broken.svg"
    svg.write_text("<svg", encoding="utf-8")
    results_gate = print_profile.run_entry_gates(
        svg=svg,
        png=_write_png(tmp_path / "front.png"),
        jpg=_write_png(tmp_path / "front.jpg"),
        package={"type": "pouch", "barcode": "8801234567890"},
        width=400,
        max_height=600,
        allow=True,
    )
    layers = next(item for item in results_gate if item["id"] == "print.layers")
    assert layers["ok"] is False and layers["skipped"] is True
    assert "해석할 수 없다" in layers["detail"]

    with pytest.raises(print_profile.PrintGateError) as exc:
        print_profile.run_entry_gates(
            svg=svg,
            png=_write_png(tmp_path / "front.png"),
            jpg=_write_png(tmp_path / "front.jpg"),
            package={"type": "pouch", "barcode": "8801234567890"},
            width=400,
            max_height=600,
        )
    assert "print.layers" in str(exc.value)


def test_entry_gate_render_size_over_cap_fails_closed(tmp_path, monkeypatch):
    calls = _record_exec(monkeypatch, [], png_size=(900, 900))
    spec = _entry_spec(package=_package(type="pouch"))
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    ctx = base.ProfileContext(run_dir=tmp_path / "run")
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    with pytest.raises(print_profile.PrintGateError) as exc:
        profile.pack(
            [{**variant, "svg": str(svg)}],
            spec,
            {"profile_ctx": ctx, "spec": spec},
        )
    _assert_only_vectorcraft(calls)
    assert "print.render_size" in str(exc.value)
    assert "상한" in str(exc.value)


def test_entry_gate_zero_byte_output_fails_closed(tmp_path):
    def empty_convert(svg, out, width, max_height=3000):
        Path(out).write_bytes(b"")

    def empty_jpg(png, out, quality=90):
        Path(out).write_bytes(b"")

    spec = _entry_spec(package=_package(type="pouch"))
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    ctx = base.ProfileContext(
        convert_png=empty_convert, to_jpg=empty_jpg, run_dir=tmp_path / "run"
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    with pytest.raises(print_profile.PrintGateError) as exc:
        profile.pack(
            [{**variant, "svg": str(svg)}],
            spec,
            {"profile_ctx": ctx, "spec": spec},
        )
    assert "print.assets" in str(exc.value)
    assert "0바이트" in str(exc.value)
    assert not (tmp_path / "run" / "pack" / "meta.json").exists()


def test_entry_allow_print_checks_completes_with_skipped_gates(tmp_path, monkeypatch):
    calls = _record_exec(monkeypatch, [])
    text = re.sub(r'<g id="fold".*?</g>', "", _pouch_svg_text(), flags=re.S)
    svg = tmp_path / "pouch.svg"
    svg.write_text(text, encoding="utf-8")
    spec = _entry_spec(package=_package(type="pouch"), allow_print_checks=True)
    ctx = base.ProfileContext(run_dir=tmp_path / "run")
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    result = profile.pack(
        [{**variant, "svg": str(svg)}],
        spec,
        {"profile_ctx": ctx, "spec": spec},
    )
    _assert_only_vectorcraft(calls)
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["gates_ok"] is False
    layers = next(gate for gate in meta["gates"] if gate["id"] == "print.layers")
    assert layers["skipped"] is True
    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "warn"


@pytest.mark.parametrize("value", [0, 1, "false", "true", [], None])
def test_entry_mode_requires_strict_true_for_handoff(tmp_path, monkeypatch, value):
    """`True`만 핸드오프다 — 문자열·정수·빈 값은 엔트리로 본다."""
    calls = _record_exec(monkeypatch, [])
    spec = _print_spec(package=_package(type="pouch"), print_ready=value)
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    ctx = base.ProfileContext(run_dir=tmp_path / "run")
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]

    result = profile.pack(
        [{**variant, "svg": str(svg)}],
        spec,
        {"profile_ctx": ctx, "spec": spec},
    )
    _assert_only_vectorcraft(calls)
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["mode"] == "entry"
    assert "pdf" not in result["items"][0]


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


# ---------------------------------------------------------------------------
# Print — 플랫폼 프리셋·W8 보고·가이드·레이아웃 (GRAPH-28·29)
# ---------------------------------------------------------------------------


def _loudsourcing_entry_spec(**overrides):
    """프리셋 상한을 실제로 쓰도록 image_width 계열을 비운 엔트리 spec."""
    spec = _entry_spec(platform="loudsourcing", **overrides)
    for key in ("image_width", "max_height", "jpg_quality"):
        spec.pop(key, None)
    return spec


def _capture_convert(captured, size=(400, 267)):
    def convert(svg, out, width, max_height=3000):
        from PIL import Image

        captured.append(Path(svg).read_text(encoding="utf-8"))
        Image.new("RGB", size, (250, 249, 245)).save(out, "PNG")

    return convert


def test_wrap_text_keeps_latin_words_whole():
    text = "Camellia sinensis, bergamot oil, cornflower petals, natural flavour"
    lines = print_profile.wrap_text(text, 8, 100)
    assert len(lines) > 1
    for word in text.split():
        assert any(word in line for line in lines), (word, lines)
    assert all(line.strip() for line in lines)


def test_wrap_text_splits_cjk_per_character():
    text = "차분한인쇄라벨디자인"
    lines = print_profile.wrap_text(text, 10, 25)
    assert len(lines) > 1
    assert "".join(lines) == text


def test_wrap_text_splits_only_a_single_overlong_word():
    text = "supercalifragilisticexpialidocious"
    lines = print_profile.wrap_text(text, 8, 20)
    assert len(lines) > 1
    assert "".join(lines) == text


def test_label_ingredients_are_rewrapped_at_word_boundaries():
    text = "Camellia sinensis, bergamot oil"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="340.2" height="453.5">'
        '<g id="art">'
        '<text x="22.7" y="48.7" font-family="Archivo" font-size="20">Mindulle Tea</text>'
        '<text x="22.7" y="114.7" font-family="monospace" font-size="8" '
        'fill="#161616" fill-opacity="0.6">Camellia sinensis, bergamot o</text>'
        '<text x="22.7" y="125.7" font-family="monospace" font-size="8" '
        'fill="#161616" fill-opacity="0.6">il</text>'
        '<rect id="barcode-quiet" x="160.7" y="343.2" width="156.8" height="87.7"/>'
        "</g></svg>"
    )
    package = print_profile.package_spec({"package": _package(ingredients=text)})
    out = print_profile._rewrap_label_ingredients(svg, package)
    lines = re.findall(
        r'<text[^>]*fill-opacity="0.6"[^>]*>(.*?)</text>', out, re.DOTALL
    )
    assert len(lines) > 1
    assert " ".join(lines).split() == text.split()
    assert not any(line.strip() == "il" for line in lines)


def test_origin_is_placed_below_mark_within_safe_margin(tmp_path):
    import xml.etree.ElementTree as ET

    mark = tmp_path / "mark.svg"
    mark.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    package = print_profile.package_spec(
        {"package": _package(type="pouch", mark_path=str(mark))}
    )
    svg, _ = print_profile.build_pouch_svg(package)

    root = ET.fromstring(svg)
    mark_element = next(el for el in root.iter() if el.get("id") == "mark")
    origin = next(
        el
        for el in root.iter()
        if print_profile._local_name(el.tag) == "text"
        and el.get("text-anchor") == "end"
    )
    mark_bottom = float(mark_element.get("y")) + float(mark_element.get("height"))
    assert float(origin.get("y")) >= mark_bottom + 8
    pad = print_profile.SAFE_PAD_MM * print_profile.MM
    width = float(package["size_mm"][0]) * print_profile.MM
    assert float(origin.get("x")) <= width - pad + 0.1


def test_label_origin_text_is_moved_away_from_mark():
    import xml.etree.ElementTree as ET

    text = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="340.2" height="453.5">'
        '<g id="art"><text x="317.5" y="76.7" font-family="monospace" '
        'font-size="8" text-anchor="end" fill="#b4551f">Jeju, Korea</text></g></svg>'
    )
    moved = print_profile._relayout_origin(text, {"mark_path": "/tmp/mark.svg"}, 340.2)
    assert 'y="76.7"' not in moved
    origin = next(
        el for el in ET.fromstring(moved).iter() if el.get("text-anchor") == "end"
    )
    mark_size = print_profile.MARK_SIZE_MM * print_profile.MM
    assert float(origin.get("y")) >= 6 + 6 + mark_size


def test_print_pack_uses_preset_limits_naming_and_records_w8(tmp_path):
    spec = _loudsourcing_entry_spec()
    svg = tmp_path / "label.svg"
    svg.write_text(_minimal_entry_svg(barcode_block=_VALID_BARCODE), encoding="utf-8")
    captured = []
    ctx = base.ProfileContext(
        convert_png=_capture_convert(captured),
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    skipped = [{"path": "/srv/assets/missing.png", "reason": "missing"}]
    preflight = {"mode": "remote", "checked": 2, "skipped": skipped, "renderable": 1}
    result = profile.pack(
        [
            {
                **variant,
                "svg": str(svg),
                "skipped_assets": skipped,
                "preflight": preflight,
            }
        ],
        spec,
        {"profile_ctx": ctx, "spec": spec, "brief": "b"},
    )
    pack_dir = Path(result["dir"])

    # 프리셋 상한과 이름 규칙이 pack에 실제로 적용된다.
    assert (pack_dir / "loudsourcing-01-label-sonagi-90x60-front.png").is_file()
    assert (pack_dir / "loudsourcing-01-label-sonagi-90x60.jpg").is_file()
    assert result["items"][0]["id"] == "label-sonagi-90x60"

    meta = json.loads((pack_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["platform"] == "loudsourcing"
    assert meta["preset"] == "loudsourcing"
    assert meta["preset_warnings"] == []
    assert meta["naming"] == "loudsourcing-{index:02d}-{id}"
    assert meta["image_width"] == 1600
    assert meta["max_height"] == 2400
    assert meta["jpg_quality"] == 88
    assert meta["skipped_assets"] == skipped
    assert meta["preflight"] == {"label-sonagi-90x60": preflight}
    assert result["items"][0]["skipped_assets"] == skipped
    assert result["items"][0]["preflight"] == preflight

    entry_md = (pack_dir / "entry.md").read_text(encoding="utf-8")
    assert "건너뛴 자산 1건" in entry_md
    manifest = json.loads(
        (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["meta"]["preset"] == "loudsourcing"
    assert manifest["meta"]["skipped_assets"] == skipped
    assert manifest["meta"]["preflight"] == {"label-sonagi-90x60": preflight}


def test_entry_presentation_hides_guides_but_keeps_original_svg(tmp_path):
    spec = _entry_spec(package=_package(type="pouch"))
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    captured = []
    ctx = base.ProfileContext(
        convert_png=_capture_convert(captured),
        to_jpg=_fake_jpg,
        run_dir=tmp_path / "run",
    )
    profile = base.get_profile("print")
    variant = profile.plan({}, spec, [])[0]
    profile.pack(
        [{**variant, "svg": str(svg)}],
        spec,
        {"profile_ctx": ctx, "spec": spec, "brief": "b"},
    )

    # 변환에 쓰인 사본에서만 dieline·fold가 display:none이다.
    assert 'display="none"' in captured[0]
    original = svg.read_text(encoding="utf-8")
    assert 'display="none"' not in original
    assert 'id="dieline"' in original and 'id="fold"' in original
    # 임시 사본은 pack에 남지 않는다.
    assert not list((tmp_path / "run" / "pack").glob("*presentation*"))


def test_guides_toggle_shows_layers_in_presentation(tmp_path):
    spec = _entry_spec(package=_package(type="pouch"), guides=True)
    svg = tmp_path / "pouch.svg"
    svg.write_text(_pouch_svg_text(), encoding="utf-8")
    captured = []
    ctx = base.ProfileContext(
        convert_png=_capture_convert(captured),
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
    assert 'display="none"' not in captured[0]
    meta = json.loads((Path(result["dir"]) / "meta.json").read_text(encoding="utf-8"))
    assert meta["guides"] is True
