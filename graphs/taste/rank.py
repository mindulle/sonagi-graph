"""taste/rank — 취향 프로필로 후보 순서와 이유를 만든다 (GRAPH-35).

이 모듈은 "정답"을 고르지 않는다. 이미 남긴 **내 선택**을 근거로 다음 후보를
어떤 순서로 보여줄지만 정하고, 왜 그렇게 정했는지 사람 말로 밝힌다.

신호는 세 가지다.

| 신호 | 어디서 오나 | 언제 쓰나 |
| --- | --- | --- |
| 명시 태그 | 승인할 때 남긴 `--tag` | 항상 |
| 스타일 승률 | 결정 기록의 선택/등장 | 항상 |
| 특징 편향 | `profile.json`의 `feature_bias` | 결정 30건 이상 |

**30건 미만이면 특징 편향은 보지 않는다.** 표본이 적을 때 회귀·평균을 만들면
없는 취향을 있는 것처럼 보이게 하기 때문이다. 이때 신뢰도는 `early`다.
신호가 하나도 없으면 id 순으로 두고, 동점도 id 순으로 깬다(결정론).

태그 가중치는 "가장 자주 나온 태그 = 1.0"으로 맞춘 상대값이다. 스타일 승률은
`선택 수 / 등장 수`다. 두 신호는 같은 무게로 평균한다 — 한쪽이 없다면 있는
신호만 쓴다.

사용::

    from graphs.taste import rank

    result = rank.rank_options(
        options=[{"id": "a", "style": "forest-paper"},
                 {"id": "b", "style": "star-editorial"}],
        profile={"decisions": 4, "style_winrate": {"forest-paper": 0.75}},
        decisions=[],
    )
    result["ranked"]        # ["a", "b"]
    result["explanations"]  # ["..."]
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from graphs.taste import ledger as taste_ledger
from graphs.taste import profile as taste_profile

EARLY_THRESHOLD = taste_profile.EARLY_THRESHOLD


def _features_of(option: Mapping[str, Any]) -> Mapping[str, Any]:
    value = option.get("features")
    return value if isinstance(value, Mapping) else {}


def _option_tags(option: Mapping[str, Any]) -> List[str]:
    """후보에 붙은 태그. 최상위 `tags`와 `features.tags`를 모두 본다."""
    tags: List[str] = []
    for raw in (option.get("tags"), _features_of(option).get("tags")):
        if isinstance(raw, (list, tuple, set)):
            for item in raw:
                if isinstance(item, str) and item and item not in tags:
                    tags.append(item)
    return tags


def _normalized(weights: Mapping[str, Any]) -> Dict[str, float]:
    """숫자 가중치만 남기고 최댓값을 1.0으로 맞춘다."""
    numeric = {
        str(key): float(value)
        for key, value in weights.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    }
    if not numeric:
        return {}
    top = max(numeric.values())
    return {key: round(value / top, 6) for key, value in numeric.items()}


def _style_rates(
    profile: Optional[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> Dict[str, float]:
    """스타일 승률. 프로필 값이 있으면 그쪽을 우선한다(증류 결과 존중)."""
    rates: Dict[str, float] = {}
    for style, stats in taste_ledger.style_stats(records).items():
        rate = stats.get("rate")
        if isinstance(rate, (int, float)):
            rates[style] = float(rate)
    stored = (profile or {}).get("style_winrate")
    if isinstance(stored, Mapping):
        for style, rate in stored.items():
            if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                rates[str(style)] = max(0.0, min(1.0, float(rate)))
    return rates


def _tag_weights(
    profile: Optional[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> Dict[str, float]:
    stored = (profile or {}).get("tag_weights")
    if isinstance(stored, Mapping) and stored:
        return _normalized(stored)
    return _normalized(taste_ledger.tag_counts(records))


def _feature_scores(
    options: Sequence[Mapping[str, Any]], bias: Mapping[str, Any]
) -> Dict[str, float]:
    """특징 편향(30건 이상에서만 호출) → 후보 id별 점수(0~1).

    특징마다 후보들 사이의 min-max 위치를 구하고, `prefer`가 `low`면 뒤집는다.
    특징별 위치는 그 특징의 `weight`로 가중 평균한다.
    """
    parts: Dict[str, List[Tuple[float, float]]] = {}
    for name, spec in bias.items():
        if not isinstance(spec, Mapping):
            continue
        values = {
            str(option.get("id")): float(raw)
            for option in options
            if isinstance(
                raw := _features_of(option).get(name),
                (int, float),
            )
            and not isinstance(raw, bool)
        }
        if len(values) < 2 or max(values.values()) == min(values.values()):
            continue
        prefer = str(spec.get("prefer") or "high")
        weight = spec.get("weight", 1.0)
        weight = float(weight) if isinstance(weight, (int, float)) else 1.0
        low, high = min(values.values()), max(values.values())
        for option_id, value in values.items():
            position = (value - low) / (high - low)
            if prefer == "low":
                position = 1.0 - position
            parts.setdefault(option_id, []).append((position, max(weight, 0.0)))
    scores: Dict[str, float] = {}
    for option_id, rows in parts.items():
        total_weight = sum(weight for _position, weight in rows)
        if total_weight > 0:
            scores[option_id] = (
                sum(position * weight for position, weight in rows) / total_weight
            )
    return scores


def rank_options(
    options: Sequence[Mapping[str, Any]],
    profile: Optional[Mapping[str, Any]] = None,
    decisions: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """후보 → `{ranked, explanations, scores, confidence, …}`.

    `options`는 `id`가 있어야 하고, `style`·`tags`·`features`는 있으면 쓴다.
    같은 점수면 id 오름차순으로 고정한다(실행마다 순서가 달라지지 않게).
    """
    records = list(decisions or [])
    profile = profile or {}
    decisions_count = (
        len(records)
        if records
        else int(profile.get("decisions") or 0)
        if isinstance(profile.get("decisions"), (int, float))
        else 0
    )
    early = decisions_count < EARLY_THRESHOLD
    tag_weights = _tag_weights(profile, records)
    style_rates = _style_rates(profile, records)
    style_details = taste_ledger.style_stats(records)

    bias = profile.get("feature_bias")
    use_bias = not early and isinstance(bias, Mapping) and bool(bias)
    feature_scores = _feature_scores(options, bias) if use_bias else {}

    scores: Dict[str, float] = {}
    per_option: Dict[str, Dict[str, Any]] = {}
    for option in options:
        option_id = str(option.get("id"))
        tags = _option_tags(option)
        matched_tags = [tag for tag in tags if tag in tag_weights]
        tag_score = (
            sum(tag_weights[tag] for tag in matched_tags) / len(matched_tags)
            if matched_tags
            else None
        )
        style = option.get("style")
        style_score = style_rates.get(str(style)) if style else None
        feature_score = feature_scores.get(option_id)
        components = [
            value
            for value in (tag_score, style_score, feature_score)
            if value is not None
        ]
        score = round(sum(components) / len(components), 6) if components else 0.0
        scores[option_id] = score
        per_option[option_id] = {
            "tags": matched_tags,
            "style": style,
            "style_rate": style_score,
            "tag_score": tag_score,
            "feature_score": feature_score,
        }

    ranked = [
        str(option.get("id"))
        for option in sorted(
            options,
            key=lambda item: (-scores[str(item.get("id"))], str(item.get("id"))),
        )
    ]

    explanations: List[str] = []
    if decisions_count == 0:
        explanations.append("아직 취향 기록이 없어 id 순으로 정렬했습니다.")
    else:
        explanations.append(f"지금까지 선택 {decisions_count}건을 참고했습니다.")
        if early:
            explanations.append(
                f"결정이 {EARLY_THRESHOLD}건 미만이라 초기(early) 단계입니다. "
                "태그와 스타일 승률만 봤습니다."
            )
        elif use_bias:
            explanations.append(
                f"결정이 {EARLY_THRESHOLD}건 이상이라 특징 편향까지 함께 봤습니다."
            )
        else:
            explanations.append(
                f"결정이 {EARLY_THRESHOLD}건 이상이라 태그와 스타일 승률로 판단했습니다."
            )

    recent = records[-3:]
    recent_tags = list(taste_ledger.tag_counts(recent).items())
    if recent_tags:
        top_tags = ", ".join(f"'{tag}' {count}번" for tag, count in recent_tags[:2])
        explanations.append(f"최근 선택 {len(recent)}건에서 {top_tags} 나왔습니다.")

    used_styles: List[str] = []
    for option in options:
        style = option.get("style")
        if style and str(style) not in used_styles and str(style) in style_rates:
            used_styles.append(str(style))
    for style in used_styles:
        rate = style_rates[style]
        detail = style_details.get(style) or {}
        if detail.get("appearances"):
            explanations.append(
                f"{style} 스타일은 {detail['appearances']}번 중 "
                f"{detail.get('wins', 0)}번 선택됐습니다(승률 {rate:.2f})."
            )
        else:
            explanations.append(f"{style} 스타일 승률 {rate:.2f}.")

    if ranked and decisions_count > 0:
        top_id = ranked[0]
        reasons: List[str] = []
        info = per_option[top_id]
        if info["tags"]:
            reasons.append("'" + "', '".join(info["tags"]) + "' 태그와 맞습니다")
        if info["style_rate"] is not None:
            reasons.append(f"{info['style']} 승률 {info['style_rate']:.2f}")
        if info["feature_score"] is not None and not reasons:
            reasons.append("특징 편향과 가깝습니다")
        if not reasons:
            reasons.append("취향 신호가 아직 없습니다")
        explanations.append(f"1순위는 '{top_id}'입니다 — {', '.join(reasons)}.")

    return {
        "confidence": "early" if early else "stable",
        "decisions": decisions_count,
        "ranked": ranked,
        "scores": scores,
        "explanations": explanations,
        "basis": {
            "tag_weights": tag_weights,
            "style_winrate": style_rates,
            "recent_tags": dict(recent_tags),
        },
    }


def _one_line_reason(
    option: Mapping[str, Any],
    *,
    score: float,
    confidence: str,
    decisions_count: int,
    style_rates: Mapping[str, float],
    tag_weights: Mapping[str, float],
) -> str:
    """HITL 옵션 카드에 붙일 한 줄 추천 이유를 만든다."""
    prefix = f"초기({confidence}, {decisions_count}건)" if confidence == "early" else f"취향({decisions_count}건)"
    if decisions_count == 0 or score <= 0.0:
        return f"{prefix} — 취향 신호가 없어 기본 품질 순으로 배치했습니다"
    parts: List[str] = []
    style = str(option.get("style") or "")
    if style and style in style_rates:
        parts.append(f"{style} 승률 {style_rates[style]:.2f}")
    matched_tags = [t for t in _option_tags(option) if t in tag_weights]
    if matched_tags:
        parts.append("'" + "', '".join(matched_tags[:2]) + "' 태그 일치")
    if not parts:
        parts.append("특징 편향 점수 반영")
    return f"{prefix} — {', '.join(parts)} 추천"


def rank_top_options(
    options: Sequence[Mapping[str, Any]],
    profile: Optional[Mapping[str, Any]] = None,
    decisions: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """상위 N개 후보의 순서만 취향으로 재정렬하고 추천 이유와 A/B 전후 비교를 붙인다.

    생성 단계는 건드리지 않고, 이미 추려진 상위 N개 목록에만 적용한다.
    취향 점수가 모두 0이면 원래 순서를 그대로 유지한다.
    """
    items = [dict(o) for o in options]
    before_top3 = [str(o.get("id") or "") for o in items[:3]]
    if not items:
        return {
            "options": [],
            "confidence": "early",
            "decisions": 0,
            "explanations": ["후보가 없습니다."],
            "ab": {
                "before_top3": [],
                "after_top3": [],
                "overlap_rate": 1.0,
            },
        }

    result = rank_options(items, profile=profile, decisions=decisions)
    scores = result["scores"]
    confidence = result["confidence"]
    decisions_count = result["decisions"]
    basis = result["basis"]
    style_rates = basis["style_winrate"]
    tag_weights = basis["tag_weights"]

    has_signal = decisions_count > 0 and any(v > 0.0 for v in scores.values())
    if has_signal:
        ordered = [
            item
            for _idx, item in sorted(
                enumerate(items),
                key=lambda pair: (-scores.get(str(pair[1].get("id") or ""), 0.0), pair[0]),
            )
        ]
    else:
        ordered = items

    for opt in ordered:
        oid = str(opt.get("id") or "")
        sc = float(scores.get(oid, 0.0))
        opt["taste_score"] = sc
        opt["taste_confidence"] = confidence
        opt["recommendation_reason"] = _one_line_reason(
            opt,
            score=sc,
            confidence=confidence,
            decisions_count=decisions_count,
            style_rates=style_rates,
            tag_weights=tag_weights,
        )

    after_top3 = [str(o.get("id") or "") for o in ordered[:3]]
    overlap = len(set(before_top3) & set(after_top3)) / max(1, len(before_top3))
    return {
        "options": ordered,
        "confidence": confidence,
        "decisions": decisions_count,
        "explanations": result["explanations"],
        "ab": {
            "before_top3": before_top3,
            "after_top3": after_top3,
            "overlap_rate": round(overlap, 4),
            "confidence": confidence,
        },
    }


def evaluate_top3_agreement(
    ab_record: Mapping[str, Any],
    selection: Sequence[str],
) -> Dict[str, Any]:
    """취향 반영 전후 top-3와 실제 사람 선택의 일치율을 기록한다."""
    before = list(ab_record.get("before_top3") or ())
    after = list(ab_record.get("after_top3") or ())
    picked = {str(s) for s in selection if str(s)}
    if not picked:
        return dict(ab_record)
    before_hits = sum(1 for item in before if item in picked)
    after_hits = sum(1 for item in after if item in picked)
    out = dict(ab_record)
    out["selection"] = sorted(picked)
    out["before_hit_rate"] = round(before_hits / len(picked), 4)
    out["after_hit_rate"] = round(after_hits / len(picked), 4)
    return out
