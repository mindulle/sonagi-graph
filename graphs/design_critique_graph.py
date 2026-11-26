"""DesignCritiqueGraph — 4축 페어 A/B 비교 판정 그래프 뼈대 (점수 없음).

설계 원칙 (GRAPH-47):
- 공통 판정 축 4개: brief_fit, concept_logic, form_craft, distinctiveness (레이아웃 축 없음).
- 자산별 보조 축은 JSON 팩 파일로 주입한다(logo, landing 등). print는 규칙 층 전용.
- 스키마와 출력 JSON에 숫자 점수/평점 필드를 두지 않는다.
- 최종 판정은 A | B | TIE | BOTH_REJECT | NEEDS_HUMAN.
  - 축 간 판정이 갈리면 NEEDS_HUMAN.
  - A/B 자리를 바꿔 재판정한 결론이 뒤집히면 NEEDS_HUMAN.
- 규칙 층(JSON 룰셋)과 판정 층(LLM 루브릭)을 분리하며, 게이트로 후보를 떨어뜨리지 않는다.
- 모델·프롬프트·재료 인덱스 경로·축 팩 경로·룰셋 경로는 모두 설정/환경변수로 교체 가능하다.
"""

from __future__ import annotations

import json
import operator
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from graphs.design_critique_rules import evaluate_ruleset_for_candidate
from graphs.llm import chat_model

CORE_AXIS_IDS = ("brief_fit", "concept_logic", "form_craft", "distinctiveness")

Verdict = Literal["A", "B", "TIE", "BOTH_REJECT", "NEEDS_HUMAN"]
AxisChoice = Literal["A", "B", "TIE", "BOTH_REJECT"]
Severity = Literal["blocker", "major", "minor"]

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "config" / "design_critique"


@dataclass(frozen=True)
class DesignCritiqueSettings:
    """그래프 실행 설정. 환경변수와 state['config']로 오버라이드할 수 있다."""

    axis_packs_dir: Path
    rules_dir: Path
    prompts_path: Path
    material_index_path: Optional[Path]
    model_name: Optional[str] = None
    temperature: float = 0.1
    system_template: Optional[str] = None
    user_template: Optional[str] = None
    swap_check: bool = True


def resolve_settings(config: Optional[Dict[str, Any]] = None) -> DesignCritiqueSettings:
    """환경변수 + state.config에서 경로와 모델/프롬프트 설정을 해석한다."""
    cfg = config or {}

    axis_packs_dir = Path(
        cfg.get("axis_packs_dir")
        or os.environ.get("DESIGN_CRITIQUE_AXIS_PACKS_DIR")
        or (DEFAULT_CONFIG_DIR / "packs")
    )
    rules_dir = Path(
        cfg.get("rules_dir")
        or os.environ.get("DESIGN_CRITIQUE_RULES_DIR")
        or (DEFAULT_CONFIG_DIR / "rules")
    )
    prompts_path = Path(
        cfg.get("prompts_path")
        or os.environ.get("DESIGN_CRITIQUE_PROMPTS_PATH")
        or (DEFAULT_CONFIG_DIR / "prompts.json")
    )
    raw_material = cfg.get("material_index_path") or os.environ.get(
        "DESIGN_CRITIQUE_MATERIAL_INDEX"
    )
    material_index_path = Path(raw_material) if raw_material else None

    return DesignCritiqueSettings(
        axis_packs_dir=axis_packs_dir,
        rules_dir=rules_dir,
        prompts_path=prompts_path,
        material_index_path=material_index_path,
        model_name=cfg.get("model_name"),
        temperature=float(cfg.get("temperature", 0.1)),
        system_template=cfg.get("system_template") or cfg.get("prompt_template"),
        user_template=cfg.get("user_template"),
        swap_check=bool(cfg.get("swap_check", True)),
    )


class CritiqueFinding(BaseModel):
    """모든 발견은 심각도 + 위치 + 고칠 문장으로 구성된다. 점수 없음."""

    severity: Severity = Field(description="심각도: blocker | major | minor")
    location: str = Field(description="결함 위치 (예: candidate_a:symbol, print_bleed)")
    fix: str = Field(description="어떻게 고쳐야 하는지 구체적인 수정 문장")


class AxisJudgementOutput(BaseModel):
    """단일 축 단방향 페어 판정 결과 (점수 필드 금지)."""

    winner: AxisChoice = Field(description="판정 결과: A | B | TIE | BOTH_REJECT")
    rationale: str = Field(description="대조 근거 요약 문장")
    findings: List[CritiqueFinding] = Field(
        default_factory=list,
        description="발견 목록 (각 항목은 severity, location, fix)",
    )


class DesignCritiqueState(TypedDict, total=False):
    brief: str
    asset_type: str
    candidate_a: Dict[str, Any]
    candidate_b: Dict[str, Any]
    tags: List[str]
    config: Dict[str, Any]

    materials: List[Dict[str, Any]]
    active_axes: List[Dict[str, Any]]
    pack_mode: str
    rule_results: Dict[str, Any]

    axis_results: Annotated[List[Dict[str, Any]], operator.add]

    verdict: Verdict
    verdict_reason: str
    forward_verdict: Verdict
    swapped_verdict_mapped: Verdict
    swap_consistent: bool
    findings: List[Dict[str, str]]
    summary: str
    output: Dict[str, Any]
    status: str


class AxisTaskState(TypedDict):
    axis: Dict[str, Any]
    brief: str
    asset_type: str
    candidate_a: Dict[str, Any]
    candidate_b: Dict[str, Any]
    materials: List[Dict[str, Any]]
    config: Dict[str, Any]


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def load_materials(
    material_index_path: Optional[Path],
    tags: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """재료(Class101 첨부·태그 인덱스)를 읽는다. 없거나 비어 있어도 빈 리스트를 반환한다."""
    if material_index_path is None or not material_index_path.is_file():
        return []
    try:
        raw = json.loads(material_index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    elif isinstance(raw, dict):
        entries = raw.get("items") or raw.get("materials") or []
        items = [x for x in entries if isinstance(x, dict)]
    else:
        return []

    if not tags:
        return items

    tag_set = {t.lower() for t in tags if isinstance(t, str)}
    matched = [
        item
        for item in items
        if tag_set & {str(t).lower() for t in item.get("tags", [])}
    ]
    return matched if matched else items


def load_axis_pack(
    asset_type: str,
    axis_packs_dir: Path,
) -> tuple[List[Dict[str, Any]], str]:
    """공통 4축 + 자산별 보조 축 팩을 로드한다."""
    common_data = _read_json(axis_packs_dir / "common.json") or {}
    common_axes = [
        ax for ax in common_data.get("axes", []) if isinstance(ax, dict) and ax.get("id")
    ]

    normalized_type = (asset_type or "").strip().lower()
    pack_data = _read_json(axis_packs_dir / f"{normalized_type}.json") if normalized_type else None

    if pack_data:
        mode = str(pack_data.get("mode", "axes"))
        if mode == "rules_only":
            return [], "rules_only"
        extra_axes = [
            ax for ax in pack_data.get("axes", []) if isinstance(ax, dict) and ax.get("id")
        ]
        return common_axes + extra_axes, mode

    return common_axes, "axes"


def load_ruleset(asset_type: str, rules_dir: Path) -> Dict[str, Any]:
    """자산 종류에 맞는 JSON 룰셋을 읽고, 없으면 default.json으로 폴백한다."""
    normalized_type = (asset_type or "").strip().lower()
    if normalized_type:
        specific = _read_json(rules_dir / f"{normalized_type}.json")
        if specific is not None:
            return specific
    return _read_json(rules_dir / "default.json") or {"asset_type": "default", "rules": []}


def load_prompts(
    prompts_path: Path,
    settings: DesignCritiqueSettings,
) -> tuple[str, str]:
    """노드 프롬프트 템플릿을 외부 JSON 및 설정에서 읽는다."""
    data = _read_json(prompts_path) or {}
    system_tpl = (
        settings.system_template
        or data.get("system_template")
        or (
            "당신은 디자인 A/B 비교 비평가입니다. 숫자 점수를 출력하지 마세요.\n"
            "판정 축: [{axis_id}] {axis_label}\n"
            "루브릭: {rubric}"
        )
    )
    user_tpl = (
        settings.user_template
        or data.get("user_template")
        or (
            "브리프: {brief}\n참고 재료: {materials}\n\n"
            "[{first_label}] 후보:\n{first_candidate}\n\n"
            "[{second_label}] 후보:\n{second_candidate}"
        )
    )
    return system_tpl, user_tpl


def _eval_rule_op(actual: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return actual == expected
    if op == "in":
        return actual in (expected or [])
    if op == "gte":
        return isinstance(actual, (int, float)) and actual >= expected
    if op == "gt":
        return isinstance(actual, (int, float)) and actual > expected
    if op == "truthy":
        return bool(actual)
    return True


def evaluate_rules_for_candidate(
    candidate: Dict[str, Any],
    candidate_label: str,
    ruleset: Dict[str, Any],
    rules_dir: Optional[Path] = None,
) -> List[Dict[str, str]]:
    """단일 후보에 대해 결정론적 JSON 규칙을 평가하고 발견 목록을 반환한다."""
    findings, _not_verified = evaluate_ruleset_for_candidate(
        candidate, candidate_label, ruleset, rules_dir=rules_dir
    )
    return findings


def map_swapped_choice(choice: str) -> Verdict:
    """A/B 자리를 바꾼 재판정 결과를 원래 후보(A/B) 기준으로 되돌린다."""
    if choice == "A":
        return "B"
    if choice == "B":
        return "A"
    if choice in ("TIE", "BOTH_REJECT", "NEEDS_HUMAN"):
        return choice  # type: ignore[return-value]
    return "NEEDS_HUMAN"


def aggregate_axis_verdicts(verdicts: List[str]) -> Verdict:
    """축별 판정 결과를 종합한다.

    - 어느 한 축이라도 NEEDS_HUMAN이면 NEEDS_HUMAN.
    - TIE를 제외한 유효 판정이 둘 이상으로 갈리면(예: A vs B, A vs BOTH_REJECT) NEEDS_HUMAN.
    - 모두 TIE이면 TIE.
    - 단일 방향으로 모이면 그 판정(A | B | BOTH_REJECT)을 반환한다.
    """
    if not verdicts:
        return "TIE"
    if "NEEDS_HUMAN" in verdicts:
        return "NEEDS_HUMAN"
    non_tie = {v for v in verdicts if v != "TIE"}
    if not non_tie:
        return "TIE"
    if len(non_tie) > 1:
        return "NEEDS_HUMAN"
    winner = next(iter(non_tie))
    if winner in ("A", "B", "BOTH_REJECT"):
        return winner  # type: ignore[return-value]
    return "NEEDS_HUMAN"


def get_llm(model_name: Optional[str] = None, temperature: float = 0.1):
    """크리틱 프록시 클라이언트. 모델은 DESIGN_CRITIQUE_LLM_MODEL 또는 config로 바꾼다."""
    return chat_model(
        "DESIGN_CRITIQUE_LLM_MODEL",
        default_model=model_name,
        temperature=temperature,
    )


def prepare_context(state: DesignCritiqueState) -> Dict[str, Any]:
    """설정, 재료 인덱스, 자산별 축 팩을 로드한다. 재료가 비어 있어도 통과한다."""
    settings = resolve_settings(state.get("config"))
    asset_type = state.get("asset_type") or "logo"
    materials = load_materials(settings.material_index_path, state.get("tags"))
    active_axes, pack_mode = load_axis_pack(asset_type, settings.axis_packs_dir)
    return {
        "asset_type": asset_type,
        "materials": materials,
        "active_axes": active_axes,
        "pack_mode": pack_mode,
        "status": "context_prepared",
    }


def rules_layer(state: DesignCritiqueState) -> Dict[str, Any]:
    """규칙 층: JSON 룰셋으로 후보 A/B를 결정론 검사한다. 후보를 떨어뜨리는 게이트는 두지 않는다."""
    settings = resolve_settings(state.get("config"))
    asset_type = state.get("asset_type") or "logo"
    ruleset = load_ruleset(asset_type, settings.rules_dir)

    cand_a = state.get("candidate_a") or {}
    cand_b = state.get("candidate_b") or {}

    findings_a, not_verified_a = evaluate_ruleset_for_candidate(
        cand_a, "candidate_a", ruleset, rules_dir=settings.rules_dir
    )
    findings_b, not_verified_b = evaluate_ruleset_for_candidate(
        cand_b, "candidate_b", ruleset, rules_dir=settings.rules_dir
    )

    rule_results = {
        "asset_type": asset_type,
        "ruleset": ruleset.get("asset_type", "default"),
        "candidate_a_passed": not any(f["severity"] == "blocker" for f in findings_a),
        "candidate_b_passed": not any(f["severity"] == "blocker" for f in findings_b),
        "findings": findings_a + findings_b,
        "not_verified": not_verified_a + not_verified_b,
    }
    return {
        "rule_results": rule_results,
        "status": "rules_evaluated",
    }


def route_to_axes(state: DesignCritiqueState):
    """활성 축 목록만큼 judge_axis로 팬아웃한다. 축이 없으면(print 등) synthesizer로 직행한다."""
    active_axes = state.get("active_axes") or []
    if not active_axes:
        return "synthesizer"

    return [
        Send(
            "judge_axis",
            {
                "axis": axis,
                "brief": state.get("brief") or "",
                "asset_type": state.get("asset_type") or "logo",
                "candidate_a": state.get("candidate_a") or {},
                "candidate_b": state.get("candidate_b") or {},
                "materials": state.get("materials") or [],
                "config": state.get("config") or {},
            },
        )
        for axis in active_axes
    ]


def _invoke_axis_pair(
    evaluator: Any,
    axis: Dict[str, Any],
    brief: str,
    materials: List[Dict[str, Any]],
    first_label: str,
    first_candidate: Dict[str, Any],
    second_label: str,
    second_candidate: Dict[str, Any],
) -> AxisJudgementOutput:
    return evaluator.invoke(
        {
            "axis_id": axis.get("id", ""),
            "axis_label": axis.get("label", axis.get("id", "")),
            "rubric": axis.get("rubric", ""),
            "brief": brief or "(브리프 미제공)",
            "materials": json.dumps(materials, ensure_ascii=False) if materials else "[]",
            "first_label": first_label,
            "first_candidate": json.dumps(first_candidate, ensure_ascii=False),
            "second_label": second_label,
            "second_candidate": json.dumps(second_candidate, ensure_ascii=False),
        }
    )


def judge_axis(state: AxisTaskState) -> Dict[str, Any]:
    """단일 축 판정 노드: (A, B) 정방향 + (B, A) 자리 바꿈 재판정을 수행한다."""
    axis = state["axis"]
    settings = resolve_settings(state.get("config"))
    system_tpl, user_tpl = load_prompts(settings.prompts_path, settings)

    prompt = ChatPromptTemplate.from_messages([("system", system_tpl), ("user", user_tpl)])
    llm = get_llm(model_name=settings.model_name, temperature=settings.temperature)
    evaluator = prompt | llm.with_structured_output(AxisJudgementOutput)

    cand_a = state.get("candidate_a") or {}
    cand_b = state.get("candidate_b") or {}
    brief = state.get("brief") or ""
    materials = state.get("materials") or []

    try:
        forward_res = _invoke_axis_pair(
            evaluator, axis, brief, materials, "A", cand_a, "B", cand_b
        )
        forward_winner: Verdict = forward_res.winner
        rationale = forward_res.rationale
        findings = [f.model_dump() for f in forward_res.findings]
    except Exception as exc:
        return {
            "axis_results": [
                {
                    "axis_id": axis.get("id", "unknown"),
                    "layer": axis.get("layer", "core"),
                    "verdict": "NEEDS_HUMAN",
                    "forward_winner": "NEEDS_HUMAN",
                    "swapped_winner_raw": "NEEDS_HUMAN",
                    "swapped_winner_mapped": "NEEDS_HUMAN",
                    "swap_consistent": False,
                    "rationale": f"축 판정 호출 실패: {exc}",
                    "findings": [
                        {
                            "severity": "major",
                            "location": f"axis:{axis.get('id', 'unknown')}",
                            "fix": "LLM 호출 오류를 확인하고 해당 축을 수동 판정하세요.",
                        }
                    ],
                }
            ]
        }

    if settings.swap_check:
        try:
            swapped_res = _invoke_axis_pair(
                evaluator, axis, brief, materials, "A", cand_b, "B", cand_a
            )
            swapped_raw: Verdict = swapped_res.winner
            swapped_mapped = map_swapped_choice(swapped_raw)
        except Exception:
            swapped_raw = "NEEDS_HUMAN"
            swapped_mapped = "NEEDS_HUMAN"
    else:
        swapped_raw = forward_winner
        swapped_mapped = forward_winner

    swap_consistent = forward_winner == swapped_mapped
    axis_verdict: Verdict = forward_winner if swap_consistent else "NEEDS_HUMAN"

    return {
        "axis_results": [
            {
                "axis_id": axis.get("id", "unknown"),
                "layer": axis.get("layer", "core"),
                "verdict": axis_verdict,
                "forward_winner": forward_winner,
                "swapped_winner_raw": swapped_raw,
                "swapped_winner_mapped": swapped_mapped,
                "swap_consistent": swap_consistent,
                "rationale": rationale,
                "findings": findings,
            }
        ]
    }


def synthesizer(state: DesignCritiqueState) -> Dict[str, Any]:
    """규칙 층과 축 판정 층 결과를 종합해 최종 판정(점수 없음)을 내린다."""
    rule_results = state.get("rule_results") or {}
    axis_results = state.get("axis_results") or []
    pack_mode = state.get("pack_mode") or "axes"

    rule_findings: List[Dict[str, str]] = list(rule_results.get("findings") or [])
    axis_findings: List[Dict[str, str]] = []
    for ax in axis_results:
        axis_findings.extend(ax.get("findings") or [])

    if pack_mode == "rules_only" or not axis_results:
        a_ok = bool(rule_results.get("candidate_a_passed", True))
        b_ok = bool(rule_results.get("candidate_b_passed", True))
        if not a_ok and not b_ok:
            verdict: Verdict = "BOTH_REJECT"
            verdict_reason = "rules_only_both_blocked"
        elif a_ok and not b_ok:
            verdict = "A"
            verdict_reason = "rules_only_b_blocked"
        elif b_ok and not a_ok:
            verdict = "B"
            verdict_reason = "rules_only_a_blocked"
        else:
            verdict = "TIE"
            verdict_reason = "rules_only_both_passed"
        forward_verdict: Verdict = verdict
        swapped_verdict_mapped: Verdict = verdict
        swap_consistent = True
    else:
        forward_verdict = aggregate_axis_verdicts(
            [str(r.get("forward_winner", "NEEDS_HUMAN")) for r in axis_results]
        )
        swapped_verdict_mapped = aggregate_axis_verdicts(
            [str(r.get("swapped_winner_mapped", "NEEDS_HUMAN")) for r in axis_results]
        )
        all_axes_swap_consistent = all(bool(r.get("swap_consistent", False)) for r in axis_results)
        swap_consistent = (
            forward_verdict == swapped_verdict_mapped and all_axes_swap_consistent
        )

        if forward_verdict == "NEEDS_HUMAN" or swapped_verdict_mapped == "NEEDS_HUMAN":
            verdict = "NEEDS_HUMAN"
            verdict_reason = "axis_divergence"
        elif not swap_consistent:
            verdict = "NEEDS_HUMAN"
            verdict_reason = "swap_reversal"
        else:
            verdict = forward_verdict
            verdict_reason = "consensus"

    all_findings = rule_findings + axis_findings
    summary = (
        f"verdict={verdict} ({verdict_reason}), "
        f"axes={len(axis_results)}, findings={len(all_findings)}"
    )

    output = {
        "asset_type": state.get("asset_type") or "logo",
        "pack_mode": pack_mode,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "forward_verdict": forward_verdict,
        "swapped_verdict_mapped": swapped_verdict_mapped,
        "swap_consistent": swap_consistent,
        "axes": axis_results,
        "rules": rule_results,
        "findings": all_findings,
        "summary": summary,
    }

    return {
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "forward_verdict": forward_verdict,
        "swapped_verdict_mapped": swapped_verdict_mapped,
        "swap_consistent": swap_consistent,
        "findings": all_findings,
        "summary": summary,
        "output": output,
        "status": "completed",
    }


def build_design_critique_graph(checkpointer=None):
    builder = StateGraph(DesignCritiqueState)

    builder.add_node("prepare_context", prepare_context)
    builder.add_node("rules_layer", rules_layer)
    builder.add_node("judge_axis", judge_axis)
    builder.add_node("synthesizer", synthesizer)

    builder.add_edge(START, "prepare_context")
    builder.add_edge("prepare_context", "rules_layer")
    builder.add_conditional_edges(
        "rules_layer",
        route_to_axes,
        ["judge_axis", "synthesizer"],
    )
    builder.add_edge("judge_axis", "synthesizer")
    builder.add_edge("synthesizer", END)

    return builder.compile(checkpointer=checkpointer)


graph = build_design_critique_graph()
