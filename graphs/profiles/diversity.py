"""변주 다양성 쿼터 · 특징 거리 중복 교체 · 자산 반복 억제 (T6 / GRAPH-40).

변주 상한(`variant_cap`)만 올렸을 때 `styles_per_direction` 기본값(2) 때문에
개수가 모자라거나, 레이아웃·여백·색·자산이 거의 같은 후보가 반복되는 문제를 막는다.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

DEFAULT_MIN_FEATURE_DISTANCE = 0.25
DEFAULT_MAX_ASSET_REUSE = 2


def resolve_styles_per_direction(
    variant_cap: int,
    direction_count: int,
    *,
    combos_per_style: int = 1,
    explicit: Any = None,
) -> int:
    """`variant_cap`에서 방향당 스타일 수를 자동 산출한다.

    명시적으로 `styles_per_direction`이 주어지면 그 값을 존중한다(최소 1).
    """
    if explicit is not None:
        return max(1, int(explicit))
    cap = max(1, int(variant_cap))
    dirs = max(1, int(direction_count))
    combos = max(1, int(combos_per_style))
    needed_styles = math.ceil(cap / combos)
    return max(1, math.ceil(needed_styles / dirs))


def _norm_token(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip().lower() for v in value if str(v).strip())
    return str(value).strip().lower()


def extract_asset_key(
    item: Mapping[str, Any], style_meta: Optional[Mapping[str, Any]] = None
) -> str:
    """후보 또는 스타일 메타에서 대표 자산 식별자를 뽑는다."""
    for src in (item, style_meta or {}):
        for key in ("asset", "image", "hero_image", "media_asset", "motif_asset"):
            val = _norm_token(src.get(key))
            if val:
                return val
        assets = src.get("assets")
        if isinstance(assets, Sequence) and not isinstance(assets, (str, bytes)) and assets:
            val = _norm_token(assets[0])
            if val:
                return val
    axes = item.get("axes") or (style_meta or {}).get("axes") or {}
    if isinstance(axes, Mapping):
        val = _norm_token(axes.get("asset") or axes.get("image"))
        if val:
            return val
    return ""


def variant_feature_vector(
    item: Mapping[str, Any], style_meta: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """레이아웃·여백·색·자산 4축의 정규화된 특징 벡터를 만든다."""
    axes: Dict[str, Any] = {}
    if isinstance(style_meta, Mapping) and isinstance(style_meta.get("axes"), Mapping):
        axes.update(style_meta["axes"])
    if isinstance(item.get("axes"), Mapping):
        axes.update(item["axes"])

    layout = "|".join(
        part
        for part in (
            _norm_token(axes.get("layout") or item.get("layout")),
            _norm_token(item.get("breakpoint")),
        )
        if part
    )
    spacing = "|".join(
        part
        for part in (
            _norm_token(axes.get("ornament") or item.get("spacing")),
            _norm_token(axes.get("type")),
        )
        if part
    )
    if item.get("whitespace_ratio") is not None:
        try:
            bucket = round(float(item["whitespace_ratio"]) * 10)
            spacing = f"{spacing}|ws:{bucket}" if spacing else f"ws:{bucket}"
        except (TypeError, ValueError):
            pass

    color = "|".join(
        part
        for part in (
            _norm_token(axes.get("palette") or item.get("palette")),
            _norm_token(item.get("theme")),
        )
        if part
    )
    asset = extract_asset_key(item, style_meta)
    if not asset and item.get("motifs"):
        asset = _norm_token(item.get("motifs"))

    return {
        "layout": layout,
        "spacing": spacing,
        "color": color,
        "asset": asset,
    }


def _axis_distance(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if not a or not b:
        return 0.5
    tokens_a = {t for t in a.replace("|", ",").split(",") if t}
    tokens_b = {t for t in b.replace("|", ",").split(",") if t}
    if not tokens_a or not tokens_b:
        return 1.0
    union = tokens_a | tokens_b
    inter = tokens_a & tokens_b
    return 1.0 - (len(inter) / len(union))


def feature_distance(
    vec_a: Mapping[str, Any], vec_b: Mapping[str, Any]
) -> float:
    """두 특징 벡터 간의 정규화된 쌍별 거리 (0.0 ~ 1.0)."""
    axes = ("layout", "spacing", "color", "asset")
    total = sum(_axis_distance(str(vec_a.get(k, "")), str(vec_b.get(k, ""))) for k in axes)
    return round(total / len(axes), 4)


def _pick_replacement_asset(
    asset_pool: Sequence[str],
    asset_counts: Counter,
    max_reuse: int,
) -> Optional[str]:
    """N회 미만으로 쓰인 대체 자산을 고른다."""
    candidates = [a for a in asset_pool if asset_counts[a] < max_reuse]
    if not candidates:
        return None
    candidates.sort(key=lambda a: (asset_counts[a], a))
    return candidates[0]


def select_diverse_variants(
    candidates: Sequence[Mapping[str, Any]],
    *,
    cap: int,
    style_lookup: Optional[Mapping[str, Mapping[str, Any]]] = None,
    min_distance: float = DEFAULT_MIN_FEATURE_DISTANCE,
    max_asset_reuse: int = DEFAULT_MAX_ASSET_REUSE,
    asset_pool: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """쌍별 특징 거리와 자산 반복 상한을 적용해 다양한 후보를 고른다.

    최소 거리 미달이거나 같은 자산이 `max_asset_reuse` 이상 쓰인 후보는
    풀의 다음 후보로 교체한다. 풀이 소진되어 `cap`에 못 미치면 보류 목록에서
    기존 선택과 가장 거리가 먼 순으로 채운다.
    """
    cap = max(0, int(cap))
    if cap == 0 or not candidates:
        return []

    lookup = style_lookup or {}
    pool_assets = [str(a) for a in (asset_pool or ()) if str(a).strip()]

    selected: List[Dict[str, Any]] = []
    selected_vecs: List[Dict[str, Any]] = []
    selected_keys = set()
    asset_counts: Counter = Counter()
    deferred: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    for raw in candidates:
        cand = dict(raw)
        key = (cand.get("style"), cand.get("breakpoint"), cand.get("theme"))
        if key in selected_keys:
            continue

        style_meta = lookup.get(str(cand.get("style_id") or "")) or lookup.get(
            str(cand.get("style") or "")
        )
        # 스타일 메타의 axes/asset 정보를 보존해 후속 단계(콘택트 시트 등)에서도 참조 가능하게 한다.
        if style_meta and "axes" not in cand and isinstance(style_meta.get("axes"), Mapping):
            cand["axes"] = dict(style_meta["axes"])

        asset_key = extract_asset_key(cand, style_meta)
        if asset_key and max_asset_reuse > 0 and asset_counts[asset_key] >= max_asset_reuse:
            replacement = _pick_replacement_asset(pool_assets, asset_counts, max_asset_reuse)
            if replacement:
                cand["asset"] = replacement
                asset_key = replacement
            else:
                vec = variant_feature_vector(cand, style_meta)
                deferred.append((cand, vec))
                continue

        vec = variant_feature_vector(cand, style_meta)
        if selected_vecs and min_distance > 0:
            nearest = min(feature_distance(vec, sv) for sv in selected_vecs)
            if nearest < min_distance:
                deferred.append((cand, vec))
                continue

        if asset_key:
            cand.setdefault("asset", asset_key)
            asset_counts[asset_key] += 1
        selected.append(cand)
        selected_vecs.append(vec)
        selected_keys.add(key)
        if len(selected) >= cap:
            return selected

    # 풀이 부족해 cap에 못 미치면 보류 후보 중 기존 선택과 가장 거리가 먼 순으로 채운다.
    while len(selected) < cap and deferred:
        best_idx = 0
        best_rank = (-1.0, 0)
        for idx, (cand, vec) in enumerate(deferred):
            key = (cand.get("style"), cand.get("breakpoint"), cand.get("theme"))
            if key in selected_keys:
                continue
            nearest = (
                min(feature_distance(vec, sv) for sv in selected_vecs)
                if selected_vecs
                else 1.0
            )
            asset_key = vec.get("asset", "")
            reuse_penalty = -asset_counts[asset_key] if asset_key else 0
            rank = (nearest, reuse_penalty)
            if rank > best_rank:
                best_rank = rank
                best_idx = idx
        cand, vec = deferred.pop(best_idx)
        key = (cand.get("style"), cand.get("breakpoint"), cand.get("theme"))
        if key in selected_keys:
            continue
        asset_key = vec.get("asset", "")
        if asset_key and max_asset_reuse > 0 and asset_counts[asset_key] >= max_asset_reuse:
            replacement = _pick_replacement_asset(pool_assets, asset_counts, max_asset_reuse)
            if replacement:
                cand["asset"] = replacement
                asset_key = replacement
        if asset_key:
            cand.setdefault("asset", asset_key)
            asset_counts[asset_key] += 1
        selected.append(cand)
        selected_vecs.append(vec)
        selected_keys.add(key)

    return selected


def diversity_summary(
    items: Sequence[Mapping[str, Any]],
    *,
    min_distance: float = DEFAULT_MIN_FEATURE_DISTANCE,
) -> Dict[str, Any]:
    """후보/렌더 목록의 다양성 점수와 중복 그룹 수를 계산한다."""
    total = len(items)
    if total == 0:
        return {
            "total": 0,
            "unique_groups": 0,
            "duplicate_groups": 0,
            "duplicate_items": 0,
            "diversity_score": 100,
        }

    vecs = [variant_feature_vector(item) for item in items]
    parent = list(range(total))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(total):
        for j in range(i + 1, total):
            if feature_distance(vecs[i], vecs[j]) < min_distance:
                union(i, j)

    groups: Counter = Counter(find(i) for i in range(total))
    unique_groups = len(groups)
    duplicate_groups = sum(1 for size in groups.values() if size >= 2)
    duplicate_items = total - unique_groups
    diversity_score = round((unique_groups / total) * 100)

    return {
        "total": total,
        "unique_groups": unique_groups,
        "duplicate_groups": duplicate_groups,
        "duplicate_items": duplicate_items,
        "diversity_score": diversity_score,
    }
