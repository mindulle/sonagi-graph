"""taste/asset_prefs — 자산별 제안·선택·거절 취향 원장 (T4 / GRAPH-38).

에셋(스타일·이미지·마크·팔레트)마다 `proposed / chosen / rejected` 횟수와
맥락 키워드·이유 태그를 `asset_prefs.json`에 축적한다.
카탈로그 원본(`catalog.json`)은 절대 수정하지 않는 별도 선호 레이어다.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from graphs.taste import profile as taste_profile

ASSET_PREFS_NAME = "asset_prefs.json"
ASSET_PREFS_VERSION = 1

#: 계속 외면받았을 때 `demoted`로 내리는 최소 거절 횟수(선택 0건 기준).
DEMOTE_REJECTED_THRESHOLD = 3


def asset_prefs_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    return taste_profile.taste_dir(environ) / ASSET_PREFS_NAME


def initial_asset_prefs(now: Optional[str] = None) -> Dict[str, Any]:
    return {
        "version": ASSET_PREFS_VERSION,
        "updated_at": now or taste_profile._now(),
        "assets": {},
    }


def load_asset_prefs(
    path: Optional[Path] = None,
    *,
    create: bool = False,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    target = Path(path) if path is not None else asset_prefs_path(environ)
    if not target.is_file():
        data = initial_asset_prefs()
        if create:
            taste_profile._write_json_atomic(target, data)
        return data
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return initial_asset_prefs()
    if not isinstance(loaded, dict):
        return initial_asset_prefs()
    out = initial_asset_prefs()
    out.update(loaded)
    if not isinstance(out.get("assets"), dict):
        out["assets"] = {}
    return out


def save_asset_prefs(
    data: Mapping[str, Any],
    path: Optional[Path] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Path:
    target = Path(path) if path is not None else asset_prefs_path(environ)
    taste_profile._write_json_atomic(target, data)
    return target


def extract_option_assets(option: Mapping[str, Any]) -> List[Dict[str, str]]:
    """후보 하나에서 추적 대상 자산(스타일·이미지·마크·팔레트)을 뽑는다."""
    found: List[Dict[str, str]] = []
    seen = set()

    def _add(kind: str, raw_id: Any) -> None:
        val = str(raw_id or "").strip()
        if not val:
            return
        aid = f"{kind}:{val}" if ":" not in val else val
        if aid not in seen:
            seen.add(aid)
            found.append({"asset_id": aid, "kind": kind, "name": val})

    _add("style", option.get("style_id") or option.get("style"))
    for key in ("asset", "image", "png", "svg"):
        val = option.get(key)
        if isinstance(val, str) and val.strip():
            _add("image", val.strip())
            break
    if option.get("family"):
        _add("mark", option.get("family"))
    axes = option.get("axes")
    if isinstance(axes, Mapping) and axes.get("palette"):
        _add("palette", axes.get("palette"))
    if option.get("palette"):
        _add("palette", option.get("palette"))
    return found


def _extract_context_keywords(record: Mapping[str, Any], option: Mapping[str, Any]) -> List[str]:
    keywords: List[str] = []
    for raw in (option.get("direction"), record.get("brief")):
        if isinstance(raw, str) and raw.strip():
            for token in raw.replace("—", " ").replace(",", " ").split():
                tok = token.strip()
                if len(tok) >= 2 and tok not in keywords:
                    keywords.append(tok)
                if len(keywords) >= 8:
                    return keywords
    return keywords


def classify_asset_status(entry: Mapping[str, Any]) -> str:
    """축적된 제안/선택/거절 수로 자산 상태(`preferred`·`demoted`·`neutral`)를 정한다."""
    proposed = int(entry.get("proposed", 0) or 0)
    chosen = int(entry.get("chosen", 0) or 0)
    rejected = int(entry.get("rejected", 0) or 0)
    if chosen >= 1 and chosen >= rejected:
        return "preferred"
    if chosen == 0 and rejected >= DEMOTE_REJECTED_THRESHOLD:
        return "demoted"
    if proposed >= 4 and (chosen / max(1, proposed)) < 0.2:
        return "demoted"
    return "neutral"


def record_decision_assets(
    prefs: Dict[str, Any],
    record: Mapping[str, Any],
) -> Dict[str, Any]:
    """단일 결정 기록을 `asset_prefs` 사전에 누적 반영한다."""
    assets_map = prefs.setdefault("assets", {})
    selected_ids = set(
        str(x) for x in (record.get("selected") or record.get("selection") or ()) if x
    )
    if not selected_ids:
        return prefs

    reason_tags = [
        str(t).strip()
        for t in (record.get("reason_tags") or record.get("tags") or ())
        if isinstance(t, str) and t.strip()
    ]

    for option in record.get("options") or ():
        if not isinstance(option, Mapping):
            continue
        oid = str(option.get("id") or "")
        is_chosen = oid in selected_ids
        ctx_words = _extract_context_keywords(record, option)

        for asset_info in extract_option_assets(option):
            aid = asset_info["asset_id"]
            entry = assets_map.setdefault(
                aid,
                {
                    "asset_id": aid,
                    "kind": asset_info["kind"],
                    "name": asset_info["name"],
                    "proposed": 0,
                    "chosen": 0,
                    "rejected": 0,
                    "context_keywords": {},
                    "reason_tags": {},
                    "status": "neutral",
                },
            )
            entry["proposed"] += 1
            if is_chosen:
                entry["chosen"] += 1
                tag_map = entry.setdefault("reason_tags", {})
                for tag in reason_tags:
                    tag_map[tag] = int(tag_map.get(tag, 0)) + 1
            else:
                entry["rejected"] += 1

            kw_map = entry.setdefault("context_keywords", {})
            for kw in ctx_words:
                kw_map[kw] = int(kw_map.get(kw, 0)) + 1

            entry["status"] = classify_asset_status(entry)

    prefs["updated_at"] = taste_profile._now()
    return prefs


def build_asset_prefs(
    records: Sequence[Mapping[str, Any]],
    *,
    base: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """결정 원장 전체에서 자산 선호 레이어를 생성한다."""
    prefs = initial_asset_prefs()
    if base and isinstance(base.get("assets"), Mapping):
        prefs["assets"] = {k: dict(v) for k, v in base["assets"].items()}
    for rec in records:
        record_decision_assets(prefs, rec)
    return prefs


def preference_boost(asset_id: str, prefs: Mapping[str, Any]) -> float:
    """향후 렌더 선택 단계에서 참조할 선호 가중치(-0.2 ~ +0.2)."""
    assets_map = prefs.get("assets")
    if not isinstance(assets_map, Mapping):
        return 0.0
    entry = assets_map.get(asset_id)
    if not isinstance(entry, Mapping):
        return 0.0
    status = entry.get("status")
    if status == "preferred":
        return 0.2
    if status == "demoted":
        return -0.2
    return 0.0
