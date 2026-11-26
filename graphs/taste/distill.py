"""taste/distill — 결정 원장을 규칙·프로필 갱신안·한국어 취향 노트로 증류한다 (T2 / GRAPH-36).

- 표본 30건 미만(`EARLY_THRESHOLD`)이면 통계적 특징 편향(`feature_bias`)을 만들지 않고
  명시 태그·스타일 승률 규칙만 정리하며 `confidence="early"`로 표시한다.
- 표본 30건 이상이면 선택 vs 거절(미선택) 후보의 수치 특징 평균 차이를 계산한다.
- 브랜드 SSOT(`sonagi-assets`)를 건드리지 않고, 사람이 승인한 뒤에만
  `profile.json`과 월간 위키 노트를 기록한다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from graphs.taste import ledger as taste_ledger
from graphs.taste import profile as taste_profile

EARLY_THRESHOLD = taste_profile.EARLY_THRESHOLD

NUMERIC_FEATURES = (
    "whitespace_ratio",
    "distinct_color_count",
    "max_contrast",
    "element_density",
    "alignment_edges",
)


def _now_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def compute_tag_weights(records: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    """태그 빈도 기반 상대 가중치(최대=1.0)를 계산한다."""
    counts = taste_ledger.tag_counts(records)
    if not counts:
        # 테스트·외부 입력이 `tags` 키를 쓴 경우도 보조로 집계한다.
        fallback: Counter = Counter()
        for rec in records:
            for tag in rec.get("tags") or ():
                if isinstance(tag, str) and tag.strip():
                    fallback[tag.strip()] += 1
        counts = dict(fallback)
    if not counts:
        return {}
    peak = max(counts.values())
    if peak <= 0:
        return {}
    return {
        tag: round(count / peak, 4)
        for tag, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    }


def compute_style_winrate(records: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    """스타일별 승률(`wins / appearances`)을 계산한다."""
    stats = taste_ledger.style_stats(records)
    result: Dict[str, float] = {}
    for style, row in sorted(stats.items()):
        rate = row.get("rate")
        if isinstance(rate, (int, float)):
            result[style] = round(float(rate), 4)
            continue
        appearances = int(row.get("appearances", 0) or 0)
        wins = int(row.get("wins", 0) or 0)
        if appearances > 0:
            result[style] = round(wins / appearances, 4)
    return result


def _numeric_features(option: Mapping[str, Any]) -> Dict[str, float]:
    feats = option.get("features")
    if not isinstance(feats, Mapping):
        return {}
    out: Dict[str, float] = {}
    for key in NUMERIC_FEATURES:
        val = feats.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = float(val)
    return out


def compute_feature_bias(
    records: Sequence[Mapping[str, Any]],
    *,
    early_threshold: int = EARLY_THRESHOLD,
) -> Dict[str, float]:
    """선택 vs 거절 후보의 수치 특징 차이를 계산한다.

    표본이 `early_threshold`(30건) 미만이면 빈 dict를 돌려준다(과신 방지).
    """
    if len(records) < early_threshold:
        return {}

    sel_sums: Counter = Counter()
    sel_counts: Counter = Counter()
    rej_sums: Counter = Counter()
    rej_counts: Counter = Counter()

    for rec in records:
        selected_ids = set(rec.get("selected") or rec.get("selection") or ())
        for opt in rec.get("options") or ():
            if not isinstance(opt, Mapping):
                continue
            oid = str(opt.get("id") or "")
            feats = _numeric_features(opt)
            if not feats:
                continue
            is_sel = oid in selected_ids
            for k, v in feats.items():
                if is_sel:
                    sel_sums[k] += v
                    sel_counts[k] += 1
                else:
                    rej_sums[k] += v
                    rej_counts[k] += 1

    bias: Dict[str, float] = {}
    for key in NUMERIC_FEATURES:
        if sel_counts[key] > 0 and rej_counts[key] > 0:
            sel_mean = sel_sums[key] / sel_counts[key]
            rej_mean = rej_sums[key] / rej_counts[key]
            bias[key] = round(sel_mean - rej_mean, 4)
    return bias


def collect_example_images(
    records: Sequence[Mapping[str, Any]], *, limit: int = 3
) -> Dict[str, List[Dict[str, str]]]:
    """원장에서 선택/거절 예시 이미지(아티팩트 경로)를 추출한다."""
    selected_examples: List[Dict[str, str]] = []
    rejected_examples: List[Dict[str, str]] = []
    seen_sel = set()
    seen_rej = set()

    for rec in reversed(list(records)):
        selected_ids = set(rec.get("selected") or rec.get("selection") or ())
        reason = str(rec.get("reason") or "").strip()
        for opt in rec.get("options") or ():
            if not isinstance(opt, Mapping):
                continue
            oid = str(opt.get("id") or "")
            style = str(opt.get("style") or "")
            img = ""
            for key in ("png", "image", "thumbnail", "svg"):
                val = opt.get(key)
                if isinstance(val, str) and val.strip():
                    img = val.strip()
                    break
            if not img and isinstance(opt.get("artifacts"), Mapping):
                for key in ("png", "svg", "card", "tile"):
                    val = opt["artifacts"].get(key)
                    if isinstance(val, str) and val.strip():
                        img = val.strip()
                        break
            if not img:
                continue
            entry = {
                "id": oid,
                "style": style,
                "path": img,
                "reason": reason,
            }
            if oid in selected_ids:
                if img not in seen_sel and len(selected_examples) < limit:
                    selected_examples.append(entry)
                    seen_sel.add(img)
            else:
                if img not in seen_rej and len(rejected_examples) < limit:
                    rejected_examples.append(entry)
                    seen_rej.add(img)
        if len(selected_examples) >= limit and len(rejected_examples) >= limit:
            break

    return {"selected": selected_examples, "rejected": rejected_examples}


def render_taste_note(
    *,
    month: str,
    decisions_count: int,
    confidence: str,
    tag_weights: Mapping[str, float],
    style_winrate: Mapping[str, float],
    feature_bias: Mapping[str, float],
    examples: Mapping[str, Sequence[Mapping[str, str]]],
) -> str:
    """사람이 읽고 검토할 수 있는 한국어 취향 노트 마크다운을 생성한다."""
    lines: List[str] = [
        f"# 취향 증류 노트 ({month})",
        "",
        f"- **결정 표본**: {decisions_count}건",
        f"- **신뢰도**: `{confidence}`"
        + (" (30건 미만이라 태그·승률 규칙만 반영)" if confidence == "early" else ""),
        "",
        "## 1. 명시 태그 가중치",
    ]
    if tag_weights:
        for tag, weight in list(tag_weights.items())[:10]:
            lines.append(f"- `{tag}`: {weight:.2f}")
    else:
        lines.append("- 기록된 태그가 아직 없습니다.")

    lines.extend(["", "## 2. 스타일 승률 (선택 / 등장)"])
    if style_winrate:
        ranked_styles = sorted(style_winrate.items(), key=lambda kv: (-kv[1], kv[0]))
        for style, rate in ranked_styles[:10]:
            lines.append(f"- `{style}`: {rate * 100:.1f}%")
    else:
        lines.append("- 집계된 스타일 승률이 아직 없습니다.")

    lines.extend(["", "## 3. 특징 편향 (선택 vs 거절 평균 차이)"])
    if feature_bias:
        for key, diff in feature_bias.items():
            sign = "+" if diff >= 0 else ""
            lines.append(f"- `{key}`: {sign}{diff:.4f}")
    else:
        lines.append("- 표본 30건 미만(또는 특징 수치 없음)으로 특징 편향은 보류합니다.")

    lines.extend(["", "## 4. 선택 / 거절 예시 이미지"])
    sel = examples.get("selected") or ()
    rej = examples.get("rejected") or ()
    lines.append("### 선택된 예시")
    if sel:
        for item in sel:
            note = f" — \"{item['reason']}\"" if item.get("reason") else ""
            lines.append(f"- `{item.get('id')}` ({item.get('style')}): `{item.get('path')}`{note}")
    else:
        lines.append("- 이미지 경로가 포함된 선택 예시가 없습니다.")

    lines.append("### 거절(미선택) 예시")
    if rej:
        for item in rej:
            lines.append(f"- `{item.get('id')}` ({item.get('style')}): `{item.get('path')}`")
    else:
        lines.append("- 이미지 경로가 포함된 거절 예시가 없습니다.")

    lines.append("")
    return "\n".join(lines)


def build_distillation(
    records: Sequence[Mapping[str, Any]],
    *,
    current_profile: Optional[Mapping[str, Any]] = None,
    month: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """원장 기록에서 `profile.json` 갱신안과 한국어 노트를 만든다."""
    decisions_count = len(records)
    confidence = "early" if decisions_count < EARLY_THRESHOLD else "ready"
    tag_weights = compute_tag_weights(records)
    style_winrate = compute_style_winrate(records)
    feature_bias = compute_feature_bias(records, early_threshold=EARLY_THRESHOLD)
    examples = collect_example_images(records)
    target_month = month or _now_month()

    note_markdown = render_taste_note(
        month=target_month,
        decisions_count=decisions_count,
        confidence=confidence,
        tag_weights=tag_weights,
        style_winrate=style_winrate,
        feature_bias=feature_bias,
        examples=examples,
    )

    proposal = dict(taste_profile.initial_profile(now=now))
    if current_profile:
        proposal.update(current_profile)
    proposal.update(
        {
            "version": taste_profile.PROFILE_VERSION,
            "updated_at": now or taste_profile._now(),
            "decisions": decisions_count,
            "tag_weights": tag_weights,
            "style_winrate": style_winrate,
            "feature_bias": feature_bias,
            "confidence": confidence,
            "notes": f"{target_month} 증류 ({decisions_count}건, {confidence})",
        }
    )

    return {
        "month": target_month,
        "decisions_count": decisions_count,
        "confidence": confidence,
        "profile_proposal": proposal,
        "note_markdown": note_markdown,
        "examples": examples,
    }
