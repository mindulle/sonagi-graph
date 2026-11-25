"""core/contract — 입력·출력 스키마와 `verify()` 계약.

그래프는 state(dict)를 주고받는다. 스키마가 없으면 키 오타 하나가 조용히
`None`이 되어 렌더 단계에서야 터진다. 여기서는 `TypedDict`를 그대로 읽어
필수 키·타입을 검사하고, 그래프별 `verify()` 판정을 하나의 `VerifyResult`로
모은다. 새 의존성은 없다(dataclasses·typing만).

계약 하나는 세 가지만 노출한다::

    class EntryPackContract:
        name = "entry_pack"
        inputs = EntryInput       # TypedDict
        outputs = EntryOutput     # TypedDict
        def verify(self, state):  # -> VerifyResult | bool | Issue 목록 | None
            ...

사용::

    from graphs.core.contract import verify_contract

    result = verify_contract(EntryPackContract(), state)
    if not result.ok:
        print(result.to_dict())

`verify()`가 아무것도 돌려주지 않으면 통과로 본다. False나 Issue를 돌려주면
실패다 — 판정을 삼키지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    """검증 한 건. `code`는 기계가, `message`는 사람이 읽는다."""

    level: str
    code: str
    message: str
    field: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }


@dataclass(frozen=True)
class VerifyResult:
    """검증 묶음. 오류가 하나도 없으면 `ok`."""

    issues: Tuple[Issue, ...] = ()
    checks: Tuple[str, ...] = field(default=())
    name: str = ""

    @property
    def ok(self) -> bool:
        return not any(issue.level == ERROR for issue in self.issues)

    @property
    def status(self) -> str:
        if any(issue.level == ERROR for issue in self.issues):
            return "fail"
        if self.issues:
            return "warn"
        return "pass"

    @property
    def errors(self) -> Tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.level == ERROR)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "ok": self.ok,
            "checks": list(self.checks),
            "issues": [issue.to_dict() for issue in self.issues],
        }

    @classmethod
    def merge(cls, *results: "VerifyResult", name: str = "") -> "VerifyResult":
        issues: List[Issue] = []
        checks: List[str] = []
        label = name
        for result in results:
            issues.extend(result.issues)
            checks.extend(result.checks)
            label = label or result.name
        deduped = tuple(dict.fromkeys(checks))
        return cls(issues=tuple(issues), checks=deduped, name=label)


def error(code: str, message: str, field: Optional[str] = None) -> Issue:
    return Issue(ERROR, code, message, field)


def warning(code: str, message: str, field: Optional[str] = None) -> Issue:
    return Issue(WARNING, code, message, field)


# ---------------------------------------------------------------------------
# 스키마 검사
# ---------------------------------------------------------------------------


def _annotations(schema: Any) -> Dict[str, Any]:
    try:
        return dict(get_type_hints(schema))
    except Exception:  # noqa: BLE001 — 문자열 주석은 원문으로도 충분하다
        return dict(getattr(schema, "__annotations__", {}) or {})


def _is_typed_dict(obj: Any) -> bool:
    return (
        isinstance(obj, type)
        and hasattr(obj, "__required_keys__")
        and hasattr(obj, "__annotations__")
    )


def _matches(annotation: Any, value: Any) -> bool:
    """타입 주석 하나와 값 하나를 비교한다. 모르는 주석은 통과시킨다."""
    if annotation is Any or annotation is None:
        return True
    if annotation is type(None):
        return value is None

    origin = get_origin(annotation)
    if origin is Union:
        return any(_matches(arg, value) for arg in get_args(annotation))
    if origin is not None:
        args = get_args(annotation)
        if str(origin).endswith("Literal"):
            return value in args
        if origin in (list, set, frozenset):
            return isinstance(value, origin)
        if origin is tuple:
            return isinstance(value, tuple)
        if origin is dict:
            return isinstance(value, dict)
        try:
            import collections.abc as cabc

            if origin in (cabc.Mapping, cabc.MutableMapping):
                return isinstance(value, cabc.Mapping)
            if origin in (cabc.Sequence,):
                return isinstance(value, cabc.Sequence) and not isinstance(
                    value, (str, bytes)
                )
        except Exception:  # noqa: BLE001
            pass
        return True

    if annotation is bool:
        return isinstance(value, bool)
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is str:
        return isinstance(value, str)
    if _is_typed_dict(annotation):
        if not isinstance(value, Mapping):
            return False
        return set(getattr(annotation, "__required_keys__", ())) <= set(value)
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return True


def check_schema(
    schema: Any,
    payload: Mapping[str, Any],
    *,
    name: Optional[str] = None,
    allow_unknown: bool = True,
) -> List[Issue]:
    """TypedDict 기준으로 필수 키·타입을 검사한다.

    `allow_unknown=True`면 스키마에 없는 키는 조용히 둔다(그래프 state는
    여러 노드가 키를 덧붙이므로 정상이다). 끄면 warning을 남긴다.
    """
    label = name or getattr(schema, "__name__", "schema")
    if not isinstance(payload, Mapping):
        return [error("not_mapping", f"{label}: 매핑(dict)이 아니다", label)]

    annotations = _annotations(schema)
    required = (
        set(getattr(schema, "__required_keys__", ()))
        if hasattr(schema, "__required_keys__")
        else set(annotations)
    )
    issues: List[Issue] = []

    for key in sorted(required):
        if key not in payload:
            issues.append(error("missing_key", f"{label}: 필수 키가 없다 — {key}", key))

    for key, value in payload.items():
        if key not in annotations:
            if not allow_unknown:
                issues.append(
                    warning("unknown_key", f"{label}: 스키마에 없는 키 — {key}", key)
                )
            continue
        expected = annotations[key]
        if not _matches(expected, value):
            issues.append(
                error(
                    "wrong_type",
                    f"{label}.{key}: {_type_name(expected)} 기대, {type(value).__name__} 받음",
                    key,
                )
            )
    return issues


def _type_name(annotation: Any) -> str:
    return getattr(annotation, "__name__", str(annotation))


# ---------------------------------------------------------------------------
# verify() 프로토콜
# ---------------------------------------------------------------------------


@runtime_checkable
class VerifyFn(Protocol):
    """그래프별 판정 함수. 반환은 `normalize_verdict()`가 흡수한다."""

    def __call__(self, state: Mapping[str, Any], /) -> Any: ...


@runtime_checkable
class Contract(Protocol):
    """그래프 하나가 노출하는 계약 — 이름·입출력 스키마·판정."""

    name: str
    inputs: Any
    outputs: Any

    def verify(self, state: Mapping[str, Any], /) -> Any: ...


@dataclass(frozen=True)
class SimpleContract:
    """클래스 없이 계약을 만들 때 쓰는 최소 구현."""

    name: str
    inputs: Any
    outputs: Any
    verify: VerifyFn

    def __call__(self, state: Mapping[str, Any]) -> Any:
        return self.verify(state)


def normalize_verdict(value: Any, *, name: str = "") -> VerifyResult:
    """`verify()`의 반환을 `VerifyResult` 하나로 정규화한다.

    허용: `VerifyResult` | `bool` | `Issue` | `None` | Issue/딕셔너리 목록 |
    `{"ok": bool, "issues": [...]}` 형태의 딕셔너리.
    """
    if value is None:
        return VerifyResult(name=name)
    if isinstance(value, VerifyResult):
        return value if value.name else VerifyResult(value.issues, value.checks, name)
    if isinstance(value, bool):
        issues = (
            () if value else (error("verify_failed", "verify()가 False를 돌려줬다"),)
        )
        return VerifyResult(issues=issues, checks=("verify",), name=name)
    if isinstance(value, Issue):
        return VerifyResult(issues=(value,), checks=("verify",), name=name)
    if isinstance(value, Mapping):
        if "issues" in value:
            issues = tuple(
                _as_issue(item, default_field=name)
                for item in value.get("issues") or ()
            )
            return VerifyResult(issues=issues, checks=("verify",), name=name)
        if "ok" in value:
            return normalize_verdict(bool(value["ok"]), name=name)
        return VerifyResult(name=name)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        items = tuple(_as_issue(item, default_field=name) for item in value)
        return VerifyResult(issues=items, checks=("verify",), name=name)
    return VerifyResult(name=name)


def _as_issue(item: Any, *, default_field: str = "") -> Issue:
    if isinstance(item, Issue):
        return item
    if isinstance(item, Mapping):
        return Issue(
            level=str(item.get("level") or ERROR),
            code=str(item.get("code") or "verify_failed"),
            message=str(item.get("message") or ""),
            field=item.get("field"),
        )
    if isinstance(item, str):
        return error("verify_failed", item, default_field or None)
    return error("verify_failed", repr(item), default_field or None)


def verify_inputs(contract: Contract, state: Mapping[str, Any]) -> VerifyResult:
    return VerifyResult(
        issues=tuple(check_schema(contract.inputs, state, name=contract.name)),
        checks=("inputs",),
        name=contract.name,
    )


def verify_outputs(contract: Contract, state: Mapping[str, Any]) -> VerifyResult:
    return VerifyResult(
        issues=tuple(check_schema(contract.outputs, state, name=contract.name)),
        checks=("outputs",),
        name=contract.name,
    )


def verify_contract(
    contract: Contract,
    state: Mapping[str, Any],
    *,
    stage: str = "output",
) -> VerifyResult:
    """입력 스키마 → `verify()` → 출력 스키마 순으로 검사한다.

    `stage`: `"input"` | `"output"` | `"both"` | `"none"`. 기본은 출력까지.
    """
    if stage not in ("input", "output", "both", "none"):
        raise ValueError(f"stage는 input/output/both/none 중 하나다: {stage}")

    results: List[VerifyResult] = []
    if stage in ("input", "both"):
        results.append(verify_inputs(contract, state))
    results.append(normalize_verdict(contract.verify(state), name=contract.name))
    if stage in ("output", "both"):
        results.append(verify_outputs(contract, state))
    return VerifyResult.merge(*results, name=contract.name)


class ContractError(AssertionError):
    """`ok=False`인 결과를 예외로 올릴 때."""

    def __init__(self, result: VerifyResult):
        self.result = result
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in result.errors)
        super().__init__(f"{result.name or 'contract'} 검증 실패 — {details}")


def raise_for_failure(result: VerifyResult) -> VerifyResult:
    """실패면 `ContractError`, 통과면 결과 그대로(체이닝용)."""
    if not result.ok:
        raise ContractError(result)
    return result


def load_schema(schema: Any) -> Dict[str, Any]:
    """TypedDict를 manifest·문서에 실을 수 있는 사전으로 편다."""
    annotations = _annotations(schema)
    required = (
        set(getattr(schema, "__required_keys__", ()))
        if hasattr(schema, "__required_keys__")
        else set(annotations)
    )
    return {
        key: {
            "type": _type_name(annotation),
            "required": key in required,
        }
        for key, annotation in annotations.items()
    }
