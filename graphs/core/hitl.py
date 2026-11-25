"""core/hitl — 승인 게이트의 표준 상태.

그래프마다 승인 플래그 이름과 재개 규칙이 달랐다(`approval_status`, `approved`,
`selected_option_index`). 게이트는 두 값만 고정한다.

- `approval_status`: `pending` → `approved` | `rejected`
- `selection`: 승인할 때 사람이 고른 id 목록

승인은 산출물 지문(`digest`)에 묶는다. 멈춘 사이에 산출물이 바뀌면 사람이
보지 않은 것에 도장이 찍히므로 `stale`로 되돌린다(`design_token_sync`의 규칙을
일반화한 것).

사용::

    from graphs.core import hitl

    # 게이트 노드
    return hitl.pending(gate="entry_pack.gate", options=options, digest=digest)

    # 재개 후
    result = hitl.check(state, digest=current_digest, allowed=ids)
    if result.approved():
        ids = hitl.select(state, allowed=ids, limit=9, require=False)

    builder.add_conditional_edges("hitl", lambda s: hitl.route_after_gate(s, digest=digest))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
STALE = "stale"
INVALID = "invalid"

STATUSES = (PENDING, APPROVED, REJECTED, STALE, INVALID)

#: LangGraph의 `END`와 같은 문자열. 핸들러에서 그대로 반환해도 된다.
END = "__end__"


class SelectionError(ValueError):
    """선택 id가 옵션에 없거나 비어 있을 때."""


@dataclass(frozen=True)
class GateResult:
    """게이트 판정. `status`가 `approved`일 때만 다음 단계로 간다."""

    status: str
    approval_status: str = PENDING
    selection: Tuple[str, ...] = ()
    reason: str = ""
    digest: Optional[str] = None

    def approved(self) -> bool:
        return self.status == APPROVED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "approval_status": self.approval_status,
            "selection": list(self.selection),
            "reason": self.reason,
            "digest": self.digest,
        }


def pending(
    *,
    gate: str = "hitl",
    options: Optional[Sequence[Mapping[str, Any]]] = None,
    digest: Optional[str] = None,
    message: str = "",
) -> Dict[str, Any]:
    """게이트 노드가 돌려줄 state 조각. 사람이 고를 때까지 여기서 멈춘다."""
    return {
        "approval_status": PENDING,
        "gate": gate,
        "options": [dict(option) for option in options or ()],
        "selection": [],
        "digest": digest,
        "message": message
        or f"승인 대기 — {gate}. update_state({{'approval_status': 'approved'}})로 재개한다.",
    }


def approve(
    selection: Optional[Iterable[str]] = None,
    *,
    gate: str = "hitl",
    digest: Optional[str] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """승인 state 조각. `selection`은 순서를 유지하고 중복을 지운다."""
    return {
        "approval_status": APPROVED,
        "gate": gate,
        "selection": _dedupe(selection),
        "digest": digest,
        "reason": reason,
    }


def reject(
    reason: str = "",
    *,
    gate: str = "hitl",
    digest: Optional[str] = None,
) -> Dict[str, Any]:
    """거절 state 조각. `reason`은 왜 멈췄는지 남기는 용도다."""
    return {
        "approval_status": REJECTED,
        "gate": gate,
        "selection": [],
        "digest": digest,
        "reason": reason or "거절됨",
    }


def _dedupe(values: Optional[Iterable[str]]) -> List[str]:
    return list(dict.fromkeys(str(value) for value in values or ()))


def is_approved(state: Mapping[str, Any]) -> bool:
    return state.get("approval_status") == APPROVED


def selected(state: Mapping[str, Any]) -> List[str]:
    """state에 기록된 선택 id(중복 제거, 순서 유지)."""
    return _dedupe(state.get("selection") or ())


def check(
    state: Mapping[str, Any],
    *,
    digest: Optional[str] = None,
    allowed: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    require_selection: bool = True,
) -> GateResult:
    """게이트 상태를 판정한다.

    `digest`를 주면 승인 시점의 지문과 비교해 `stale`을 잡는다. `allowed`를
    주면 선택 id가 실제 옵션인지 확인한다(오타·삭제된 후보 차단).
    `require_selection=False`면 빈 선택을 승인으로 본다(선택이 곧 승인은 아닌
    그래프용 — `select(require=False)`가 이 값을 넘긴다).
    """
    status = state.get("approval_status") or PENDING
    selection = tuple(selected(state))
    reason = str(state.get("reason") or "")
    recorded = state.get("digest")

    if status == REJECTED:
        return GateResult(REJECTED, REJECTED, (), reason or "거절됨", recorded)
    if status != APPROVED:
        return GateResult(PENDING, PENDING, selection, reason or "승인 대기", recorded)

    if digest is not None and recorded and str(recorded) != str(digest):
        return GateResult(
            STALE,
            PENDING,
            (),
            f"승인 뒤 산출물 지문이 바뀌었다: 승인 {str(recorded)[:12]} ≠ 현재 {str(digest)[:12]}",
            digest,
        )

    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise SelectionError(f"limit은 정수여야 한다: {limit}") from None
        if limit < 0:
            raise SelectionError(f"limit은 0 이상이어야 한다: {limit}")

    if allowed is not None:
        pool = set(str(item) for item in allowed)
        unknown = [item for item in selection if item not in pool]
        if unknown:
            return GateResult(
                INVALID,
                PENDING,
                (),
                f"옵션에 없는 선택 id: {', '.join(unknown)}",
                recorded,
            )
        if not selection and pool and require_selection:
            return GateResult(
                INVALID,
                PENDING,
                (),
                "승인됐지만 선택이 비어 있다",
                recorded,
            )

    if limit is not None:
        selection = selection[:limit]
    return GateResult(APPROVED, APPROVED, selection, reason, recorded)


def select(
    state: Mapping[str, Any],
    *,
    allowed: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    require: bool = True,
) -> List[str]:
    """검증된 선택 목록. 비었거나 모르는 id면 `SelectionError`.

    `require=False`면 빈 선택을 허용한다(선택이 곧 승인은 아닌 그래프용).
    """
    result = check(state, allowed=allowed, limit=limit, require_selection=require)
    if result.status == INVALID:
        raise SelectionError(result.reason)
    if not result.selection and require:
        raise SelectionError("선택이 비어 있다. selection을 먼저 채워라")
    return list(result.selection)


def route_after_gate(
    state: Mapping[str, Any],
    *,
    approved: str = "publish",
    otherwise: str = END,
    digest: Optional[str] = None,
    allowed: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> str:
    """조건부 엣지용 — 승인일 때만 `approved` 노드로 보낸다."""
    result = check(state, digest=digest, allowed=allowed, limit=limit)
    return approved if result.approved() else otherwise


def gate_manifest_entry(result: GateResult, *, gate: str = "hitl") -> Dict[str, Any]:
    """manifest `gates` 항목으로 남길 형태."""
    return {"gate": gate, **result.to_dict()}
