"""디자인 토큰 정본 → 소비처(CSS·Tailwind·Craft·Figma) 결정론적 트랜스파일.

정본은 `mindulle-design-system/packages/tokens`(`@mindulle/tokens`, 2026-11-23
확정)이고 `sonagi-assets/tokens`는 그 **투영**이다. 이 모듈은 정본을 읽어
검사하고, 소비처가 바로 먹을 수 있는 산출물을 만든다. LangGraph에 의존하지
않으므로 `graphs/design_token_sync_graph.py`와 테스트가 같은 함수를 부른다.

```
tokens/*.json ──▶ parse ──▶ audit ──▶ transpile ──▶ report
                    │         │           │
                 alias 해석   검사 4종    CSS·Tailwind·Craft 스와치
```

## 계층 판정은 파일로 한다

토큰 한 덩어리(`primitive`, `typography`, `semantic` …)만 보고는 계층을 알 수
없다. `typography`는 `primitives.json`(원시 스케일)과 `semantics.json`(h1~h6
역할)에 동시에 나오기 때문이다. 그래서 계층은 **출처 파일**로 정한다.

| 출처 | 계층 | 규칙 |
| --- | --- | --- |
| `primitives.json` | primitive | 원시값(hex·px) 허용 |
| `semantics.json` · `surfaces.json` · `components.json` | semantic | 원시 hex 금지, 참조는 primitive로 착지 |
| `space.json` · `breakpoints.json` | 구조값 | 참조 해석만 검사 |

## 검사 4종

| id | 잡는 것 | 심각도 |
| --- | --- | --- |
| `alias_resolution` | `{a.b.c}`가 primitive까지 재귀 해석되는가, 순환·오타(`{font.sans}`) | 착지 실패=warning, 순환·형식오류=error |
| `no_raw_hex_in_semantic` | semantic 계층의 원시 hex | error |
| `scales` | 타이포 6단계·space 8px 배수·radius 3단계 | 위반=error(그리드) / 불일치=warning(스케일) |
| `projection_drift` | `sonagi-assets/tokens/semantic.json`과 값 차이 | warning |

**정본은 고치지 않는다.** 발견은 리포트에만 남기고 사람이 MDS에서 고친다.
`{font.sans}` 11건, `state.error = var(--mds-…)` 2건은 실제로 정본에 있는
결함이다(2026-11-24 확인). 그래서 unresolved leaf는 산출물에서 **뺀다**.
없는 변수를 가리키는 CSS를 만들어 두면 소비처에서 조용히 깨진다.

## 결정론

같은 입력 → 같은 바이트. 생성 시각·UUID·dict 삽입 순서에 의존하지 않는다
(모든 키 정렬, 타임스탬프 없음). 그래서 `artifact_set_hash`가 재현 검증의
기준이 된다.

사용::

    import token_sync

    result = token_sync.compile_all()       # 기본 경로(MDS 정본 + sonagi-assets 투영)
    result["report"]["summary"]             # {"leaves": 274, "errors": 0, ...}
    token_sync.figma_plan(result["analysis"])["variables"]
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------- 상수 / 경로

DEFAULT_TOKENS_DIR = "/home/ubuntu/mindulle-design-system/packages/tokens"
DEFAULT_ASSETS_DIR = "/home/ubuntu/sonagi-assets"
DEFAULT_OUT_DIR = "reports/design_tokens"

#: 정본 구성 파일. 읽기 전용이며 순서가 곧 병합 순서다(뒤가 앞을 덮지 않고 합쳐진다).
TOKEN_FILES: Tuple[str, ...] = (
    "primitives.json",
    "semantics.json",
    "space.json",
    "surfaces.json",
    "components.json",
    "breakpoints.json",
)

#: 파일 → 계층. 병합 뒤에는 경로만 남으므로 출처를 함께 들고 다닌다.
PRIMITIVE_FILES = {"primitives.json", "breakpoints.json"}
SEMANTIC_FILES = {"semantics.json", "surfaces.json", "components.json"}

#: 없으면 컴파일하지 않는 파일. 하나만 빠져도 "빈 색상표"가 정상 산출물로 나간다.
REQUIRED_FILES = {"primitives.json", "semantics.json"}

#: primitive 계층 루트. semantic 참조가 여기로 착지해야 한다.
PRIMITIVE_ROOTS = {
    "primitive",
    "typography",
    "spacing",
    "borderRadius",
    "borderWidth",
    "transition",
    "zIndex",
    "opacity",
    "breakpoints",
    "touch",
}

#: 스케일 계약. 값이 아니라 **단계 수**를 본다.
TYPE_SCALE_STEPS = 6  # h1~h6
RADIUS_STEPS = 3
GRID_UNIT_PX = 8
GRID_HALF_STEP_PX = 4

ALIAS = re.compile(r"\{([^{}]*)\}")
HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
CSS_VAR_LITERAL = re.compile(r"^var\(--[a-z0-9-]+\)$")
PX = re.compile(r"^(-?\d+(?:\.\d+)?)px$")

SEVERITY_ORDER = {"error": 0, "warning": 1}

REPORT_SCHEMA = "sonagi.token_sync.report/1"
SWATCH_SCHEMA = "sonagi.craft.swatches/1"
FIGMA_SCHEMA = "sonagi.figma.variables_plan/1"


class TokenSourceMissing(RuntimeError):
    """정본 토큰 디렉터리를 찾지 못했을 때. 조용히 빈손으로 넘어가지 않는다."""


# ---------------------------------------------------------------- 기본 타입


@dataclass(frozen=True)
class Finding:
    """검사 1건. 정렬 가능해야 리포트가 결정론적이다."""

    code: str
    severity: str  # "error" | "warning"
    path: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
            "detail": dict(sorted(self.detail.items())),
        }

    def sort_key(self) -> Tuple[int, str, str]:
        return (SEVERITY_ORDER.get(self.severity, 9), self.code, self.path)


@dataclass(frozen=True)
class Leaf:
    """`$value`를 가진 토큰 하나."""

    path: str
    value: Any
    type: Optional[str]
    source: str

    @property
    def tier(self) -> str:
        if self.source in PRIMITIVE_FILES:
            return "primitive"
        if self.source in SEMANTIC_FILES:
            return "semantic"
        return "structure"


@dataclass
class Analysis:
    """정본 한 벌을 읽고 해석한 결과."""

    doc: Dict[str, Any]
    leaves: Dict[str, Leaf]
    resolved: Dict[str, Any]
    sources: List[Dict[str, Any]]
    findings: List[Finding]

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def token_version(self) -> str:
        meta = self.doc.get("meta") or {}
        return str(meta.get("version") or "")

    def summary(self) -> Dict[str, Any]:
        return {
            "leaves": len(self.leaves),
            "resolved": len(self.resolved),
            "unresolved": len(self.leaves) - len(self.resolved),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
        }


class AliasError(Exception):
    """참조 해석 실패. code로 원인을 구분한다."""

    def __init__(self, code: str, target: str, chain: List[str], message: str = ""):
        self.code = code
        self.target = target
        self.chain = chain
        self.message = message or code
        super().__init__(f"{code}: {target}")


# ---------------------------------------------------------------- 읽기 / 병합


def tokens_dir() -> Path:
    """정본 디렉터리. `MDS_TOKENS_DIR`로 바꾼다."""
    return Path(os.environ.get("MDS_TOKENS_DIR", DEFAULT_TOKENS_DIR))


def assets_dir() -> Path:
    """투영 저장소. `SONAGI_ASSETS_DIR`로 바꾼다."""
    return Path(os.environ.get("SONAGI_ASSETS_DIR", DEFAULT_ASSETS_DIR))


def projection_path() -> Path:
    """`sonagi-assets` 투영 파일 경로."""
    return Path(
        os.environ.get(
            "SONAGI_TOKEN_PROJECTION", str(assets_dir() / "tokens/semantic.json")
        )
    )


def out_dir() -> Path:
    """산출물 디렉터리. 저장소 상대 경로면 저장소 루트 기준."""
    raw = os.environ.get("SONAGI_TOKEN_OUT", DEFAULT_OUT_DIR)
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_sources(
    root: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """정본 파일들을 읽는다. 없는 파일은 건너뛰고, 하나도 없으면 멈춘다."""
    root = Path(root or tokens_dir())
    docs: Dict[str, Any] = {}
    sources: List[Dict[str, Any]] = []
    for name in TOKEN_FILES:
        path = root / "tokens" / name
        if not path.is_file():
            path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        docs[name] = json.loads(text)
        sources.append(
            {
                "file": name,
                "sha256": sha256_text(text),
                "bytes": len(text.encode("utf-8")),
            }
        )
    if not docs:
        raise TokenSourceMissing(
            f"토큰 정본이 없습니다: {root}\n"
            "`MDS_TOKENS_DIR`에 mindulle-design-system/packages/tokens를 지정하세요."
        )
    return docs, sources


def deep_merge(
    into: Dict[str, Any],
    other: Dict[str, Any],
    conflicts: List[str],
    trail: Tuple[str, ...] = (),
) -> None:
    """`$value` 잎을 기준으로 문서를 합친다. 같은 경로에 다른 값이 있으면 기록한다."""
    for key, value in other.items():
        if key == "$type" and value is not None:
            into.setdefault(key, value)  # DTCG 그룹 $type은 자식이 상속한다
            continue
        if key.startswith("$") or key == "meta":
            continue
        here = trail + (key,)
        if isinstance(value, dict) and "$value" not in value and "value" not in value:
            target = into.setdefault(key, {})
            if not isinstance(target, dict):  # 잎과 그룹이 충돌
                conflicts.append(".".join(here))
                continue
            deep_merge(target, value, conflicts, here)
        else:
            if (
                key in into
                and isinstance(into[key], dict)
                and "$value" not in into[key]
            ):
                conflicts.append(".".join(here))
                continue
            if key in into:
                conflicts.append(".".join(here))
                continue
            into[key] = value


def merge_documents(docs: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """파일 묶음 → 단일 DTCG 문서. `meta`는 첫 파일 것을 남긴다."""
    merged: Dict[str, Any] = {}
    conflicts: List[str] = []
    for name in TOKEN_FILES:
        if name not in docs:
            continue
        doc = docs[name]
        if "meta" not in merged and isinstance(doc.get("meta"), dict):
            merged["meta"] = dict(doc["meta"])
    for name in TOKEN_FILES:
        if name in docs:
            deep_merge(merged, docs[name], conflicts)
    return merged, sorted(conflicts)


def flatten(doc: Dict[str, Any], source_map: Dict[str, str]) -> Dict[str, Leaf]:
    """경로 → Leaf. 그룹 노드는 결과에 남지 않는다.

    DTCG의 그룹 `$type` 상속을 따른다(부모가 선언하면 자식이 물려받는다).
    """
    leaves: Dict[str, Leaf] = {}
    source_of = dict(source_map)

    def walk(
        node: Any, trail: Tuple[str, ...], source: str, inherited: Optional[str]
    ) -> None:
        if not isinstance(node, dict):
            return
        node_type = node.get("$type") or node.get("type") or inherited
        if "$value" in node or "value" in node:
            value = node["$value"] if "$value" in node else node["value"]
            leaves[".".join(trail)] = Leaf(
                path=".".join(trail),
                value=value,
                type=node_type,
                source=source_of.get(".".join(trail), source),
            )
            return
        for key, value in node.items():
            if key.startswith("$") or key == "meta":
                continue
            child = ".".join(trail + (key,))
            walk(value, trail + (key,), source_of.get(child, source), node_type)

    for key, value in doc.items():
        if key == "meta" or key.startswith("$"):
            continue
        walk(value, (key,), "primitives.json", None)
    return leaves


def index_sources(docs: Dict[str, Any]) -> Dict[str, str]:
    """경로 → 출처 파일. flatten보다 먼저 돌려야 계층을 안다."""
    index: Dict[str, str] = {}

    def walk(node: Any, trail: Tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        if "$value" in node or "value" in node:
            index[".".join(trail)] = ""
            return
        for key, value in node.items():
            if key.startswith("$") or key == "meta":
                continue
            walk(value, trail + (key,))

    per_file: Dict[str, List[str]] = {}
    for name, doc in docs.items():
        found: List[str] = []
        for key, value in doc.items():
            if key == "meta" or key.startswith("$"):
                continue
            before = len(index)
            walk(value, (key,))
            found.extend(list(index)[before:])
        per_file[name] = found
    # 첫 등장 파일을 출처로 고정한다(primitive → semantic 순서).
    for name in TOKEN_FILES:
        for path in per_file.get(name, []):
            if path not in index or not index[path]:
                index[path] = name
    return index


# ---------------------------------------------------------------- 참조 해석


def _substitute(value: str, leaves: Dict[str, Leaf], chain: List[str]) -> Any:
    """문자열 안의 `{ref}`를 모두 치환한다. 전체가 참조면 값 자체를 돌려준다."""
    whole = ALIAS.fullmatch(value)
    if whole:
        return _follow(whole.group(1), leaves, chain)
    out = value
    for match in ALIAS.finditer(value):
        out = out.replace(match.group(0), str(_follow(match.group(1), leaves, chain)))
    return out


def _follow(target: str, leaves: Dict[str, Leaf], chain: List[str]) -> Any:
    target = target.strip()
    if not target or "." not in target:
        raise AliasError(
            "malformed_alias", target, chain, f"참조 형식이 아닙니다: {{{target}}}"
        )
    if target in chain:
        raise AliasError("alias_cycle", target, chain, " → ".join(chain + [target]))
    leaf = leaves.get(target)
    if leaf is None:
        raise AliasError("dangling_alias", target, chain)
    value = leaf.value
    if isinstance(value, str):
        return _substitute(value, leaves, chain + [target])
    if isinstance(value, dict):
        return {k: resolve_any(v, leaves, chain + [target]) for k, v in value.items()}
    return value


def resolve_any(
    value: Any, leaves: Dict[str, Leaf], chain: Optional[List[str]] = None
) -> Any:
    """리터럴·참조·합성값을 재귀 해석한다."""
    chain = list(chain or [])
    if isinstance(value, str):
        return _substitute(value, leaves, chain)
    if isinstance(value, dict):
        return {k: resolve_any(v, leaves, chain) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_any(v, leaves, chain) for v in value]
    return value


def references(value: Any) -> List[str]:
    """값 안의 참조 대상을 등장 순서대로 모은다(중복 제거)."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for match in ALIAS.finditer(node):
                target = match.group(1).strip()
                if target not in found:
                    found.append(target)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def alias_chain(path: str, leaves: Dict[str, Leaf], limit: int = 16) -> List[str]:
    """`path`가 거치는 참조 경로. 순환하면 잘라서 돌려준다."""
    chain: List[str] = [path]
    current = path
    for _ in range(limit):
        leaf = leaves.get(current)
        if leaf is None:
            break
        refs = references(leaf.value)
        if not refs:
            break
        current = refs[0]
        chain.append(current)
        if chain.count(current) > 1:
            break
    return chain


# ---------------------------------------------------------------- 검사


def _suggest(target: str, leaves: Dict[str, Leaf]) -> Optional[str]:
    """오타 추정: `font.sans` → `typography.fontFamily.sans`."""
    for prefix in ("typography.", "typography.fontFamily.", "primitive."):
        candidate = prefix + target.split(".")[-1]
        if candidate in leaves:
            return candidate
    tail = target.split(".")[-1]
    matches = sorted(
        (path for path in leaves if path.endswith("." + tail)), key=lambda p: (p.count("."), p)
    )
    return matches[0] if matches else None


def check_alias_resolution(
    leaves: Dict[str, Leaf],
) -> Tuple[Dict[str, Any], List[Finding]]:
    """모든 참조가 primitive까지 재귀 해석되는지. 실패는 값과 함께 리포트한다."""
    resolved: Dict[str, Any] = {}
    findings: List[Finding] = []
    for path in sorted(leaves):
        leaf = leaves[path]
        try:
            resolved[path] = resolve_any(leaf.value, leaves, [path])
        except AliasError as exc:
            if exc.code == "dangling_alias":
                suggestion = _suggest(exc.target, leaves)
                detail: Dict[str, Any] = {"target": exc.target, "chain": exc.chain}
                if suggestion:
                    detail["suggestion"] = suggestion
                findings.append(
                    Finding(
                        code="dangling_alias",
                        severity="warning",
                        path=path,
                        message=(
                            f"{{{exc.target}}}를 찾을 수 없습니다"
                            + (f" — {suggestion} 아닌가요?" if suggestion else "")
                        ),
                        detail=detail,
                    )
                )
            elif exc.code == "alias_cycle":
                findings.append(
                    Finding(
                        code="alias_cycle",
                        severity="error",
                        path=path,
                        message="참조가 순환합니다: " + exc.message,
                        detail={"target": exc.target, "chain": exc.chain},
                    )
                )
            else:
                findings.append(
                    Finding(
                        code="malformed_alias",
                        severity="error",
                        path=path,
                        message=exc.message,
                        detail={"target": exc.target},
                    )
                )
    return resolved, findings


def check_semantic_tier(
    leaves: Dict[str, Leaf], resolved: Dict[str, Any]
) -> List[Finding]:
    """semantic 계층 규칙: 원시 hex 금지, 착지는 primitive, DTCG 아닌 값은 경고."""
    findings: List[Finding] = []
    for path in sorted(leaves):
        leaf = leaves[path]
        if leaf.tier != "semantic":
            continue
        raw = leaf.value
        if isinstance(raw, str) and HEX.match(raw.strip()):
            findings.append(
                Finding(
                    code="raw_hex_in_semantic",
                    severity="error",
                    path=path,
                    message="semantic 계층에 원시 hex가 있습니다. primitive를 참조하세요.",
                    detail={"value": raw},
                )
            )
            continue
        if isinstance(raw, str) and CSS_VAR_LITERAL.match(raw.strip()):
            findings.append(
                Finding(
                    code="non_dtcg_value",
                    severity="warning",
                    path=path,
                    message=(
                        "DTCG 참조가 아니라 CSS var() 리터럴입니다. "
                        "`{primitive...}` 또는 `{semantic...}`로 바꿔야 해석됩니다."
                    ),
                    detail={"value": raw},
                )
            )
            continue
        if path not in resolved:
            continue
        refs = references(raw)
        if not refs:
            continue
        landing = alias_chain(path, leaves)
        final = landing[-1]
        root = final.split(".")[0]
        if root not in PRIMITIVE_ROOTS:
            findings.append(
                Finding(
                    code="semantic_chain",
                    severity="warning",
                    path=path,
                    message="semantic 참조가 primitive가 아닌 곳에 착지합니다.",
                    detail={"chain": landing},
                )
            )
    return findings


def check_css_var_targets(resolved: Dict[str, Any]) -> List[Finding]:
    """`var(--mds-…)` 리터럴이 실제로 생성되는 변수를 가리키는지."""
    produced = {css_var_name(path) for path in resolved}
    findings: List[Finding] = []
    for path in sorted(resolved):
        value = resolved[path]
        if not isinstance(value, str):
            continue
        for match in re.finditer(r"var\((--mds-[a-z0-9-]+)\)", value):
            name = match.group(1)
            if name not in produced:
                findings.append(
                    Finding(
                        code="css_var_target_missing",
                        severity="warning",
                        path=path,
                        message=f"산출물에 없는 변수를 가리킵니다: {name}",
                        detail={"target": name},
                    )
                )
    return findings


def check_scales(
    leaves: Dict[str, Leaf], resolved: Optional[Dict[str, Any]] = None
) -> Tuple[List[Finding], Dict[str, Any]]:
    """타이포 6단계 · space 8px 배수 · radius 3단계."""
    # 별칭은 raw 문자열이 px가 아니다. raw로 검사하면 계약이 조용히 무력화된다.
    values = {path: (resolved or {}).get(path, leaf.value) for path, leaf in leaves.items()}
    findings: List[Finding] = []
    detail: Dict[str, Any] = {}

    # ── 타이포: h1~h6 6단계, 위에서 아래로 줄어드는가
    headings = sorted(
        {
            path.split(".")[1]
            for path in leaves
            if path.startswith("typography.") and path.count(".") >= 2
        }
    )
    heading_paths = [h for h in headings if re.fullmatch(r"h\d+", h)]
    detail["type_scale"] = {
        "heading_steps": len(heading_paths),
        "expected": TYPE_SCALE_STEPS,
    }
    if not heading_paths:
        findings.append(
            Finding(
                code="type_scale_missing",
                severity="warning",
                path="typography",
                message=f"타이포 스케일이 없습니다. {TYPE_SCALE_STEPS}단계(h1~h6)를 기대합니다.",
                detail={"found": []},
            )
        )
    elif len(heading_paths) != TYPE_SCALE_STEPS:
        findings.append(
            Finding(
                code="type_scale_mismatch",
                severity="warning",
                path="typography",
                message=(
                    f"타이포 스케일이 {len(heading_paths)}단계입니다. "
                    f"{TYPE_SCALE_STEPS}단계를 기대합니다."
                ),
                detail={"found": heading_paths},
            )
        )
    else:
        sizes: List[Tuple[str, float]] = []
        for heading in heading_paths:
            parsed = parse_px(values.get(f"typography.{heading}.fontSize"))
            if parsed is not None:
                sizes.append((heading, parsed))
        if len(sizes) >= 2:
            ordered = [px for _, px in sizes]
            if ordered != sorted(ordered, reverse=True):
                findings.append(
                    Finding(
                        code="type_scale_order",
                        severity="warning",
                        path="typography",
                        message="타이포 스케일이 단조 감소하지 않습니다.",
                        detail={"sizes": {h: px for h, px in sizes}},
                    )
                )

    # ── space: 8px 배수. 4px 하프스텝은 허용하되 개수만 알린다
    violations: List[Dict[str, Any]] = []
    half_steps: List[str] = []
    for path in sorted(leaves):
        if not path.startswith("spacing."):
            continue
        value = values[path]
        px = parse_px(value)
        if px is None:
            continue
        if px % GRID_HALF_STEP_PX != 0:
            violations.append({"path": path, "value": value})
        elif px % GRID_UNIT_PX != 0:
            half_steps.append(path)
    detail["space_grid"] = {
        "unit_px": GRID_UNIT_PX,
        "half_step_px": GRID_HALF_STEP_PX,
        "half_step_tokens": half_steps,
        "violations": violations,
    }
    for item in violations:
        findings.append(
            Finding(
                code="space_grid_violation",
                severity="error",
                path=item["path"],
                message=(
                    f"{item['value']}는 {GRID_UNIT_PX}px 그리드({GRID_HALF_STEP_PX}px 하프스텝)의 "
                    "배수가 아닙니다."
                ),
                detail={"value": item["value"]},
            )
        )
    if half_steps:
        findings.append(
            Finding(
                code="space_half_step",
                severity="warning",
                path="spacing",
                message=(
                    f"{len(half_steps)}개 간격이 {GRID_UNIT_PX}px 배수가 아니라 "
                    f"{GRID_HALF_STEP_PX}px 하프스텝에 놓여 있습니다."
                ),
                detail={"tokens": half_steps},
            )
        )

    # ── radius: semantic 3단계. primitive 스케일은 정보로만 남긴다
    primitive_radius = sorted(
        path
        for path in leaves
        if path.startswith("borderRadius.") and path.count(".") == 1
    )
    semantic_radius = sorted(
        path
        for path, leaf in leaves.items()
        if leaf.tier == "semantic"
        and ("radius" in path.lower() or "borderradius" in path.lower())
        and not path.startswith("borderWidth")
    )
    detail["radius"] = {
        "expected": RADIUS_STEPS,
        "semantic_steps": semantic_radius,
        "primitive_steps": primitive_radius,
    }
    if not semantic_radius:
        findings.append(
            Finding(
                code="radius_layer_missing",
                severity="warning",
                path="borderRadius",
                message=(
                    f"radius {RADIUS_STEPS}단계 계약에 해당하는 semantic radius 레이어가 없습니다. "
                    f"primitive에 {len(primitive_radius)}단계만 있습니다."
                ),
                detail={"primitive_steps": primitive_radius},
            )
        )
    elif len(semantic_radius) != RADIUS_STEPS:
        findings.append(
            Finding(
                code="radius_steps_mismatch",
                severity="warning",
                path="borderRadius",
                message=(
                    f"semantic radius가 {len(semantic_radius)}단계입니다. "
                    f"{RADIUS_STEPS}단계를 기대합니다."
                ),
                detail={"found": semantic_radius},
            )
        )

    return findings, detail


def parse_px(value: Any) -> Optional[float]:
    """`"8px"` → 8.0. px가 아니면 None."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = PX.match(value.strip())
    return float(match.group(1)) if match else None


#: 투영(`sonagi-assets`) 역할 → semantic 모드·경로. 값이 아니라 **역할 대응**만 정의한다.
PROJECTION_MAP: Dict[str, Dict[str, str]] = {
    "editorial": {
        "background": "background.base",
        "surface": "background.surface",
        "surface-sunken": "background.hover",
        "ink": "text.primary",
        "text": "text.primary",
        "text-secondary": "text.secondary",
        "text-muted": "text.muted",
        "text-faint": "text.disabled",
        "accent": "brand.primary",
        "accent-strong": "brand.accent",
        "accent-ink": "text.inverse",
        "border": "border.default",
        "border-strong": "border.strong",
        "success": "state.success",
        "warning": "state.warning",
        "danger": "state.danger",
        "info": "state.info",
    },
    "architecture": {
        "background": "background.base",
        "surface": "background.surface",
        "surface-sunken": "background.hover",
        "ink": "text.primary",
        "text": "text.primary",
        "text-secondary": "text.secondary",
        "text-muted": "text.muted",
        "text-faint": "text.disabled",
        "accent": "brand.primary",
        "accent-strong": "brand.accent",
        "accent-ink": "text.inverse",
        "border": "border.default",
        "border-strong": "border.strong",
        "success": "state.success",
        "warning": "state.warning",
        "danger": "state.danger",
        "info": "state.info",
        "terminal": "primitive.brand.cyan",
    },
}

PROJECTION_MODE = {"editorial": "light", "architecture": "dark"}


def check_projection(
    analysis: Analysis, projection: Optional[Path] = None
) -> Tuple[List[Finding], Dict[str, Any]]:
    """`sonagi-assets/tokens/semantic.json`과 값이 어긋나는지. 고치지 않고 알린다."""
    path = Path(projection or projection_path())
    detail: Dict[str, Any] = {
        "path": str(path),
        "mapped": 0,
        "mismatches": 0,
        "unmapped": {},
    }
    if not path.is_file():
        return (
            [
                Finding(
                    code="projection_missing",
                    severity="warning",
                    path=str(path),
                    message="투영 파일이 없어 대조하지 못했습니다.",
                    detail={"path": str(path)},
                )
            ],
            detail,
        )

    projection_doc = json.loads(path.read_text(encoding="utf-8"))
    findings: List[Finding] = []
    for theme, roles in sorted(PROJECTION_MAP.items()):
        actual_theme = projection_doc.get(theme)
        if not isinstance(actual_theme, dict):
            findings.append(
                Finding(
                    code="projection_theme_missing",
                    severity="warning",
                    path=theme,
                    message=f"투영에 {theme} 테마가 없습니다.",
                    detail={"theme": theme},
                )
            )
            continue
        mode = PROJECTION_MODE[theme]
        for role, mapping in sorted(roles.items()):
            token = mapping if mapping.startswith("primitive.") else f"semantic.{mode}.color.{mapping}"
            expected = analysis.resolved.get(token)
            if not isinstance(expected, str):
                findings.append(
                    Finding(
                        code="projection_token_missing",
                        severity="warning",
                        path=f"{theme}.{role}",
                        message=(
                            f"{theme}.{role}의 정본 대응 {token}을 해석하지 못했습니다. "
                            "PROJECTION_MAP이 낡았거나 정본 참조가 깨졌습니다."
                        ),
                        detail={"token": token},
                    )
                )
                continue
            detail["mapped"] += 1
            if role not in actual_theme:
                findings.append(
                    Finding(
                        code="projection_role_missing",
                        severity="warning",
                        path=f"{theme}.{role}",
                        message=f"투영에 {theme}.{role} 역할이 없습니다.",
                        detail={
                            "expected": expected,
                            "token": f"semantic.{mode}.color.{mapping}",
                        },
                    )
                )
                continue
            got = str(actual_theme[role]).strip()
            if got.lower() != expected.strip().lower():
                detail["mismatches"] += 1
                findings.append(
                    Finding(
                        code="projection_drift",
                        severity="warning",
                        path=f"{theme}.{role}",
                        message=(
                            f"{theme}.{role} 값이 정본과 다릅니다 "
                            f"(투영 {got} ≠ 정본 {expected})."
                        ),
                        detail={
                            "projection": got,
                            "canonical": expected,
                            "token": f"semantic.{mode}.color.{mapping}",
                        },
                    )
                )
        detail["unmapped"][theme] = sorted(set(actual_theme) - set(roles))
        for role in detail["unmapped"][theme]:
            findings.append(
                Finding(
                    code="projection_role_unmapped",
                    severity="warning",
                    path=f"{theme}.{role}",
                    message=f"{theme}.{role}의 정본 대응을 PROJECTION_MAP에 정의하지 않았습니다.",
                    detail={"value": actual_theme[role]},
                )
            )
    return findings, detail


def analyze(
    root: Optional[Path] = None, projection: Optional[Path] = None
) -> Tuple[Analysis, Dict[str, Any]]:
    """정본 읽기 → 해석 → 검사 4종. `(Analysis, 상세)`를 돌려준다."""
    docs, sources = read_sources(root)
    merged, conflicts = merge_documents(docs)
    source_index = index_sources(docs)
    leaves = flatten(merged, source_index)

    findings: List[Finding] = []
    for path in conflicts:
        findings.append(
            Finding(
                code="duplicate_token",
                severity="warning",
                path=path,
                message="여러 파일이 같은 경로를 정의합니다. 먼저 읽은 파일을 남겼습니다.",
                detail={},
            )
        )
    for path in sorted(leaves):
        leaf = leaves[path]
        if leaf.type is None and leaf.tier == "semantic":
            findings.append(
                Finding(
                    code="missing_type",
                    severity="warning",
                    path=path,
                    message="`$type`이 없습니다.",
                    detail={
                        "value": leaf.value if isinstance(leaf.value, str) else "…"
                    },
                )
            )

    resolved, alias_findings = check_alias_resolution(leaves)
    findings.extend(alias_findings)
    findings.extend(check_semantic_tier(leaves, resolved))
    scale_findings, scale_detail = check_scales(leaves, resolved)
    findings.extend(scale_findings)
    findings.extend(check_css_var_targets(resolved))

    for name in sorted(REQUIRED_FILES):
        if name not in docs:
            findings.append(
                Finding(
                    code="missing_source_file",
                    severity="error",
                    path=name,
                    message=f"필수 정본 파일이 없습니다: {name}. 부분 컴파일은 하지 않는다.",
                    detail={"missing": name},
                )
            )

    analysis = Analysis(
        doc=merged,
        leaves=leaves,
        resolved=resolved,
        sources=sources,
        findings=sorted(findings, key=Finding.sort_key),
    )
    projection_findings, projection_detail = check_projection(analysis, projection)
    analysis.findings = sorted(
        analysis.findings + projection_findings, key=Finding.sort_key
    )
    return analysis, {"scales": scale_detail, "projection": projection_detail}


# ---------------------------------------------------------------- CSS 이름


def css_var_name(path: str) -> str:
    """토큰 경로 → `--mds-*` 변수명.

    MDS `scripts/build.js`의 규칙을 따르되, 모드를 지우면 충돌하는 곳만 다르게 둔다
    (`shadow.light.raised` → `--mds-shadow-light-raised`).
    """
    parts = path.split(".")
    if parts[0] == "semantic" and len(parts) > 2:
        parts = parts[2:]
    root = parts[0]
    if root == "typography" and len(parts) >= 3:
        group = {
            "fontFamily": "font",
            "fontSize": "font-size",
            "fontWeight": "font-weight",
            "lineHeight": "line-height",
            "letterSpacing": "letter-spacing",
        }.get(parts[1])
        if group:
            parts = [group, *parts[2:]]
    elif root == "borderRadius" and len(parts) >= 2:
        parts = ["radius", *parts[1:]]
    elif root == "borderWidth" and len(parts) >= 2:
        parts = ["border", *parts[1:]]
    elif root == "transition" and len(parts) >= 3:
        parts = [{"duration": "duration", "easing": "ease"}[parts[1]], *parts[2:]]
    elif root == "zIndex" and len(parts) >= 2:
        parts = ["z", *parts[1:]]
    elif root == "shadow" and len(parts) >= 3:
        parts = [parts[0], *parts[2:]]
    elif root == "breakpoints" and len(parts) >= 2:
        parts = ["breakpoint", *parts[1:]]
    return "--mds-" + "-".join(parts)


def _kebab(name: str) -> str:
    """`fontSize` → `font-size`. 선행 하이픈을 만들지 않는다(이중 `--` 방지)."""
    return re.sub(r"(?<!^)([A-Z])", lambda m: "-" + m.group(1).lower(), name)


def _css_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# ---------------------------------------------------------------- 트랜스파일


ARTIFACT_CSS = "mds-tokens.css"
ARTIFACT_TAILWIND = "tailwind.tokens.cjs"
ARTIFACT_SWATCH_JSON = "craft-swatches.json"
ARTIFACT_SWATCH_SVG = "craft-swatches.svg"
ARTIFACT_FIGMA_JS = "figma-variables.js"
ARTIFACT_REPORT = "token-report.json"


def _banner(analysis: Analysis, note: str) -> str:
    version = analysis.token_version or "unknown"
    return (
        "/* @mindulle/tokens → " + note + "\n"
        " * generated by sonagi-graph token_sync.py — 직접 수정 금지, 토큰을 고치고 다시 생성한다.\n"
        f" * tokens v{version} · 결정론 산출물(생성 시각 없음)\n"
        " */\n"
    )


def transpile_css(analysis: Analysis) -> str:
    """① CSS Custom Properties. 정본 순서(primitive → semantic light/dark → 구조값)."""
    primitive_lines: List[str] = []
    light_lines: List[str] = []
    dark_lines: List[str] = []
    structure_lines: List[str] = []

    def emit(lines: List[str], name: str, value: Any) -> None:
        lines.append(f"  {name}: {_css_value(value)};")

    def alias_ref(leaf: Leaf) -> Optional[str]:
        """전체가 단일 참조면 `var(--mds-…)`로 잇는다.

        리터럴로 인라인하면 테마 전환이 죽는다(components.json의 "themes switch
        automatically" 계약). MDS `build.js`의 `resolveAliases`와 같은 판단이다.
        """
        if not isinstance(leaf.value, str):
            return None
        whole = ALIAS.fullmatch(leaf.value.strip())
        if not whole:
            return None
        target = whole.group(1).strip()
        if target not in analysis.leaves:
            return None
        return f"var({css_var_name(target)})"

    for path in sorted(analysis.leaves):
        leaf = analysis.leaves[path]
        if path not in analysis.resolved:
            continue  # 해석 실패는 산출물에서 뺀다(없는 변수를 만들지 않는다)
        value = analysis.resolved[path]
        if isinstance(value, dict):  # 합성값(fontSize + lineHeight)은 하위 키로 편다
            for sub_key in sorted(value):
                emit(structure_lines, css_var_name(f"{path}.{_kebab(sub_key)}"), value[sub_key])
            continue
        name = css_var_name(path)
        ref = alias_ref(leaf)
        if ref is not None:
            value = ref
        if path.startswith("shadow.light."):
            emit(light_lines, name, value)
            continue
        if path.startswith("shadow.dark."):
            emit(dark_lines, name, value)
            continue
        if leaf.tier == "primitive":
            target = primitive_lines
        elif leaf.tier == "semantic":
            if path.startswith("semantic.dark."):
                target = dark_lines
            elif path.startswith("semantic.light.") or path.startswith(
                ("surface.", "component.")
            ):
                target = light_lines
            else:
                target = structure_lines
        else:
            target = structure_lines
        emit(target, name, value)

    blocks = [
        "/* --- Primitive Tokens --- */",
        ":root {",
        *primitive_lines,
        "}",
        "",
        "/* --- Semantic Tokens (light) --- */",
        ':root, [data-theme="light"], [data-theme="sonagi-core"] {',
        *light_lines,
        "}",
        "",
        "/* --- Semantic Tokens (dark) --- */",
        '[data-theme="dark"] {',
        *dark_lines,
        "}",
    ]
    if structure_lines:
        blocks.extend(
            [
                "",
                "/* --- Structure Tokens (typography·space·shadow…) --- */",
                ":root {",
                *structure_lines,
                "}",
            ]
        )
    return _banner(analysis, "CSS Custom Properties") + "\n" + "\n".join(blocks) + "\n"


def _nested(path: str, value: Any, into: Dict[str, Any]) -> None:
    """`brand.primary` → `{"brand": {"primary": value}}`."""
    parts = path.split(".")
    cur = into
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def transpile_tailwind(analysis: Analysis) -> str:
    """② Tailwind theme extension. 색·간격·타이포만. 값은 CSS 변수 참조."""
    colors: Dict[str, Any] = {}
    colors_dark: Dict[str, Any] = {}
    spacing: Dict[str, Any] = {}
    font_size: Dict[str, Any] = {}
    font_family: Dict[str, Any] = {}
    line_height: Dict[str, Any] = {}
    font_weight: Dict[str, Any] = {}
    radius: Dict[str, Any] = {}
    border_width: Dict[str, Any] = {}

    for path in sorted(analysis.leaves):
        leaf = analysis.leaves[path]
        if path not in analysis.resolved or isinstance(analysis.resolved[path], dict):
            continue
        name = css_var_name(path)
        ref = f"var({name})"
        if leaf.tier == "semantic" and path.startswith("semantic.light.color."):
            _nested(path[len("semantic.light.color.") :], ref, colors)
        elif leaf.tier == "semantic" and path.startswith("semantic.dark.color."):
            _nested(path[len("semantic.dark.color.") :], ref, colors_dark)
        elif path.startswith("spacing."):
            spacing[path.split(".", 1)[1]] = ref
        elif path.startswith("typography.fontSize."):
            font_size[path.split(".", 2)[2]] = ref
        elif path.startswith("typography.fontFamily."):
            font_family[path.split(".", 2)[2]] = ref
        elif path.startswith("typography.lineHeight."):
            line_height[path.split(".", 2)[2]] = ref
        elif path.startswith("typography.fontWeight."):
            font_weight[path.split(".", 2)[2]] = ref
        elif path.startswith("borderRadius.") and path.count(".") == 1:
            radius[path.split(".", 1)[1]] = ref
        elif path.startswith("borderWidth.") and path.count(".") == 1:
            border_width[path.split(".", 1)[1]] = ref

    # 타이포 역할(h1~h6)은 fontSize/lineHeight 쌍으로 넣는다
    for heading in sorted(
        k
        for k in {
            p.split(".")[1] for p in analysis.leaves if p.startswith("typography.h")
        }
        if re.fullmatch(r"h\d+", k)
    ):
        size = analysis.resolved.get(f"typography.{heading}.fontSize")
        line = analysis.resolved.get(f"typography.{heading}.lineHeight")
        if isinstance(size, str) and isinstance(line, str):
            font_size[heading] = [
                f"var({css_var_name(f'typography.{heading}.fontSize')})",
                f"var({css_var_name(f'typography.{heading}.lineHeight')})",
            ]

    theme = {
        "colors": colors,
        "colorsDark": colors_dark,
        "spacing": spacing,
        "fontSize": font_size,
        "fontFamily": font_family,
        "lineHeight": line_height,
        "fontWeight": font_weight,
        "borderRadius": radius,
        "borderWidth": border_width,
    }
    body = json.dumps(
        {k: v for k, v in sorted(theme.items()) if v},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return (
        _banner(analysis, "Tailwind theme extension")
        + "\n/** tailwind.config.js: const tokens = require('./tailwind.tokens.cjs'); ...tokens.theme.extend */\n"
        + "module.exports = {\n"
        + "  theme: {\n"
        + "    extend: "
        + body.replace("\n", "\n    ")
        + ",\n"
        + "  },\n"
        + "};\n"
    )


def _swatch_groups(analysis: Analysis) -> Dict[str, Any]:
    """스와치용 팔레트(테마별 역할)와 스케일."""
    themes: Dict[str, Any] = {"light": {}, "dark": {}}
    primitives: Dict[str, Any] = {}
    for path in sorted(analysis.leaves):
        leaf = analysis.leaves[path]
        if path not in analysis.resolved or isinstance(analysis.resolved[path], dict):
            continue
        value = analysis.resolved[path]
        if not isinstance(value, str) or not HEX.match(value.strip()):
            continue
        entry = {
            "hex": value.strip().lower(),
            "cssVar": css_var_name(path),
            "token": path,
        }
        if path.startswith("semantic.light.color."):
            _nested(path[len("semantic.light.color.") :], entry, themes["light"])
        elif path.startswith("semantic.dark.color."):
            _nested(path[len("semantic.dark.color.") :], entry, themes["dark"])
        elif path.startswith("primitive."):
            _nested(path[len("primitive.") :], entry, primitives)
    return {"themes": themes, "primitives": primitives}


def transpile_swatch_json(analysis: Analysis) -> str:
    """③-a Craft 5대 스위트 공용 스와치. 한 벌의 팔레트를 다섯 도구가 같이 읽는다."""
    groups = _swatch_groups(analysis)
    payload = {
        "schema": SWATCH_SCHEMA,
        "tokenVersion": analysis.token_version,
        "consumers": [
            {
                "name": "vectorcraft",
                "kind": "vector",
                "import": "swatch 파일을 팔레트 소스로 수동 등록",
            },
            {
                "name": "designcraft",
                "kind": "layout",
                "import": "swatch 파일을 팔레트 소스로 수동 등록",
            },
            {
                "name": "photocraft",
                "kind": "raster",
                "import": "swatch 파일을 팔레트 소스로 수동 등록",
            },
            {
                "name": "effectcraft",
                "kind": "motion",
                "import": "swatch 파일을 팔레트 소스로 수동 등록",
            },
            {
                "name": "filmcraft",
                "kind": "video",
                "import": "swatch 파일을 팔레트 소스로 수동 등록",
            },
        ],
        "themes": groups["themes"],
        "primitives": groups["primitives"],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _flat_swatches(analysis: Analysis, theme: str) -> List[Tuple[str, str]]:
    """테마 하나의 (역할 경로, hex) 목록. 정렬 고정."""
    out: List[Tuple[str, str]] = []
    prefix = f"semantic.{theme}.color."
    for path in sorted(analysis.leaves):
        if not path.startswith(prefix) or path not in analysis.resolved:
            continue
        value = analysis.resolved[path]
        if isinstance(value, str) and HEX.match(value.strip()):
            out.append((path[len(prefix) :], value.strip().lower()))
    return out


def transpile_swatch_svg(analysis: Analysis) -> str:
    """③-b 눈으로 보는 스와치 시트. 검수(HITL)에서 '시각 파괴'를 보는 용도."""
    themes = [("light", "Semantic / Light"), ("dark", "Semantic / Dark")]
    columns = 3
    cell_w, cell_h, gap = 232, 32, 8
    pad, header_h = 24, 34
    rows = max(
        (len(_flat_swatches(analysis, t)) + columns - 1) // columns for t, _ in themes
    )
    width = pad * 2 + columns * cell_w + (columns - 1) * gap
    height = pad * 2 + len(themes) * (header_h + rows * (cell_h + gap))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Mindulle Design System swatches">',
        f'  <rect width="{width}" height="{height}" fill="#fbfbfa"/>',
    ]
    y = pad
    for theme, label in themes:
        parts.append(
            f'  <text x="{pad}" y="{y + 16}" font-family="monospace" font-size="13" '
            f'fill="#18181b">{xml_escape(label)}</text>'
        )
        y += header_h
        for index, (role, hex_value) in enumerate(_flat_swatches(analysis, theme)):
            col, row = index % columns, index // columns
            x = pad + col * (cell_w + gap)
            cy = y + row * (cell_h + gap)
            dark_text = luminance(hex_value) > 0.55
            parts.append(f"  <g><title>{xml_escape(role)} {hex_value}</title>")
            parts.append(
                f'    <rect x="{x}" y="{cy}" width="{cell_w}" height="{cell_h}" rx="4" '
                f'fill="{hex_value}" stroke="#e4e4e7"/>'
            )
            parts.append(
                f'    <text x="{x + 8}" y="{cy + 20}" font-family="monospace" font-size="11" '
                f'fill="{"#18181b" if dark_text else "#fbfbfa"}">{xml_escape(role)} {hex_value}</text>'
            )
            parts.append("  </g>")
        y += rows * (cell_h + gap)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def luminance(hex_value: str) -> float:
    """WCAG 상대 휘도. 라벨 색을 고르는 데만 쓴다."""
    raw = hex_value.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def transpile(analysis: Analysis) -> Dict[str, str]:
    """산출물 3종(+SVG). 파일명 → 내용. 모두 결정론적이다."""
    return {
        ARTIFACT_CSS: transpile_css(analysis),
        ARTIFACT_TAILWIND: transpile_tailwind(analysis),
        ARTIFACT_SWATCH_JSON: transpile_swatch_json(analysis),
        ARTIFACT_SWATCH_SVG: transpile_swatch_svg(analysis),
    }


# ---------------------------------------------------------------- Figma 브리지


def hex_to_rgb01(hex_value: str) -> Dict[str, float]:
    """Figma Variables는 0~1 실수를 쓴다. 6자리 소수로 고정해 결정론을 지킨다."""
    raw = hex_value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    alpha = 1.0
    if len(raw) == 8:
        alpha = round(int(raw[6:8], 16) / 255, 6)
        raw = raw[:6]
    if len(raw) != 6:
        raise ValueError(f"hex가 아닙니다: {hex_value}")
    return {
        "r": round(int(raw[0:2], 16) / 255, 6),
        "g": round(int(raw[2:4], 16) / 255, 6),
        "b": round(int(raw[4:6], 16) / 255, 6),
        "a": alpha,
    }


def figma_collection_name() -> str:
    """Variables 컬렉션 이름. `FIGMA_COLLECTION`을 바꾸면 `figma-variables.js` 해시도 바뀐다."""
    return os.environ.get("FIGMA_COLLECTION", "Semantic Colors")


def figma_variable_name(path: str) -> str:
    """`semantic.light.color.brand.primary` → `brand/primary` (Figma 그룹 표기)."""
    body = path
    for prefix in ("semantic.light.color.", "semantic.dark.color."):
        if body.startswith(prefix):
            body = body[len(prefix) :]
            break
    return "/".join(body.split("."))


def figma_plan(analysis: Analysis) -> Dict[str, Any]:
    """Variables 배치 계획. 쓰지 않는다 — 무엇을 쓸지 계산만 한다."""
    variables: Dict[str, Any] = {}
    for mode in ("light", "dark"):
        prefix = f"semantic.{mode}.color."
        for path in sorted(analysis.leaves):
            if not path.startswith(prefix) or path not in analysis.resolved:
                continue
            value = analysis.resolved[path]
            if not isinstance(value, str) or not HEX.match(value.strip()):
                continue
            name = figma_variable_name(path)
            entry = variables.setdefault(
                name,
                {"name": name, "type": "COLOR", "modes": {}, "tokens": {}},
            )
            entry["modes"][mode] = hex_to_rgb01(value)
            entry["tokens"][mode] = path
    ordered = [variables[name] for name in sorted(variables)]
    incomplete = sorted(v["name"] for v in ordered if len(v["modes"]) != 2)
    return {
        "schema": FIGMA_SCHEMA,
        "collection": figma_collection_name(),
        "modes": ["light", "dark"],
        "modeLabels": {"light": "Light", "dark": "Dark"},
        "variables": ordered,
        "counts": {
            "variables": len(ordered),
            "incomplete": len(incomplete),
        },
        "incomplete": incomplete,
        "write": {
            "dryRun": True,
            "delivery": "figma-console-paste",
            "script": ARTIFACT_FIGMA_JS,
            "note": (
                "CDP 브리지(/json/version)가 확인되지 않아 API 직접 쓰기를 구현하지 않았다. "
                "생성된 스크립트를 Figma 콘솔에 붙여넣되 DRY_RUN을 false로 바꿔야 쓴다."
            ),
        },
    }


def bridge_ports() -> List[int]:
    raw = os.environ.get("FIGMA_CDP_PORTS", "9223,9224,9225")
    ports: List[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ports.append(int(chunk))
    return ports


def probe_bridge(
    ports: Optional[Iterable[int]] = None, timeout: float = 0.4
) -> Dict[str, Any]:
    """Figma CDP 브리지 확인. `/json/version`이 200 + JSON이어야 '확인됨'이다."""
    ports = list(ports if ports is not None else bridge_ports())
    probes: List[Dict[str, Any]] = []
    verified = False
    for port in ports:
        url = f"http://127.0.0.1:{port}/json/version"
        entry: Dict[str, Any] = {"port": port, "url": url}
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read(2048).decode("utf-8", "replace")
                entry["status"] = response.status
                entry["body"] = body[:200]
                if response.status == 200 and body.strip().startswith("{"):
                    verified = True
                    entry["ok"] = True
        except urllib.error.HTTPError as exc:
            entry["status"] = exc.code
            entry["ok"] = False
        except Exception as exc:  # 연결 거부·타임아웃
            entry["status"] = None
            entry["error"] = type(exc).__name__
            entry["ok"] = False
        probes.append(entry)
    return {
        "verified": verified,
        "probes": probes,
        "checkedAt": None,  # 결정론: 시각을 남기지 않는다
        "evidence": (
            "GET /json/version 200 + JSON"
            if verified
            else "GET /json/version 실패(브리지 미확인)"
        ),
    }


def transpile_figma_script(
    analysis: Analysis, plan: Optional[Dict[str, Any]] = None
) -> str:
    """콘솔에 붙여넣을 Variables 스크립트. 기본은 DRY_RUN — 쓰지 않는다.

    API 사용 형태는 MDS `figma-automation/scripts/sync-adr-tokens.js`를 따랐다.
    다만 그 스크립트는 hex 16개를 코드에 박아 두었고(ADR 0001 시절 값) 지금 정본과
    다르다. 여기서는 값을 박지 않고 정본에서 계산한 계획을 그대로 옮긴다.
    """
    plan = plan or figma_plan(analysis)
    payload = {
        "collection": plan["collection"],
        "modeLabels": plan["modeLabels"],
        "variables": [
            {
                "name": item["name"],
                "type": item["type"],
                "modes": item["modes"],
            }
            for item in plan["variables"]
        ],
    }
    body = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    return (
        _banner(analysis, "Figma Variables batch")
        + "// Figma 데스크톱 → 개발자 도구 Console에 전체 붙여넣기.\n"
        + "// DRY_RUN=true면 계획만 로그로 남기고 아무것도 만들지 않는다.\n"
        + "const DRY_RUN = true;\n"
        + f"const PLAN = {body};\n"
        + r"""
const rgb = (c) => ({ r: c.r, g: c.g, b: c.b, a: c.a ?? 1 });

async function syncVariables() {
  const collections = await figma.variables.getLocalVariableCollectionsAsync();
  const existing = collections.find((c) => c.name === PLAN.collection) || null;

  if (DRY_RUN) {
    const modes = existing ? existing.modes.map((m) => m.name) : [];
    console.log(
      `[dry-run] collection=${PLAN.collection} exists=${Boolean(existing)} modes=${modes.join(",") || "(없음)"}`
    );
    console.log(`[dry-run] variables=${PLAN.variables.length}`);
    for (const variable of PLAN.variables) {
      const modesSeen = Object.keys(variable.modes).join(",");
      console.log(`  ${variable.name} [${variable.type}] ${modesSeen}`);
    }
    return;
  }

  const collection = existing || figma.variables.createVariableCollection(PLAN.collection);
  const modeId = {};
  const claimed = new Set();
  for (const key of Object.keys(PLAN.modeLabels)) {
    const label = PLAN.modeLabels[key];
    const found = collection.modes.find((m) => m.name === label);
    if (found) {
      modeId[key] = found.modeId;
      claimed.add(found.modeId);
      continue;
    }
    // 새 컬렉션의 기본 모드("Mode 1")는 이름만 바꿔 쓴다. addMode로 남기면 빈 모드가 생긴다.
    const spare = collection.modes.find((m) => !claimed.has(m.modeId));
    if (spare) {
      collection.renameMode(spare.modeId, label);
      modeId[key] = spare.modeId;
      claimed.add(spare.modeId);
    } else {
      modeId[key] = collection.addMode(label);
      claimed.add(modeId[key]);
    }
  }

  const local = await figma.variables.getLocalVariablesAsync();
  let created = 0;
  let updated = 0;
  for (const variable of PLAN.variables) {
    let target = local.find(
      (v) => v.name === variable.name && v.variableCollectionId === collection.id
    );
    if (!target) {
      target = figma.variables.createVariable(variable.name, collection.id, variable.type);
      created += 1;
    } else {
      updated += 1;
    }
    for (const [key, color] of Object.entries(variable.modes)) {
      target.setValueForMode(modeId[key], rgb(color));
    }
  }
  console.log(`[write] created=${created} updated=${updated} collection=${collection.name}`);
}

syncVariables();
"""
    )


# ---------------------------------------------------------------- 리포트


def artifact_manifest(
    artifacts: Dict[str, str], paths: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    """파일명 → {path, sha256, bytes}. 이름 순 정렬."""
    paths = paths or {}
    return [
        {
            "name": name,
            "path": paths.get(name, name),
            "sha256": sha256_text(artifacts[name]),
            "bytes": len(artifacts[name].encode("utf-8")),
        }
        for name in sorted(artifacts)
    ]


def artifact_set_hash(manifest: List[Dict[str, Any]]) -> str:
    """산출물 묶음 지문. 같은 입력 2회 실행이면 같아야 한다."""
    canonical = "\n".join(f"{item['name']}:{item['sha256']}" for item in manifest)
    return sha256_text(canonical)


def build_report(
    analysis: Analysis,
    manifest: List[Dict[str, Any]],
    detail: Dict[str, Any],
    figma: Optional[Dict[str, Any]] = None,
    canonical_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """검사 리포트. 해시·경고·산출물 경로를 담고, 생성 시각은 담지 않는다."""
    checks = []
    for check_id, label in (
        ("alias_resolution", "참조가 primitive까지 해석되는가"),
        ("no_raw_hex_in_semantic", "semantic 계층에 원시 hex가 없는가"),
        ("scales", "타이포 6단계 · space 8px 배수 · radius 3단계"),
        ("projection_drift", "sonagi-assets 투영과 값이 맞는가"),
    ):
        related = [
            finding
            for finding in analysis.findings
            if finding.code
            in {
                "alias_resolution": {
                    "dangling_alias",
                    "alias_cycle",
                    "malformed_alias",
                    "semantic_chain",
                    "missing_source_file",
                    "duplicate_token",
                },
                "no_raw_hex_in_semantic": {
                    "raw_hex_in_semantic",
                    "non_dtcg_value",
                    "css_var_target_missing",
                },
                "scales": {
                    "type_scale_missing",
                    "type_scale_mismatch",
                    "type_scale_order",
                    "space_grid_violation",
                    "space_half_step",
                    "radius_layer_missing",
                    "radius_steps_mismatch",
                },
                "projection_drift": {
                    "projection_drift",
                    "projection_missing",
                    "projection_theme_missing",
                    "projection_role_missing",
                    "projection_token_missing",
                    "projection_role_unmapped",
                },
            }[check_id]
        ]
        errors = [f for f in related if f.severity == "error"]
        checks.append(
            {
                "id": check_id,
                "label": label,
                "status": "fail" if errors else ("warn" if related else "pass"),
                "errors": len(errors),
                "warnings": len(related) - len(errors),
            }
        )

    return {
        "schema": REPORT_SCHEMA,
        "tokenVersion": analysis.token_version,
        "sources": [dict(item) for item in analysis.sources],
        "summary": analysis.summary(),
        "checks": checks,
        "errors": [f.as_dict() for f in analysis.errors],
        "warnings": [f.as_dict() for f in analysis.warnings],
        "detail": detail,
        "artifacts": manifest,
        "artifactSetHash": artifact_set_hash(manifest),
        "figma": figma or {},
        "canonicalTokens": str(canonical_root or tokens_dir()),
    }


def write_artifacts(
    artifacts: Dict[str, str], directory: Optional[Path] = None
) -> Dict[str, str]:
    """산출물을 디스크에 쓴다. 파일명 → 절대 경로."""
    directory = Path(directory or out_dir())
    directory.mkdir(parents=True, exist_ok=True)
    paths: Dict[str, str] = {}
    for name in sorted(artifacts):
        path = directory / name
        path.write_text(artifacts[name], encoding="utf-8")
        paths[name] = str(path)
    return paths


def preview_artifacts(
    root: Optional[Path] = None, projection: Optional[Path] = None
) -> Dict[str, Any]:
    """디스크에 쓰지 않고 산출물 내용과 지문만 계산한다(승인 신선도 확인용)."""
    analysis, detail = analyze(root, projection)
    artifacts = transpile(analysis)
    plan = figma_plan(analysis)
    artifacts[ARTIFACT_FIGMA_JS] = transpile_figma_script(analysis, plan)
    manifest = artifact_manifest(artifacts)
    return {
        "analysis": analysis,
        "detail": detail,
        "artifacts": artifacts,
        "figma": plan,
        "manifest": manifest,
        "artifactSetHash": artifact_set_hash(manifest),
        "blocked": bool(analysis.errors),
    }


def compile_all(
    root: Optional[Path] = None,
    projection: Optional[Path] = None,
    directory: Optional[Path] = None,
    figma: Optional[Dict[str, Any]] = None,
    strict: bool = True,
) -> Dict[str, Any]:
    """읽기 → 검사 → 트랜스파일 → 리포트. 그래프와 CLI가 같이 쓰는 한 줄 진입점.

    `strict`(기본)면 치명 결함이 있을 때 **산출물을 쓰지 않고** 리포트만 남긴다.
    깨진 정본으로 반쪽 CSS를 만들어 두면 소비처가 조용히 물든다.
    """
    preview = preview_artifacts(root, projection)
    analysis, detail, plan = preview["analysis"], preview["detail"], preview["figma"]
    target_dir = Path(directory or out_dir())
    blocked = strict and bool(analysis.errors)

    artifacts: Dict[str, str] = {}
    paths: Dict[str, str] = {}
    if not blocked:
        artifacts = preview["artifacts"]
        paths = write_artifacts(artifacts, target_dir)

    manifest = artifact_manifest(artifacts, paths)
    report = build_report(analysis, manifest, detail, figma or {"plan": plan}, root)
    report["blocked"] = blocked
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = target_dir / ARTIFACT_REPORT
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    paths[ARTIFACT_REPORT] = str(report_path)
    return {
        "analysis": analysis,
        "detail": detail,
        "artifacts": artifacts,
        "paths": paths,
        "manifest": manifest,
        "report": report,
        "figma": plan,
        "blocked": blocked,
    }


if __name__ == "__main__":  # pragma: no cover - 수동 확인용
    started = time.time()
    result = compile_all()
    print(json.dumps(result["report"]["summary"], ensure_ascii=False))
    for item in result["report"]["artifacts"]:
        print(f"{item['name']:24s} {item['sha256'][:12]} {item['path']}")
    print(f"artifactSetHash={result['report']['artifactSetHash']}")
    print(f"{time.time() - started:.2f}s")
