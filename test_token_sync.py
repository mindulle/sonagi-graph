"""토큰 동기화 검증 — 해석·검사·결정론·경고 케이스.

정본(MDS)은 읽기 전용이다. 테스트는 임시 픽스처로 규칙을 고정하고, 실제 정본이
있는 환경에서는 같은 규칙을 실제 토큰에 적용해 본다.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import token_sync as ts  # noqa: E402

MDS = Path(ts.tokens_dir())
needs_mds = pytest.mark.skipif(
    not (MDS / "tokens" / "primitives.json").is_file(),
    reason="MDS 토큰 정본이 없는 환경(MDS_TOKENS_DIR)",
)


# ---------------------------------------------------------------- 픽스처


def primitives(**overrides):
    doc = {
        "$schema": "https://design-tokens.github.io/community-group/format/",
        "meta": {"name": "fixture", "version": "9.9.9"},
        "primitive": {
            "brand": {
                "blue": {"$value": "#0055ff", "$type": "color"},
                "sun": {"$value": "#ffcc00", "$type": "color"},
            },
            "base": {"white": {"$value": "#ffffff", "$type": "color"}},
        },
        "spacing": {
            "0": {"$value": "0px", "$type": "dimension"},
            "1": {"$value": "4px", "$type": "dimension"},
            "2": {"$value": "8px", "$type": "dimension"},
            "4": {"$value": "16px", "$type": "dimension"},
            "6": {"$value": "24px", "$type": "dimension"},
            "8": {"$value": "32px", "$type": "dimension"},
        },
        "typography": {
            "fontFamily": {
                "sans": {"$value": "'Inter', sans-serif", "$type": "fontFamily"}
            },
            "fontSize": {
                "sm": {"$value": "14px", "$type": "dimension"},
                "base": {"$value": "16px", "$type": "dimension"},
            },
        },
        "borderRadius": {
            "none": {"$value": "0px", "$type": "dimension"},
            "sm": {"$value": "4px", "$type": "dimension"},
            "base": {"$value": "6px", "$type": "dimension"},
            "md": {"$value": "8px", "$type": "dimension"},
            "full": {"$value": "9999px", "$type": "dimension"},
        },
    }
    doc.update(overrides)
    return doc


def typography_scale(steps=6, reverse=False):
    sizes = [((steps - index) * 8 + 8) for index in range(steps)]
    if reverse:
        sizes = list(reversed(sizes))
    return {
        f"h{index + 1}": {
            "fontSize": {"$value": f"{size}px", "$type": "dimension"},
            "lineHeight": {"$value": f"{size + 4}px", "$type": "dimension"},
            "fontWeight": {"$value": "700", "$type": "fontWeight"},
        }
        for index, size in enumerate(sizes)
    }


def semantics(light=None, dark=None, typography=None):
    light_color = light or {
        "brand": {
            "primary": {"$value": "{primitive.brand.blue}", "$type": "color"},
            "accent": {"$value": "{primitive.brand.sun}", "$type": "color"},
        },
        "background": {"base": {"$value": "{primitive.base.white}", "$type": "color"}},
        "text": {
            "primary": {"$value": "{primitive.brand.blue}", "$type": "color"},
            "inverse": {"$value": "{primitive.base.white}", "$type": "color"},
        },
        "border": {"default": {"$value": "{primitive.brand.blue}", "$type": "color"}},
        "state": {"success": {"$value": "{primitive.brand.blue}", "$type": "color"}},
    }
    dark_color = dark or {
        "brand": {
            "primary": {"$value": "{primitive.brand.sun}", "$type": "color"},
            "accent": {"$value": "{primitive.brand.blue}", "$type": "color"},
        },
        "background": {"base": {"$value": "{primitive.brand.blue}", "$type": "color"}},
        "text": {
            "primary": {"$value": "{primitive.base.white}", "$type": "color"},
            "inverse": {"$value": "{primitive.brand.blue}", "$type": "color"},
        },
        "border": {"default": {"$value": "{primitive.brand.sun}", "$type": "color"}},
        "state": {"success": {"$value": "{primitive.brand.sun}", "$type": "color"}},
    }
    return {
        "$schema": "https://design-tokens.github.io/community-group/format/",
        "meta": {"name": "fixture-semantics", "version": "9.9.9"},
        "semantic": {"light": {"color": light_color}, "dark": {"color": dark_color}},
        "typography": typography if typography is not None else typography_scale(),
    }


def write_tokens(root: Path, docs: dict) -> Path:
    tokens = root / "tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    for name, doc in docs.items():
        (tokens / name).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return root


def fixture_root(tmp_path: Path, **overrides) -> Path:
    docs = {"primitives.json": primitives(), "semantics.json": semantics()}
    docs.update(overrides)
    return write_tokens(tmp_path, docs)


def codes(analysis):
    return {finding.code for finding in analysis.findings}


# ---------------------------------------------------------------- 해석


def test_alias_resolves_recursively_to_primitive(tmp_path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert analysis.resolved["semantic.light.color.brand.primary"] == "#0055ff"
    assert analysis.resolved["semantic.dark.color.brand.primary"] == "#ffcc00"
    chain = ts.alias_chain("semantic.light.color.brand.primary", analysis.leaves)
    assert chain == [
        "semantic.light.color.brand.primary",
        "primitive.brand.blue",
    ]


def test_alias_resolves_through_two_hops(tmp_path):
    doc = primitives()
    doc["primitive"]["brand"]["deep"] = {
        "$value": "{primitive.brand.blue}",
        "$type": "color",
    }
    root = fixture_root(tmp_path, **{"primitives.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert analysis.resolved["semantic.light.color.brand.primary"] == "#0055ff"
    assert not [f for f in analysis.findings if f.code == "alias_cycle"]


def test_cycle_is_error(tmp_path):
    doc = primitives()
    doc["primitive"]["brand"]["blue"]["$value"] = "{primitive.brand.loop}"
    doc["primitive"]["brand"]["loop"] = {
        "$value": "{primitive.brand.blue}",
        "$type": "color",
    }
    root = fixture_root(tmp_path, **{"primitives.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert "alias_cycle" in codes(analysis)
    assert analysis.errors, "순환은 치명으로 잡아야 한다"


def test_malformed_alias_is_error(tmp_path):
    doc = primitives()
    doc["primitive"]["brand"]["blue"]["$value"] = "{brandblue}"
    root = fixture_root(tmp_path, **{"primitives.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    findings = [f for f in analysis.findings if f.code == "malformed_alias"]
    assert findings and findings[0].severity == "error"


def test_dangling_alias_warns_and_suggests(tmp_path):
    doc = semantics()
    doc["typography"] = {
        "ui": {
            "label": {
                "$value": {"fontFamily": "{font.sans}", "fontSize": "12px"},
                "$type": "typography",
            }
        }
    }
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "dangling_alias")
    assert finding.severity == "warning"
    assert finding.detail["suggestion"] == "typography.fontFamily.sans"
    assert "typography.ui.label" not in analysis.resolved


def test_unresolved_leaf_is_left_out_of_css(tmp_path):
    doc = semantics()
    doc["typography"] = {
        "ui": {
            "label": {"$value": {"fontFamily": "{font.missing}", "fontSize": "12px"}}
        }
    }
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    css = ts.transpile_css(analysis)

    assert "font-missing" not in css
    assert "typography-ui-label-font-size" not in css
    # 해석되는 잎은 남는다 — 별칭은 리터럴이 아니라 변수 참조로
    assert "--mds-color-brand-primary: var(--mds-primitive-brand-blue);" in css
    assert "--mds-primitive-brand-blue: #0055ff;" in css


# ---------------------------------------------------------------- semantic 계층


def test_raw_hex_in_semantic_is_error(tmp_path):
    doc = semantics()
    doc["semantic"]["light"]["color"]["border"]["default"]["$value"] = "#123456"
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "raw_hex_in_semantic")
    assert finding.severity == "error"
    assert finding.path == "semantic.light.color.border.default"


def test_css_var_literal_is_warning(tmp_path):
    doc = semantics()
    doc["semantic"]["light"]["color"]["state"]["error"] = {
        "$value": "var(--mds-color-state-danger)",
        "$type": "color",
    }
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "non_dtcg_value")
    assert finding.severity == "warning"


def test_semantic_chain_landing_outside_primitive_warns(tmp_path):
    doc = semantics()
    doc["semantic"]["light"]["color"]["brand"]["primary"] = {
        "$value": "{semantic.light.color.brand.accent}",
        "$type": "color",
    }
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    # accent는 primitive로 착지하므로 경고가 없다 — 2단계라도 착지가 primitive면 통과
    assert "semantic_chain" not in codes(analysis)


# ---------------------------------------------------------------- 스케일


def test_space_grid_violation_is_error(tmp_path):
    doc = primitives()
    doc["spacing"]["3"] = {"$value": "10px", "$type": "dimension"}
    root = fixture_root(tmp_path, **{"primitives.json": doc})
    analysis, detail = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "space_grid_violation")
    assert finding.severity == "error"
    assert detail["scales"]["space_grid"]["violations"] == [
        {"path": "spacing.3", "value": "10px"}
    ]


def test_space_half_step_is_allowed_without_warning(tmp_path):
    """4px 하프스텝은 계약이다(CHARTER Pillar 2). 경고로 남기지 않는다."""
    root = fixture_root(tmp_path)
    analysis, detail = ts.analyze(root, tmp_path / "nope.json")

    assert "space_half_step" not in codes(analysis)
    assert "space_grid_violation" not in codes(analysis)
    assert detail["scales"]["space_grid"]["half_step_tokens"] == ["spacing.1"]


def test_type_scale_mismatch_warns(tmp_path):
    root = fixture_root(
        tmp_path, **{"semantics.json": semantics(typography=typography_scale(steps=4))}
    )
    analysis, detail = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "type_scale_mismatch")
    assert finding.detail["found"] == ["h1", "h2", "h3", "h4"]
    assert detail["scales"]["type_scale"]["expected"] == 6


def test_type_scale_order_warns(tmp_path):
    root = fixture_root(
        tmp_path,
        **{"semantics.json": semantics(typography=typography_scale(reverse=True))},
    )
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert "type_scale_order" in codes(analysis)


def test_six_step_scale_passes(tmp_path):
    root = fixture_root(tmp_path)
    analysis, detail = ts.analyze(root, tmp_path / "nope.json")

    assert "type_scale_mismatch" not in codes(analysis)
    assert detail["scales"]["type_scale"]["heading_steps"] == 6


def test_radius_contract_passes_without_semantic_layer(tmp_path):
    """radius는 2-Tier다. semantic 레이어가 없어도 값 계약이 맞으면 통과."""
    root = fixture_root(tmp_path)
    analysis, detail = ts.analyze(root, tmp_path / "nope.json")

    assert "radius_contract_violation" not in codes(analysis)
    assert "radius_contract_missing" not in codes(analysis)
    assert detail["scales"]["radius"]["contract"]["md"]["px"] == 8.0


def test_radius_value_contract_violation_is_error(tmp_path):
    """8px을 80px로 친 오타를 잡는다. 이게 계층 검사 대신 남긴 검사다."""
    doc = primitives()
    doc["borderRadius"]["md"] = {"$value": "80px", "$type": "dimension"}
    root = fixture_root(tmp_path, **{"primitives.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "radius_contract_violation")
    assert finding.severity == "error"
    assert finding.path == "borderRadius.md"
    assert finding.detail == {"expected_px": 8.0, "px": 80.0}


def test_radius_contract_missing_step_warns(tmp_path):
    doc = primitives()
    del doc["borderRadius"]["sm"]
    root = fixture_root(tmp_path, **{"primitives.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "radius_contract_missing")
    assert finding.severity == "warning"
    assert finding.path == "borderRadius.sm"


def test_radius_semantic_layer_is_not_required(tmp_path):
    """semantic radius가 없어도 경고가 없어야 한다(2-Tier, ADR 0010)."""
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert "radius_layer_missing" not in codes(analysis)


# ---------------------------------------------------------------- 투영 대조


def test_projection_drift_is_reported_not_fixed(tmp_path):
    root = fixture_root(tmp_path)
    projection = tmp_path / "semantic.json"
    projection.write_text(
        json.dumps(
            {
                "editorial": {"background": "#ffffff", "accent": "#123456"},
                "architecture": {"background": "#0055ff"},
            }
        ),
        encoding="utf-8",
    )
    analysis, detail = ts.analyze(root, projection)

    drift = [f for f in analysis.findings if f.code == "projection_drift"]
    assert {f.path for f in drift} == {"editorial.accent"}
    assert detail["projection"]["mismatches"] == 1
    # 투영 파일은 손대지 않는다
    assert (
        json.loads(projection.read_text(encoding="utf-8"))["editorial"]["accent"]
        == "#123456"
    )


def test_projection_missing_is_warning(tmp_path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "absent.json")

    finding = next(f for f in analysis.findings if f.code == "projection_missing")
    assert finding.severity == "warning"


# ---------------------------------------------------------------- 산출물


def test_css_var_names_follow_mds_contract():
    assert ts.css_var_name("primitive.neutral.50") == "--mds-primitive-neutral-50"
    assert (
        ts.css_var_name("semantic.light.color.brand.primary")
        == "--mds-color-brand-primary"
    )
    assert (
        ts.css_var_name("semantic.dark.color.background.base")
        == "--mds-color-background-base"
    )
    assert ts.css_var_name("typography.fontFamily.sans") == "--mds-font-sans"
    assert ts.css_var_name("typography.fontSize.sm") == "--mds-font-size-sm"
    assert ts.css_var_name("borderRadius.base") == "--mds-radius-base"
    assert ts.css_var_name("borderWidth.base") == "--mds-border-base"
    assert ts.css_var_name("zIndex.modal") == "--mds-z-modal"
    assert ts.css_var_name("transition.duration.fast") == "--mds-duration-fast"
    assert ts.css_var_name("space.gap.xs") == "--mds-space-gap-xs"


def test_css_has_light_and_dark_blocks(tmp_path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    css = ts.transpile_css(analysis)

    assert ':root, [data-theme="light"], [data-theme="sonagi-core"] {' in css
    assert '[data-theme="dark"] {' in css
    light = css.index("--mds-color-brand-primary: var(--mds-primitive-brand-blue);")
    dark = css.index("--mds-color-brand-primary: var(--mds-primitive-brand-sun);")
    assert light < dark


def test_aliases_stay_references_so_themes_switch(tmp_path):
    """semantic → primitive는 `var()`로 남아야 primitive 재정의가 전파된다."""
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    css = ts.transpile_css(analysis)

    assert "--mds-color-background-base: var(--mds-primitive-base-white);" in css
    assert "--mds-color-background-base: #ffffff;" not in css


def test_tailwind_covers_colors_spacing_type(tmp_path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    script = ts.transpile_tailwind(analysis)

    assert "module.exports" in script
    assert '"primary": "var(--mds-color-brand-primary)"' in script
    assert '"spacing"' in script and '"2": "var(--mds-spacing-2)"' in script
    assert '"fontFamily"' in script and '"sans": "var(--mds-font-sans)"' in script
    assert '"h1"' in script, "타이포 역할(h1~h6)이 fontSize에 들어가야 한다"


def test_swatch_json_covers_five_suites(tmp_path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    payload = json.loads(ts.transpile_swatch_json(analysis))

    assert payload["schema"] == ts.SWATCH_SCHEMA
    assert [c["name"] for c in payload["consumers"]] == [
        "vectorcraft",
        "designcraft",
        "photocraft",
        "effectcraft",
        "filmcraft",
    ]
    assert payload["themes"]["light"]["brand"]["primary"]["hex"] == "#0055ff"
    assert payload["primitives"]["brand"]["sun"]["hex"] == "#ffcc00"


def test_swatch_svg_is_svg_and_escapes_labels(tmp_path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    svg = ts.transpile_swatch_svg(analysis)

    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "brand.primary #0055ff" in svg
    assert ts.xml_escape("a<b&c") == "a&lt;b&amp;c"


def test_figma_plan_is_dry_run_with_both_modes(tmp_path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    plan = ts.figma_plan(analysis)

    assert plan["modes"] == ["light", "dark"]
    assert plan["counts"]["incomplete"] == 0
    brand = next(v for v in plan["variables"] if v["name"] == "brand/primary")
    assert brand["modes"]["light"]["r"] == 0.0
    assert brand["modes"]["light"]["b"] == 1.0
    assert brand["modes"]["dark"]["r"] == 1.0
    assert plan["write"]["dryRun"] is True

    script = ts.transpile_figma_script(analysis, plan)
    assert "const DRY_RUN = true;" in script
    assert "setValueForMode" in script


def test_hex_to_rgb01_rejects_non_hex():
    with pytest.raises(ValueError):
        ts.hex_to_rgb01("rgb(0,0,0)")
    assert ts.hex_to_rgb01("#fff") == {"r": 1.0, "g": 1.0, "b": 1.0, "a": 1.0}


# ---------------------------------------------------------------- 결정론 / 리포트


def test_same_input_twice_yields_same_artifact_hash(tmp_path):
    root = fixture_root(tmp_path)
    first = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "out")
    second = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "out")

    assert first["report"]["artifactSetHash"] == second["report"]["artifactSetHash"]
    for name, text in first["artifacts"].items():
        assert text == second["artifacts"][name]
        assert (tmp_path / "out" / name).read_text(encoding="utf-8") == text


def test_artifacts_have_no_generation_timestamp(tmp_path):
    root = fixture_root(tmp_path)
    result = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "out")

    for name, text in result["artifacts"].items():
        assert not re.search(r"20\d\d-\d\d-\d\dT", text), name
        assert "generated at" not in text.lower(), name


def test_report_records_hashes_paths_and_checks(tmp_path):
    root = fixture_root(tmp_path)
    result = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "out")
    report = result["report"]

    assert report["schema"] == ts.REPORT_SCHEMA
    assert report["tokenVersion"] == "9.9.9"
    assert [check["id"] for check in report["checks"]] == [
        "alias_resolution",
        "no_raw_hex_in_semantic",
        "scales",
        "projection_drift",
    ]
    assert {item["name"] for item in report["artifacts"]} == {
        ts.ARTIFACT_CSS,
        ts.ARTIFACT_TAILWIND,
        ts.ARTIFACT_SWATCH_JSON,
        ts.ARTIFACT_SWATCH_SVG,
        ts.ARTIFACT_FIGMA_JS,
    }
    for item in report["artifacts"]:
        assert len(item["sha256"]) == 64
        assert Path(item["path"]).is_file()
    assert (tmp_path / "out" / ts.ARTIFACT_REPORT).is_file()
    assert report["summary"]["errors"] == 0


def test_blocking_error_keeps_report_and_flags_fail(tmp_path):
    doc = semantics()
    doc["semantic"]["dark"]["color"]["text"]["primary"]["$value"] = "#abcdef"
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    result = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "out")
    report = result["report"]

    assert report["summary"]["errors"] == 1
    check = next(c for c in report["checks"] if c["id"] == "no_raw_hex_in_semantic")
    assert check["status"] == "fail"


def test_missing_tokens_dir_raises(tmp_path):
    with pytest.raises(ts.TokenSourceMissing):
        ts.analyze(tmp_path / "empty", tmp_path / "nope.json")


# ---------------------------------------------------------------- 실제 정본


@needs_mds
def test_real_mds_compiles_without_errors(tmp_path):
    result = ts.compile_all(MDS, None, tmp_path / "out")
    report = result["report"]

    assert report["summary"]["errors"] == 0, report["errors"]
    assert report["summary"]["leaves"] > 200
    for name in (
        ts.ARTIFACT_CSS,
        ts.ARTIFACT_TAILWIND,
        ts.ARTIFACT_SWATCH_JSON,
        ts.ARTIFACT_SWATCH_SVG,
        ts.ARTIFACT_FIGMA_JS,
    ):
        assert Path(result["paths"][name]).is_file(), name


@needs_mds
def test_real_mds_is_deterministic(tmp_path):
    first = ts.compile_all(MDS, None, tmp_path / "a")
    second = ts.compile_all(MDS, None, tmp_path / "b")

    assert first["report"]["artifactSetHash"] == second["report"]["artifactSetHash"]
    for name, text in first["artifacts"].items():
        assert first["artifacts"][name] == second["artifacts"][name], name


@needs_mds
def test_real_mds_type_scale_is_six_steps(tmp_path):
    analysis, detail = ts.analyze(MDS, None)

    assert detail["scales"]["type_scale"]["heading_steps"] == 6
    assert "raw_hex_in_semantic" not in codes(analysis)


# ---------------------------------------------------------------- 그래프


try:  # langgraph가 없다고 순수 로직 테스트까지 skip되면 안 된다
    import langgraph  # noqa: F401

    HAS_LANGGRAPH = True
except Exception:  # pragma: no cover - 설치 환경에 따라
    HAS_LANGGRAPH = False

langgraph_ready = pytest.mark.skipif(not HAS_LANGGRAPH, reason="langgraph 미설치")


@langgraph_ready
def test_graph_pauses_before_hitl_and_records_report(tmp_path):
    from graphs.design_token_sync_graph import build_graph

    root = fixture_root(tmp_path)
    graph = build_graph()
    config = {"configurable": {"thread_id": f"t-{tmp_path.name}"}}

    graph.invoke(
        {
            "tokens_dir": str(root),
            "projection": str(tmp_path / "nope.json"),
            "out_dir": str(tmp_path / "out"),
            "bridge_ports": [],
        },
        config,
    )
    assert graph.get_state(config).next == ("hitl_checkpoint",)
    state = graph.get_state(config).values
    assert state["status"] == "transpiled"
    assert Path(state["review"]["preview"]["swatchSvg"]).is_file()

    graph.update_state(config, {"approved": True})
    graph.invoke(None, config)
    assert graph.get_state(config).next == ("figma_apply",)
    state = graph.get_state(config).values
    assert state["status"] == "figma_bridge_unverified"
    report = json.loads(Path(state["report_path"]).read_text(encoding="utf-8"))
    assert report["approval"] == {"approved": True, "gate": "hitl_checkpoint"}
    assert report["artifactSetHash"] == state["artifact_set_hash"]

    graph.invoke(None, config)
    state = graph.get_state(config).values
    assert state["status"] == "figma_apply_skipped"
    report = json.loads(Path(state["report_path"]).read_text(encoding="utf-8"))
    assert report["figma"]["apply"]["attempted"] is False


@langgraph_ready
def test_graph_stops_when_errors_block_compilation(tmp_path):
    from graphs.design_token_sync_graph import build_graph

    doc = semantics()
    doc["semantic"]["light"]["color"]["text"]["primary"]["$value"] = "#abcdef"
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    graph = build_graph()
    config = {"configurable": {"thread_id": f"blocked-{tmp_path.name}"}}

    state = graph.invoke(
        {
            "tokens_dir": str(root),
            "projection": str(tmp_path / "nope.json"),
            "out_dir": str(tmp_path / "out"),
            "bridge_ports": [],
        },
        config,
    )
    assert state["status"] == "blocked_by_errors"
    assert graph.get_state(config).next == ()
    assert not (tmp_path / "out" / ts.ARTIFACT_CSS).exists()


# ---------------------------------------------------------------- 회귀 (리뷰 지적)


def test_missing_primitives_is_error_and_blocks_write(tmp_path):
    """필수 파일이 빠지면 부분 컴파일하지 않는다(빈 색상표가 정상 산출물이 되면 안 된다)."""
    root = write_tokens(tmp_path, {"semantics.json": semantics()})
    result = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "out")

    assert "missing_source_file" in codes(result["analysis"])
    assert result["blocked"] is True
    assert not (tmp_path / "out" / ts.ARTIFACT_CSS).exists()
    assert (tmp_path / "out" / ts.ARTIFACT_REPORT).is_file()


def test_duplicate_token_across_files_warns(tmp_path):
    doc = primitives()
    doc["typography"] = {"h1": {"fontSize": {"$value": "99px", "$type": "dimension"}}}
    root = write_tokens(
        tmp_path, {"primitives.json": doc, "semantics.json": semantics()}
    )
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert "duplicate_token" in codes(analysis)


def test_group_type_is_inherited(tmp_path):
    """DTCG 그룹 `$type` 상속. 없으면 전 토큰이 missing_type이 된다."""
    doc = primitives()
    doc["primitive"]["brand"].pop("blue")
    doc["primitive"]["brand"]["$type"] = "color"
    doc["primitive"]["brand"]["sun"].pop("$type")
    root = fixture_root(tmp_path, **{"primitives.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert analysis.leaves["primitive.brand.sun"].type == "color"


def test_scale_check_uses_resolved_values(tmp_path):
    """h1 fontSize가 별칭이어도 순서 검사가 살아 있어야 한다."""
    typo = typography_scale()
    typo["h1"]["fontSize"]["$value"] = "{typography.fontSize.base}"  # 16px → 역전
    root = fixture_root(tmp_path, **{"semantics.json": semantics(typography=typo)})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    assert "type_scale_order" in codes(analysis)


def test_css_var_target_missing_warns(tmp_path):
    doc = semantics()
    doc["semantic"]["light"]["color"]["state"]["error"] = {
        "$value": "var(--mds-does-not-exist)",
        "$type": "color",
    }
    root = fixture_root(tmp_path, **{"semantics.json": doc})
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")

    finding = next(f for f in analysis.findings if f.code == "css_var_target_missing")
    assert finding.detail["target"] == "--mds-does-not-exist"


def test_projection_token_missing_warns(tmp_path, monkeypatch):
    monkeypatch.setitem(ts.PROJECTION_MAP["editorial"], "danger", "state.missing")
    root = fixture_root(tmp_path)
    projection = tmp_path / "semantic.json"
    projection.write_text(
        json.dumps({"editorial": {"danger": "#0055ff"}}), encoding="utf-8"
    )
    analysis, _ = ts.analyze(root, projection)

    tokens = {
        f.detail["token"]
        for f in analysis.findings
        if f.code == "projection_token_missing"
    }
    assert "semantic.light.color.state.missing" in tokens


def test_hex_alpha_is_preserved():
    assert ts.hex_to_rgb01("#0055ff80")["a"] == round(0x80 / 255, 6)
    assert ts.hex_to_rgb01("#0055ff")["a"] == 1.0


def test_probe_bridge_marks_unverified_without_service(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(ts.urllib.request, "urlopen", refuse)
    probe = ts.probe_bridge([9223], timeout=0.01)

    assert probe["verified"] is False
    assert probe["probes"][0]["error"] == "OSError"


def test_probe_bridge_requires_json_version(monkeypatch):
    class Response:
        status = 200

        def read(self, size):
            return b'{"status":"ok","version":"1.40.9","connectedClients":1}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ts.urllib.request, "urlopen", lambda *a, **k: Response())
    probe = ts.probe_bridge([9223])
    assert probe["verified"] is True
    assert probe["connectedClients"] == 1
    assert probe["probes"][0]["url"].endswith("/health")


def test_probe_bridge_rejects_open_port_without_plugin(monkeypatch):
    """포트가 열려 있어도 플러그인이 안 붙었으면(connectedClients=0) 쓰면 안 된다."""

    class Response:
        status = 200

        def read(self, size):
            return (
                b'{"status":"ok","version":"1.40.9","clients":0,"connectedClients":0}'
            )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ts.urllib.request, "urlopen", lambda *a, **k: Response())
    probe = ts.probe_bridge([9224])
    assert probe["verified"] is False
    assert probe["probes"][0]["reason"] == "플러그인 미연결(connectedClients=0)"


def test_preview_writes_nothing_and_agrees_with_compile(tmp_path):
    root = fixture_root(tmp_path)
    preview = ts.preview_artifacts(root, tmp_path / "nope.json")

    assert len(preview["artifactSetHash"]) == 64
    result = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "out")
    assert preview["artifactSetHash"] == result["report"]["artifactSetHash"]


def test_same_hash_across_output_directories(tmp_path):
    root = fixture_root(tmp_path)
    first = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "a")
    second = ts.compile_all(root, tmp_path / "nope.json", tmp_path / "b")

    assert first["artifacts"] == second["artifacts"]
    assert first["report"]["artifactSetHash"] == second["report"]["artifactSetHash"]


@langgraph_ready
def test_graph_rejection_stops_before_figma(tmp_path):
    from graphs.design_token_sync_graph import build_graph

    root = fixture_root(tmp_path)
    graph = build_graph()
    config = {"configurable": {"thread_id": f"reject-{tmp_path.name}"}}

    graph.invoke(
        {
            "tokens_dir": str(root),
            "projection": str(tmp_path / "nope.json"),
            "out_dir": str(tmp_path / "out"),
            "bridge_ports": [],
        },
        config,
    )
    graph.update_state(config, {"approved": False})
    state = graph.invoke(None, config)

    assert state["status"] == "awaiting_approval"
    assert graph.get_state(config).next == ()
    assert not (tmp_path / "out" / ts.ARTIFACT_REPORT).exists()


@langgraph_ready
def test_graph_refuses_stale_approval(tmp_path):
    """승인과 재개 사이에 정본이 바뀌면 사람이 보지 않은 산출물에 도장을 찍지 않는다."""
    from graphs.design_token_sync_graph import build_graph

    root = fixture_root(tmp_path)
    graph = build_graph()
    config = {"configurable": {"thread_id": f"stale-{tmp_path.name}"}}
    graph.invoke(
        {
            "tokens_dir": str(root),
            "projection": str(tmp_path / "nope.json"),
            "out_dir": str(tmp_path / "out"),
            "bridge_ports": [],
        },
        config,
    )

    doc = primitives()
    doc["primitive"]["brand"]["blue"]["$value"] = "#0044ee"
    write_tokens(root, {"primitives.json": doc, "semantics.json": semantics()})

    graph.update_state(config, {"approved": True})
    state = graph.invoke(None, config)

    assert state["status"] == "approval_stale"
    assert graph.get_state(config).next == ()
    assert not (tmp_path / "out" / ts.ARTIFACT_REPORT).exists()
