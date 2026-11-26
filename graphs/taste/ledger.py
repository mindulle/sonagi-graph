"""taste/ledger — 승인 한 건을 취향 결정 기록으로 남긴다 (GRAPH-35).

`graph_cli approve`가 성공하면 그 스레드의 상태를 다시 읽어 **무엇을 보고
무엇을 골랐는지**를 한 줄 JSON으로 덧붙인다. 파일은 append-only다 — 지난
기록을 고쳐 쓰지 않으므로, 나중에 증류(distill)가 잘못돼도 원본은 남는다.

파일 위치::

    SONAGI_TASTE_DIR/decisions.jsonl   # 기본 /home/ubuntu/sonagi-taste/

한 줄 형식::

    {
      "ts": "2026-11-26T00:00:00Z",
      "run_id": "entry-52fa13b0",
      "thread_id": "...",
      "profile": "web",
      "platform": "99designs",
      "brief_sha256": "…",
      "brief_head": "비 오는 날의 차 — 차분하고 …",
      "options": [
        {"id": "forest-paper-desktop-dark", "direction": "…", "style": "…",
         "theme": "dark", "features": {…}, "critic": {…}}
      ],
      "selected": ["…"],
      "rejected": ["…"],
      "reason_tags": ["차분함", "여백"],
      "reason_text": "빈 공간이 편안해서",
      "critic_top": ["…"],
      "agreement_top1": true,
      "artifacts": {"forest-paper-desktop-dark": "sha256…"}
    }

특징은 PNG가 있으면 PNG(픽셀)에서, 없으면 SVG(구조)에서 뽑는다. 파일이 없거나
읽지 못하면 그 후보의 `features`는 빈 사전이다 — 기록 자체를 막지 않는다.
비평 점수(`plan_score`)가 모두 0이면 "비평 점수가 없다"로 보고 `critic_top`을
비운다. 0을 순위로 착각하지 않기 위해서다.

사용::

    from graphs.taste import ledger

    record = ledger.build_record(thread_id="t-1", state=state, values=values,
                                 reason_tags=["차분함"], reason_text="편안해서")
    ledger.append_decision(record)
    ledger.read_decisions(limit=5)
    ledger.summarize(ledger.read_decisions())
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from graphs.taste import features as taste_features
from graphs.taste import profile as taste_profile

DECISIONS_NAME = "decisions.jsonl"
#: `brief_head`에 남기는 글자 수(한 줄).
BRIEF_HEAD_CHARS = 120
#: 비평 상위 몇 개를 `critic_top`에 남길지.
CRITIC_TOP_N = 3
#: 기록에 남기는 findings 최대 개수(한 줄이 지나치게 길어지지 않게).
MAX_FINDINGS = 10


def decisions_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    return taste_profile.taste_dir(environ) / DECISIONS_NAME


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: Any, limit: int = BRIEF_HEAD_CHARS) -> str:
    """줄바꿈·연속 공백을 하나로 접고 앞부분만 남긴다."""
    return " ".join(str(text or "").split())[:limit]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> Optional[str]:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _state_values(state: Any) -> Dict[str, Any]:
    """API 상태(`{"values":…}`)와 값 사전을 모두 받아 값만 꺼낸다."""
    if isinstance(state, Mapping) and isinstance(state.get("values"), Mapping):
        return dict(state["values"])
    if isinstance(state, Mapping):
        return dict(state)
    return {}


def _string_list(value: Any) -> List[str]:
    """문자열 목록만 남기고 순서를 지킨 채 중복을 없앤다."""
    if not isinstance(value, (list, tuple, set)):
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, str) and item and item not in result:
            result.append(item)
    return result


def _clean_findings(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, (list, tuple)):
        return []
    findings = []
    for item in list(raw)[:MAX_FINDINGS]:
        if isinstance(item, Mapping):
            findings.append(
                {
                    "rule": str(item.get("rule") or ""),
                    "severity": str(item.get("severity") or ""),
                }
            )
        elif isinstance(item, str):
            findings.append({"rule": _one_line(item, 120), "severity": ""})
    return findings


def _critic_of(
    critiques: Mapping[str, Mapping[str, Any]], option_id: str
) -> Dict[str, Any]:
    """린트/비평 결과에서 기록에 남길 만한 값만 추린다."""
    critique = critiques.get(option_id)
    if not critique:
        return {}
    critic: Dict[str, Any] = {
        "verdict": critique.get("verdict"),
        "plan_score": critique.get("plan_score"),
        "findings": _clean_findings(critique.get("findings")),
    }
    if critique.get("score") is not None:
        critic["score"] = critique.get("score")
    return critic


def _artifact_map(state_values: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """`entry.items`를 id → 항목으로. profile마다 키가 달라도 id만 본다."""
    entry = state_values.get("entry")
    items = entry.get("items") if isinstance(entry, Mapping) else None
    if not isinstance(items, (list, tuple)):
        return {}
    return {
        str(item["id"]): item
        for item in items
        if isinstance(item, Mapping) and item.get("id")
    }


def _feature_source(
    option: Mapping[str, Any], artifact: Optional[Mapping[str, Any]]
) -> Optional[Path]:
    """특징을 뽑을 파일. 픽셀 특징이 더 정확하므로 PNG를 먼저 본다."""
    candidates: List[Any] = []
    if artifact:
        candidates.extend([artifact.get("png"), artifact.get("svg")])
    candidates.extend([option.get("png"), option.get("svg")])
    for raw in candidates:
        if not raw:
            continue
        path = Path(str(raw))
        if path.is_file():
            return path
    return None


def _artifacts_for(
    option: Mapping[str, Any],
    artifact: Optional[Mapping[str, Any]],
    feature_path: Optional[Path],
) -> Optional[str]:
    if artifact and isinstance(artifact.get("sha256"), str):
        return artifact["sha256"]
    for raw in (
        (artifact or {}).get("png"),
        (artifact or {}).get("svg"),
        option.get("png"),
        option.get("svg"),
    ):
        if raw and Path(str(raw)).is_file():
            digest = _sha256_file(Path(str(raw)))
            if digest:
                return digest
    if feature_path is not None:
        return _sha256_file(feature_path)
    return None


def build_record(
    *,
    thread_id: str,
    state: Any,
    values: Optional[Mapping[str, Any]] = None,
    graph: Optional[str] = None,
    reason_tags: Sequence[str] = (),
    reason_text: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """스레드 상태 → 결정 기록 한 건(사전). 파일은 건드리지 않는다."""
    state_values = _state_values(state)
    merged: Dict[str, Any] = {**state_values, **(values or {})}
    spec = merged.get("spec") if isinstance(merged.get("spec"), Mapping) else {}
    brief = str(merged.get("brief") or "")

    critiques: Dict[str, Mapping[str, Any]] = {}
    for critique in merged.get("critiques") or []:
        if isinstance(critique, Mapping) and critique.get("id"):
            critiques[str(critique["id"])] = critique
    artifacts = _artifact_map(merged)

    options: List[Dict[str, Any]] = []
    hashes: Dict[str, str] = {}
    for raw in merged.get("options") or []:
        if not isinstance(raw, Mapping) or not raw.get("id"):
            continue
        option_id = str(raw["id"])
        artifact = artifacts.get(option_id)
        feature_path = _feature_source(raw, artifact)
        option_features = (
            taste_features.safe_extract_features(feature_path)
            if feature_path is not None
            else {}
        )
        options.append(
            {
                "id": option_id,
                "direction": raw.get("direction"),
                "style": raw.get("style"),
                "theme": raw.get("theme"),
                "features": option_features,
                "critic": _critic_of(critiques, option_id),
            }
        )
        digest = _artifacts_for(raw, artifact, feature_path)
        if digest:
            hashes[option_id] = digest

    option_ids = [option["id"] for option in options]
    selected = [
        item for item in _string_list(merged.get("selection")) if item in option_ids
    ]
    rejected = [item for item in option_ids if item not in selected] if selected else []

    # 비평 점수는 0이 아닌 값이 하나라도 있을 때만 순위로 인정한다.
    scored = [
        (option["id"], option["critic"].get("plan_score"))
        for option in options
        if isinstance(option.get("critic"), Mapping)
        and isinstance(option["critic"].get("plan_score"), (int, float))
        and not isinstance(option["critic"].get("plan_score"), bool)
    ]
    positive = [(oid, float(score)) for oid, score in scored if float(score) > 0]
    critic_top = [
        oid for oid, _score in sorted(positive, key=lambda row: (-row[1], row[0]))
    ][:CRITIC_TOP_N]

    agreement: Optional[bool] = None
    if critic_top and selected:
        agreement = selected[0] == critic_top[0]

    # profile을 명시하지 않은 web 실행은 spec에 키가 없다 — 기본값은 web이다.
    profile_id = spec.get("profile") or spec.get("contest_type") or "web"

    return {
        "ts": now or _now(),
        "run_id": merged.get("run_id") or "",
        "thread_id": str(thread_id),
        "profile": str(profile_id),
        "platform": spec.get("platform"),
        "brief_sha256": _sha256_text(brief),
        "brief_head": _one_line(brief),
        "options": options,
        "selected": selected,
        "rejected": rejected,
        "reason_tags": _string_list(list(reason_tags)),
        "reason_text": _one_line(reason_text or "", limit=2000),
        "critic_top": critic_top,
        "agreement_top1": agreement,
        "artifacts": hashes,
    }


def append_decision(record: Mapping[str, Any], path: Optional[Path] = None) -> Path:
    """한 줄을 원자적으로 덧붙인다. 기존 줄은 절대 다시 쓰지 않는다.

    `O_APPEND`로 연 파일에 한 번에 쓴 뒤 `fsync`한다. 여러 프로세스가 동시에
    호출해도 각 줄은 섞이지 않는다(줄 단위 append).
    """
    target = Path(path) if path is not None else decisions_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    payload = line.encode("utf-8")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return target


def read_decisions(
    limit: Optional[int] = None, path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """결정 기록을 오래된 것부터 읽는다. `limit`은 **최근 N건**만 남긴다.

    깨진 줄은 건너뛴다(수기 편집·중간 중단이 있어도 나머지는 살린다).
    """
    target = Path(path) if path is not None else decisions_path()
    if not target.is_file():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
    except OSError:
        return []
    if limit is not None and limit > 0:
        return records[-limit:]
    return records


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------


def style_stats(records: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """스타일별 (선택 수 / 등장 수). 선택이 없는 기록은 등장만 센다."""
    appearances: Counter = Counter()
    wins: Counter = Counter()
    for record in records:
        options = record.get("options") or []
        selected = set(_string_list(record.get("selected")))
        if not selected:
            continue
        for option in options:
            if not isinstance(option, Mapping):
                continue
            style = option.get("style")
            if not style:
                continue
            appearances[style] += 1
            if option.get("id") in selected:
                wins[style] += 1
    stats: Dict[str, Dict[str, Any]] = {}
    for style in sorted(appearances):
        count = appearances[style]
        stats[style] = {
            "wins": wins[style],
            "appearances": count,
            "rate": round(wins[style] / count, 4) if count else None,
        }
    return stats


def tag_counts(records: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """사람이 남긴 이유 태그의 빈도(선택 기준)."""
    counter: Counter = Counter()
    for record in records:
        for tag in _string_list(record.get("reason_tags")):
            counter[tag] += 1
    return dict(sorted(counter.items(), key=lambda row: (-row[1], row[0])))


def summarize(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """결정 기록 → 사람이 읽을 요약(사전). 통계를 꾸며내지 않는다."""
    records = list(records)
    total = len(records)
    styles = style_stats(records)
    tags = tag_counts(records)

    agreed = 0
    compared = 0
    for record in records:
        value = record.get("agreement_top1")
        if isinstance(value, bool):
            compared += 1
            if value:
                agreed += 1

    recent = records[-3:]
    recent_tags = tag_counts(recent)

    return {
        "decisions": total,
        "confidence": ("early" if total < taste_profile.EARLY_THRESHOLD else "stable"),
        "early_threshold": taste_profile.EARLY_THRESHOLD,
        "last_ts": records[-1].get("ts") if records else None,
        "tag_ranking": [{"tag": tag, "count": count} for tag, count in tags.items()],
        "recent_tags": [
            {"tag": tag, "count": count} for tag, count in recent_tags.items()
        ],
        "styles": styles,
        "agreement": {
            "agree": agreed,
            "compared": compared,
            "rate": round(agreed / compared, 4) if compared else None,
        },
    }


def format_decisions(records: Sequence[Mapping[str, Any]]) -> str:
    """최근 결정을 사람 말로. 비어 있으면 한 줄 안내."""
    if not records:
        return "취향 기록 없음 — 다음 승인부터 쌓입니다."
    lines = []
    for index, record in enumerate(records, start=1):
        tags = ", ".join(_string_list(record.get("reason_tags"))) or "없음"
        reason = _one_line(record.get("reason_text") or "", limit=200) or "남기지 않음"
        selected = ", ".join(_string_list(record.get("selected"))) or "없음"
        rejected = ", ".join(_string_list(record.get("rejected"))) or "없음"
        lines.append(
            "{}. {} [{}/{}] 선택 {}개".format(
                index,
                record.get("ts") or "-",
                record.get("profile") or "-",
                record.get("platform") or "-",
                len(_string_list(record.get("selected"))),
            )
        )
        lines.append(f"   이유: {reason} (태그: {tags})")
        lines.append(f"   고름: {selected}")
        lines.append(f"   제외: {rejected}")
    return "\n".join(lines)


def format_summary(summary: Mapping[str, Any]) -> str:
    """요약을 디자이너가 아닌 사람이 읽는 문장으로."""
    total = int(summary.get("decisions") or 0)
    if not total:
        return "취향 기록이 아직 없습니다. 첫 승인을 남기면 여기서부터 쌓입니다."

    lines = [f"취향 기록 {total}건"]
    if summary.get("last_ts"):
        lines[0] += f" (마지막 {summary['last_ts']})"

    ranking = summary.get("tag_ranking") or []
    if ranking:
        lines.append(
            "자주 고른 태그: "
            + ", ".join(f"{row['tag']} {row['count']}회" for row in ranking)
        )
    else:
        lines.append("자주 고른 태그: 아직 없음")

    styles = summary.get("styles") or {}
    if styles:
        rows = sorted(
            styles.items(), key=lambda row: (-(row[1].get("wins") or 0), row[0])
        )
        parts = []
        for style, stats in rows:
            rate = stats.get("rate")
            percent = f"{rate * 100:.0f}%" if isinstance(rate, (int, float)) else "-"
            parts.append(
                f"{style} {stats.get('wins', 0)}/{stats.get('appearances', 0)} ({percent})"
            )
        lines.append("스타일 승률: " + ", ".join(parts))
    else:
        lines.append("스타일 승률: 아직 없음")

    agreement = summary.get("agreement") or {}
    if agreement.get("compared"):
        rate = agreement.get("rate") or 0
        lines.append(
            "비평가 1순위와 내 선택 일치: {}/{} ({:.0f}%)".format(
                agreement.get("agree", 0),
                agreement.get("compared", 0),
                rate * 100,
            )
        )
    else:
        lines.append("비평가 1순위와 내 선택 일치: 비교할 기록이 아직 없음")

    if summary.get("confidence") == "early":
        threshold = summary.get("early_threshold") or taste_profile.EARLY_THRESHOLD
        lines.append(
            f"아직 {threshold}건 미만(초기 단계)입니다. 태그와 스타일 승률만 참고하세요."
        )
    return "\n".join(lines)
