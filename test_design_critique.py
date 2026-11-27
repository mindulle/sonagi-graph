"""DesignCritiqueGraph 단위 테스트 — LLM 호출은 스텁으로 대체한다.

검증 항목:
1. 스키마 및 출력 JSON에 숫자 점수/평점 필드가 전혀 없다.
2. 공통 4축(brief_fit, concept_logic, form_craft, distinctiveness)이 적용되고 레이아웃 축은 없다.
3. 자산별 축 팩(logo/landing/print/미등록)과 JSON 룰셋이 분리 로드된다.
4. 재료(Class101 인덱스)가 없거나 비어 있어도 최소 입력으로 끝까지 돈다.
5. 축 간 판정이 갈리면 NEEDS_HUMAN으로 떨어진다 (axis_divergence).
6. A/B 자리를 바꿔 재판정한 결론이 뒤집히면 NEEDS_HUMAN으로 떨어진다 (swap_reversal).
7. 모델·프롬프트·경로 설정을 교체할 수 있다.
8. 규칙 위반 후보를 게이트로 떨어뜨리지 않고 판정 층까지 전달한다.
"""

import json
import pytest
from langchain_core.runnables import RunnableLambda

from graphs import design_critique_graph as mod
from graphs.design_critique_graph import (
    AxisJudgementOutput,
    CritiqueFinding,
    CORE_AXIS_IDS,
)


def _extract_axis_id(prompt_value) -> str:
    text = (
        prompt_value.to_string()
        if hasattr(prompt_value, "to_string")
        else str(prompt_value)
    )
    for ax in (
        "brief_fit",
        "concept_logic",
        "form_craft",
        "distinctiveness",
        "logotype",
        "small_scale_legibility",
        "system_extension",
        "copy_hierarchy",
        "states",
        "cta",
    ):
        if f"[{ax}]" in text:
            return ax
    return "unknown"


def _is_swapped_call(prompt_value) -> bool:
    text = (
        prompt_value.to_string()
        if hasattr(prompt_value, "to_string")
        else str(prompt_value)
    )
    marker_a = "[A] 후보:\n"
    marker_b = "\n\n[B] 후보:"
    idx_a = text.find(marker_a)
    if idx_a == -1:
        return False
    start = idx_a + len(marker_a)
    idx_b = text.find(marker_b, start)
    section_a = text[start:idx_b] if idx_b != -1 else text[start:]
    return '"id": "B"' in section_a or '"name": "B"' in section_a


class _StubCritiqueLLM:
    def __init__(self, resolver):
        self.resolver = resolver

    def with_structured_output(self, schema):
        assert schema is AxisJudgementOutput

        def _call(prompt_value):
            axis_id = _extract_axis_id(prompt_value)
            swapped = _is_swapped_call(prompt_value)
            winner, findings = self.resolver(axis_id, swapped, prompt_value)
            return AxisJudgementOutput(
                winner=winner,
                rationale=f"{axis_id} ({'swapped' if swapped else 'forward'}) -> {winner}",
                findings=findings,
            )

        return RunnableLambda(_call)


@pytest.fixture
def install_stub(monkeypatch):
    captured = {"calls": []}

    def _install(resolver):
        def _fake_get_llm(model_name=None, temperature=0.1, timeout=45):
            captured["calls"].append(
                {
                    "model_name": model_name,
                    "temperature": temperature,
                    "timeout": timeout,
                }
            )
            return _StubCritiqueLLM(resolver)

        monkeypatch.setattr(mod, "get_llm", _fake_get_llm)
        return captured

    return _install


def _assert_no_numeric_scores(obj, path="root"):
    forbidden_keys = {"score", "scores", "rating", "ratings", "points", "total_score"}
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k.lower() not in forbidden_keys, (
                f"Forbidden score field at {path}.{k}"
            )
            _assert_no_numeric_scores(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _assert_no_numeric_scores(item, f"{path}[{i}]")


def test_schema_has_no_score_fields():
    assert "score" not in AxisJudgementOutput.model_fields
    assert "score" not in CritiqueFinding.model_fields
    assert set(CritiqueFinding.model_fields.keys()) == {"severity", "location", "fix"}


def test_minimal_input_empty_materials_completes(install_stub):
    def resolver(axis_id, swapped, _pv):
        winner = "B" if swapped else "A"
        return winner, [
            CritiqueFinding(
                severity="minor",
                location=f"candidate_b:{axis_id}",
                fix="대비를 조금 더 선명하게 정리하세요.",
            )
        ]

    install_stub(resolver)
    result = mod.graph.invoke(
        {
            "brief": "차분한 스튜디오 브랜드",
            "asset_type": "unknown_type",
            "candidate_a": {"id": "A"},
            "candidate_b": {"id": "B"},
        }
    )

    out = result["output"]
    _assert_no_numeric_scores(out)
    assert result["materials"] == []
    assert [ax["id"] for ax in result["active_axes"]] == list(CORE_AXIS_IDS)
    assert "layout" not in [ax["id"] for ax in result["active_axes"]]
    assert out["verdict"] == "A"
    assert out["verdict_reason"] == "consensus"
    assert out["swap_consistent"] is True
    assert len(out["findings"]) == 4


def test_axis_divergence_triggers_needs_human(install_stub):
    """축 간 판정이 갈릴 때(A vs B) NEEDS_HUMAN으로 떨어지는 경로를 검증한다."""

    def resolver(axis_id, swapped, _pv):
        if axis_id == "concept_logic":
            # 이 축만 일관되게 B를 선택 (정방향 B, 역방향 A -> 원래 B)
            winner = "A" if swapped else "B"
        else:
            # 나머지 축은 일관되게 A를 선택 (정방향 A, 역방향 B -> 원래 A)
            winner = "B" if swapped else "A"
        return winner, []

    install_stub(resolver)
    result = mod.graph.invoke(
        {
            "brief": "로고 리뉴얼",
            "asset_type": "logo",
            "candidate_a": {"id": "A", "format": "svg"},
            "candidate_b": {"id": "B", "format": "svg"},
        }
    )

    out = result["output"]
    _assert_no_numeric_scores(out)
    assert out["swap_consistent"] is True
    assert out["verdict"] == "NEEDS_HUMAN"
    assert out["verdict_reason"] == "axis_divergence"


def test_ab_swap_reversal_triggers_needs_human(install_stub):
    """A/B 자리를 바꿨을 때 결론이 뒤집히면(위치 편향) NEEDS_HUMAN이 된다."""

    def resolver(_axis_id, _swapped, _pv):
        # 자리를 바꿔도 무조건 첫 번째 슬롯("A")을 고르는 위치 편향 모델
        return "A", []

    install_stub(resolver)
    result = mod.graph.invoke(
        {
            "brief": "랜딩 페이지",
            "asset_type": "landing",
            "candidate_a": {"id": "A"},
            "candidate_b": {"id": "B"},
        }
    )

    out = result["output"]
    assert out["forward_verdict"] == "A"
    assert out["swapped_verdict_mapped"] == "B"
    assert out["swap_consistent"] is False
    assert out["verdict"] == "NEEDS_HUMAN"
    assert out["verdict_reason"] == "swap_reversal"


def test_asset_packs_and_rules_without_gate_drop(install_stub):
    """logo 팩(4+3=7축)과 규칙 층 위반이 있어도 후보를 떨어뜨리지 않고 판정한다."""
    install_stub(lambda _ax, swapped, _pv: ("A" if swapped else "B", []))

    result = mod.graph.invoke(
        {
            "brief": "핀테크 로고",
            "asset_type": "logo",
            "candidate_a": {"id": "A", "format": "png", "min_size_px": 12},
            "candidate_b": {"id": "B", "format": "svg", "min_size_px": 24},
        }
    )

    axis_ids = [ax["id"] for ax in result["active_axes"]]
    assert axis_ids == [
        "brief_fit",
        "concept_logic",
        "form_craft",
        "distinctiveness",
        "logotype",
        "small_scale_legibility",
        "system_extension",
    ]
    assert len(result["rule_results"]["findings"]) == 2
    assert result["verdict"] == "B"


def test_print_asset_uses_rules_only_without_judgement_axes(install_stub):
    captured = install_stub(lambda _ax, _sw, _pv: ("TIE", []))

    result = mod.graph.invoke(
        {
            "brief": "명함 인쇄",
            "asset_type": "print",
            "candidate_a": {
                "id": "A",
                "bleed_mm": 3,
                "color_mode": "CMYK",
                "paper_weight_gsm": 300,
            },
            "candidate_b": {
                "id": "B",
                "bleed_mm": 0,
                "color_mode": "RGB",
                "paper_weight_gsm": 300,
            },
        }
    )

    assert result["pack_mode"] == "rules_only"
    assert result["active_axes"] == []
    assert captured["calls"] == []
    assert result["verdict"] == "A"
    assert any(f["location"] == "candidate_b:print_bleed" for f in result["findings"])
    assert any(f["location"] == "candidate_b:color_mode" for f in result["findings"])


def test_configurable_model_prompt_and_material_index(install_stub, tmp_path):
    mat_file = tmp_path / "materials.json"
    mat_file.write_text(
        json.dumps(
            [{"id": "m1", "tags": ["minimal"], "note": "여백 참고"}], ensure_ascii=False
        ),
        encoding="utf-8",
    )

    seen_prompts = []

    def resolver(_ax, swapped, pv):
        seen_prompts.append(pv.to_string())
        return ("B" if swapped else "A"), []

    captured = install_stub(resolver)

    result = mod.graph.invoke(
        {
            "brief": "미니멀 로고",
            "asset_type": "other",
            "tags": ["minimal"],
            "candidate_a": {"id": "A"},
            "candidate_b": {"id": "B"},
            "config": {
                "model_name": "custom-critique-model",
                "system_template": "CUSTOM_PROMPT [{axis_id}] {rubric}",
                "material_index_path": str(mat_file),
            },
        }
    )

    assert len(result["materials"]) == 1
    assert all(c["model_name"] == "custom-critique-model" for c in captured["calls"])
    assert any("CUSTOM_PROMPT" in p for p in seen_prompts)
    assert any("여백 참고" in p for p in seen_prompts)


def test_rules_schema_and_40_plus_executable_rules():
    """GRAPH-48: 실행 가능 규칙 40개 이상, checkable_by/exceptions 필수, severity_map.json 데이터화."""
    from pathlib import Path
    from graphs.design_critique_rules import (
        VALID_CHECKABLE_BY,
        VALID_RULE_KINDS,
        load_severity_map,
        normalize_rule_kind,
        resolve_severity,
    )

    rules_dir = Path(mod.DEFAULT_CONFIG_DIR) / "rules"
    sev_map = load_severity_map(rules_dir)
    assert sev_map["map"]["HIGH"] == "blocker"
    assert sev_map["map"]["MEDIUM"] == "major"
    assert sev_map["map"]["LOW"] == "minor"

    landing = json.loads((rules_dir / "landing.json").read_text(encoding="utf-8"))
    executable = [
        r for r in landing["rules"] if r.get("checkable_by") in ("grep", "ast", "token")
    ]
    assert len(executable) >= 40, (
        f"Expected >= 40 executable rules, got {len(executable)}"
    )

    for fname in ("landing.json", "logo.json", "print.json"):
        data = json.loads((rules_dir / fname).read_text(encoding="utf-8"))
        for rule in data["rules"]:
            assert normalize_rule_kind(rule) in VALID_RULE_KINDS
            assert rule.get("checkable_by") in VALID_CHECKABLE_BY
            assert isinstance(rule.get("exceptions"), list)
            for exc in rule["exceptions"]:
                assert "glob" in exc and "reason" in exc and "basis" in exc
            assert resolve_severity(rule, sev_map) in ("blocker", "major", "minor")


def test_backward_compat_attribute_rule_without_kind():
    """kind가 없는 기존 attribute 룰셋도 하위 호환되어 정상 평가된다."""
    from graphs.design_critique_rules import evaluate_ruleset_for_candidate

    legacy_ruleset = {
        "asset_type": "legacy",
        "rules": [
            {
                "id": "legacy_bleed",
                "field": "bleed_mm",
                "op": "gte",
                "value": 3,
                "when_present": False,
                "severity": "blocker",
                "location": "bleed",
                "fix": "도련 3mm 필요",
            }
        ],
    }
    findings, not_verified = evaluate_ruleset_for_candidate(
        {"id": "A", "bleed_mm": 1}, "candidate_a", legacy_ruleset
    )
    assert len(findings) == 1
    assert findings[0]["severity"] == "blocker"
    assert findings[0]["location"] == "candidate_a:bleed"
    assert not_verified == []


def test_render_and_human_rules_separated_into_not_verified(install_stub):
    """render·human 규칙은 실행하지 않고 not_verified로 분리하며 findings에 넣지 않는다."""
    install_stub(lambda _ax, swapped, _pv: ("A" if swapped else "B", []))

    result = mod.graph.invoke(
        {
            "brief": "랜딩 리뷰",
            "asset_type": "landing",
            "candidate_a": {
                "id": "A",
                "source_files": {"app/page.tsx": "<main><h1>Hello</h1></main>"},
            },
            "candidate_b": {
                "id": "B",
                "source_files": {"app/page.tsx": "<main><h1>World</h1></main>"},
            },
        }
    )

    out = result["output"]
    _assert_no_numeric_scores(out)
    not_verified = out["rules"]["not_verified"]
    assert len(not_verified) > 0
    assert all(item["checkable_by"] in ("render", "human") for item in not_verified)
    finding_locs = [f["location"] for f in out["rules"]["findings"]]
    assert not any("render." in loc or "human." in loc for loc in finding_locs)


def test_source_rules_grep_ast_token_and_exceptions(tmp_path):
    """grep/ast/token 규칙 평가와 exceptions(path glob) 면제 동작을 검증한다."""
    from pathlib import Path
    from graphs.design_critique_rules import evaluate_ruleset_for_candidate

    rules_dir = Path(mod.DEFAULT_CONFIG_DIR) / "rules"
    landing = json.loads((rules_dir / "landing.json").read_text(encoding="utf-8"))

    candidate = {
        "id": "A",
        "source_files": {
            "app/Bad.tsx": '<div onClick={() => {}} className="transition-all"></div>',
            "app/Exempted.test.tsx": '<div onClick={() => {}} className="transition-all"></div>',
            "app/icons/libraryIcons.tsx": '<svg><path fill="#ff0000" /></svg>',
            "app/icons/bad.svg": '<svg><path fill="#123456" /></svg>',
            "app/tokens/theme.css": ":root { --accent: oklch(65% 0.2 250); --error: oklch(60% 0.2 255); --text: #777777; --bg: #888888; }",
        },
    }

    findings, not_verified = evaluate_ruleset_for_candidate(
        candidate, "candidate_a", landing, rules_dir=rules_dir
    )
    locs = [f["location"] for f in findings]
    rule_ids = {f.get("rule_id") for f in findings}

    assert "a11y.click.div" in rule_ids
    assert "motion.transition.all" in rule_ids
    assert "ui.svg.hardcoded.ast" in rule_ids
    assert "ui.svg.viewbox.ast" in rule_ids
    assert "color.token.oklch.hue_collision" in rule_ids
    assert "color.token.contrast.aa" in rule_ids

    # exceptions에 걸린 파일은 발견을 만들지 않는다.
    assert not any("Exempted.test.tsx" in loc for loc in locs)
    assert not any("libraryIcons.tsx" in loc for loc in locs)
    assert len(not_verified) == 12


def test_no_subprocess_in_design_critique():
    """subprocess 0 원칙: design_critique_graph와 design_critique_rules는 subprocess를 import하지 않는다."""
    from pathlib import Path
    from graphs.design_critique_rules import evaluate_ruleset_for_candidate

    root = Path(__file__).resolve().parent
    ruleset = {
        "asset_type": "py_check",
        "rules": [
            {
                "id": "no_subprocess",
                "kind": "source",
                "checkable_by": "ast",
                "ast_check": "no_subprocess",
                "path_globs": ["**/*.py"],
                "severity": "blocker",
                "fix": "subprocess 사용 금지",
                "exceptions": [],
            }
        ],
    }
    files = {
        "graphs/design_critique_graph.py": (
            root / "graphs" / "design_critique_graph.py"
        ).read_text(encoding="utf-8"),
        "graphs/design_critique_rules.py": (
            root / "graphs" / "design_critique_rules.py"
        ).read_text(encoding="utf-8"),
    }
    findings, _ = evaluate_ruleset_for_candidate(
        {"id": "repo", "source_files": files}, "repo", ruleset
    )
    assert findings == []


def test_false_positive_tuning_autofocus_and_stop_propagation():
    """오탐 보강: TS autoFocus?: boolean 타입 선언과 stopPropagation 래퍼 div는 발견하지 않는다."""
    from pathlib import Path
    from graphs.design_critique_rules import evaluate_ruleset_for_candidate

    rules_dir = Path(mod.DEFAULT_CONFIG_DIR) / "rules"
    landing = json.loads((rules_dir / "landing.json").read_text(encoding="utf-8"))
    candidate = {
        "id": "A",
        "source_files": {
            "components/SearchBar.tsx": "type Props = { autoFocus?: boolean; };\nexport function SearchBar({ autoFocus }: Props) { return <input autoFocus={false} />; }",
            "components/Row.tsx": '<div onClick={(e) => e.stopPropagation()}><button type="button">Copy</button></div>',
        },
    }
    findings, _ = evaluate_ruleset_for_candidate(
        candidate, "candidate_a", landing, rules_dir=rules_dir
    )
    rule_ids = {f.get("rule_id") for f in findings}
    assert "a11y.autofocus.prop" not in rule_ids
    assert "a11y.click.div" not in rule_ids


def test_false_positive_tuning_graph_50_four_cases():
    """GRAPH-50 오탐 4종 정리 및 정상 탐지 2건 유지 회귀 테스트."""
    from pathlib import Path
    from graphs.design_critique_rules import evaluate_ruleset_for_candidate

    rules_dir = Path(mod.DEFAULT_CONFIG_DIR) / "rules"
    landing = json.loads((rules_dir / "landing.json").read_text(encoding="utf-8"))

    candidate = {
        "id": "A",
        "source_files": {
            # 1) Next.js 템플릿 자산은 제외, 커스텀 SVG는 유지
            "public/file.svg": '<svg><path fill="#666" /></svg>',
            "public/globe.svg": '<svg><path fill="#666" /></svg>',
            "public/next.svg": '<svg><path fill="#000" /></svg>',
            "public/vercel.svg": '<svg><path fill="#000" /></svg>',
            "public/window.svg": '<svg><path fill="#666" /></svg>',
            "public/custom-badge.svg": '<svg><path fill="#123456" /></svg>',
            # 2) type.allcaps.tracking: 인라인 letterSpacing 인식, 무자간은 유지
            "components/BacklinksSection.tsx": (
                "export const H = () => <h3 style={{\n"
                "  textTransform: 'uppercase',\n"
                "  letterSpacing: '0.05em',\n"
                "}}>Backlinks</h3>;"
            ),
            "components/WiredCopyNoteShape.tsx": (
                "export const N = () => <div style={{ fontSize: '10px', textTransform: 'uppercase' }}>NOTE</div>;"
            ),
            "components/ZeroTracking.tsx": (
                "export const Z = () => <div style={{ textTransform: 'uppercase', letterSpacing: '0' }}>ZERO</div>;"
            ),
            # 3) ui.hover.media_query: 색상 전용 hover 제외, 비색상(transform/opacity) hover 유지
            "app/globals.css": (
                "a:hover { color: var(--brand); }\n"
                ".link:hover,\n.prose a:hover {\n  color: #fff;\n  background-color: #1991b9;\n}\n"
                ".card:hover { transform: translateY(-2px); }\n"
            ),
            # 4) ui.icon.hardcoded: 데이터 상수 SVG 문자열 제외, JSX UI 아이콘 유지
            "src/AssetDrawer.tsx": (
                "const SAMPLES = [\n"
                "  { label: 'star', svg: '<svg viewBox=\"0 0 24 24\" fill=\"#f59e0b\"><path d=\"M0 0\"/></svg>' },\n"
                "];\n"
                "export const ChromeIcon = () => <svg viewBox=\"0 0 24 24\" fill=\"#ef4444\"><path d=\"M0 0\"/></svg>;\n"
            ),
            # 정상 동작 유지: transition-all
            "components/ReadingProgress.tsx": (
                "export const Bar = () => <div className=\"h-1 transition-all duration-150\" />;"
            ),
        },
    }

    findings, _ = evaluate_ruleset_for_candidate(
        candidate, "candidate_a", landing, rules_dir=rules_dir
    )
    locs = [f["location"] for f in findings]

    # 1) Next.js 템플릿 자산 5종 제외, 커스텀 SVG는 탐지
    for tmpl in ("file.svg", "globe.svg", "next.svg", "vercel.svg", "window.svg"):
        assert not any(f"public/{tmpl}" in loc for loc in locs), f"{tmpl} should be excluded"
    assert any("public/custom-badge.svg" in loc for loc in locs)

    # 2) 인라인 letterSpacing 면제, 무자간(WiredCopyNoteShape, ZeroTracking) 탐지
    assert not any("BacklinksSection.tsx" in loc for loc in locs)
    assert any(
        f.get("rule_id") == "type.allcaps.tracking" and "WiredCopyNoteShape.tsx" in f["location"]
        for f in findings
    )
    assert any(
        f.get("rule_id") == "type.allcaps.tracking" and "ZeroTracking.tsx" in f["location"]
        for f in findings
    )

    # 3) 색상 전용 hover 제외, transform hover(5행)만 탐지
    hover_locs = [
        f["location"] for f in findings if f.get("rule_id") == "ui.hover.media_query"
    ]
    assert len(hover_locs) == 1 and hover_locs[0].endswith("app/globals.css:7")

    # 4) 데이터 상수 SVG(2행) 제외, JSX UI 아이콘(4행)만 탐지
    icon_locs = [
        f["location"] for f in findings if f.get("rule_id") == "ui.icon.hardcoded"
    ]
    assert any(loc.endswith("src/AssetDrawer.tsx:4") for loc in icon_locs) and not any("src/AssetDrawer.tsx:2" in loc for loc in icon_locs)

    # 정상 동작 유지: ReadingProgress.tsx transition-all
    assert any(
        f.get("rule_id") == "motion.transition.all" and "ReadingProgress.tsx" in f["location"]
        for f in findings
    )


# --------------------------------------------------------------------------- #
# 비전 입력 (GRAPH-49): 후보 이미지는 라벨 붙은 블록으로, 경로는 텍스트에서 제거
# --------------------------------------------------------------------------- #


class _VisionCaptureLLM:
    """프롬프트 값을 그대로 모으는 스텁. 이미지 블록 검사용."""

    def __init__(self, sink):
        self.sink = sink

    def with_structured_output(self, schema):
        assert schema is AxisJudgementOutput

        def _call(prompt_value):
            self.sink.append(prompt_value)
            return AxisJudgementOutput(
                winner="TIE", rationale="vision stub", findings=[]
            )

        return RunnableLambda(_call)


def _install_vision_capture(monkeypatch):
    captured = []
    monkeypatch.setattr(
        mod,
        "get_llm",
        lambda model_name=None, temperature=0.1, timeout=45: _VisionCaptureLLM(
            captured
        ),
    )
    return captured


def _write_png(path, color):
    from PIL import Image

    Image.new("RGB", (24, 24), color).save(path)
    return path


def _human_content(prompt_value):
    messages = getattr(prompt_value, "messages", None) or []
    humans = [m for m in messages if getattr(m, "type", "") == "human"]
    assert humans, "human 메시지가 없습니다"
    return humans[-1].content


def _text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _image_urls(content):
    if isinstance(content, str):
        return []
    return [
        block["image_url"]["url"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]


def test_images_are_labeled_blocks_and_paths_are_stripped(tmp_path, monkeypatch):
    a_png = _write_png(tmp_path / "a.png", (200, 30, 30))
    b_png = _write_png(tmp_path / "b.png", (30, 30, 200))
    captured = _install_vision_capture(monkeypatch)

    result = mod.graph.invoke(
        {
            "brief": "비전 테스트",
            "asset_type": "unknown_type",
            "candidate_a": {"id": "A", "image": str(a_png)},
            "candidate_b": {"id": "B", "image": str(b_png)},
            "config": {"swap_check": False},
        }
    )

    assert result["output"]["verdict"] == "TIE"
    assert len(captured) == 4  # 4 core axes, swap off
    for prompt_value in captured:
        content = _human_content(prompt_value)
        assert isinstance(content, list)
        text = _text_of(content)
        assert str(a_png) not in text and "a.png" not in text
        assert str(b_png) not in text and "b.png" not in text
        assert '"image":' not in text
        urls = _image_urls(content)
        assert len(urls) == 2
        assert all(url.startswith("data:image/png;base64,") for url in urls)
        labels = [
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        assert labels[-2:] == ["[A] 첨부 이미지 1", "[B] 첨부 이미지 1"]


def test_images_follow_candidate_when_slots_swap(tmp_path, monkeypatch):
    a_png = _write_png(tmp_path / "a.png", (255, 0, 0))
    b_png = _write_png(tmp_path / "b.png", (0, 0, 255))
    captured = _install_vision_capture(monkeypatch)

    mod.graph.invoke(
        {
            "brief": "스왑",
            "asset_type": "unknown_type",
            "candidate_a": {"id": "A", "image": str(a_png)},
            "candidate_b": {"id": "B", "image": str(b_png)},
        }
    )

    a_url = mod.resolve_image_url(str(a_png), tmp_path)
    b_url = mod.resolve_image_url(str(b_png), tmp_path)
    seen_swapped = 0
    for prompt_value in captured:
        content = _human_content(prompt_value)
        text = _text_of(content)
        urls = _image_urls(content)
        assert len(urls) == 2
        # 첫 슬롯(B)의 텍스트가 "id": "B"면 스왑 호출이다.
        swapped = '"id": "B"' in text.split("[B] 후보:")[0]
        if swapped:
            seen_swapped += 1
            assert urls[0] == b_url
            assert urls[1] == a_url
        else:
            assert urls[0] == a_url
            assert urls[1] == b_url
    assert seen_swapped == 4


def test_missing_image_yields_needs_human_with_reason(tmp_path, monkeypatch):
    _install_vision_capture(monkeypatch)
    result = mod.graph.invoke(
        {
            "brief": "없는 파일",
            "asset_type": "unknown_type",
            "candidate_a": {"id": "A", "image": str(tmp_path / "nope.png")},
            "candidate_b": {"id": "B"},
            "config": {"swap_check": False},
        }
    )

    assert result["verdict"] == "NEEDS_HUMAN"
    rationales = [ax["rationale"] for ax in result["output"]["axes"]]
    assert all("이미지 파일이 없습니다" in r for r in rationales)
    assert all(ax["verdict"] == "NEEDS_HUMAN" for ax in result["output"]["axes"])


def test_image_helper_rejects_svg_and_passes_data_uri(tmp_path):
    from graphs.design_critique_graph import CritiqueImageError

    svg = tmp_path / "mark.svg"
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    with pytest.raises(CritiqueImageError):
        mod.resolve_image_url(str(svg), tmp_path)

    data_uri = "data:image/png;base64,AAAA"
    assert mod.resolve_image_url(data_uri, tmp_path) == data_uri
    assert mod.resolve_image_url({"url": "https://example.com/a.png"}, tmp_path) == (
        "https://example.com/a.png"
    )


def test_text_only_candidates_keep_string_content(tmp_path, monkeypatch):
    captured = _install_vision_capture(monkeypatch)
    mod.graph.invoke(
        {
            "brief": "텍스트만",
            "asset_type": "unknown_type",
            "candidate_a": {"id": "A"},
            "candidate_b": {"id": "B"},
            "config": {"swap_check": False},
        }
    )
    assert captured
    assert all(isinstance(_human_content(pv), str) for pv in captured)


def test_too_many_images_returns_needs_human(tmp_path, monkeypatch):
    _install_vision_capture(monkeypatch)
    pngs = [str(_write_png(tmp_path / f"{i}.png", (i, 0, 0))) for i in range(5)]
    result = mod.graph.invoke(
        {
            "brief": "이미지 5장",
            "asset_type": "unknown_type",
            "candidate_a": {"id": "A", "images": pngs},
            "candidate_b": {"id": "B"},
            "config": {"swap_check": False},
        }
    )
    assert result["verdict"] == "NEEDS_HUMAN"
    assert any("넘습니다" in ax["rationale"] for ax in result["output"]["axes"])
