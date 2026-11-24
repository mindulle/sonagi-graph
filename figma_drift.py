"""Figma Variables 별칭 감사 — 읽기 전용.

실서비스 파일의 시맨틱 변수는 대부분 **별칭**(`semantic → primitive`)이다. 그래서
값을 `#rrggbb`로만 비교하면 전부 `"alias"`로 읽혀 거짓 드리프트가 난다(2026-11-24
실서비스 감사에서 29건). 여기서는 별칭을 모드 이름을 따라 끝까지 해석한 뒤 값으로
비교한다.

두 입력을 같은 스냅샷 모양으로 정규화한다.

```
Figma REST  GET /v1/files/:key/variables/local ─┐
플러그인 figma_execute (figma_snapshot_script) ─┴─▶ snapshot(schema v1) ─▶ resolve ─▶ 대조
```

**fail-closed**: 토큰이 없거나 만료됐으면 "이상 없음"이 아니라 "확인 불가"다.
종료 코드는 0=일치, 1=드리프트, 2=확인 불가.

사용::

    python3 figma_drift.py --file-key AEoW19jmlUh3rFgzhhV1vH   # FIGMA_TOKEN 필요
    python3 figma_drift.py --snapshot test_data/figma/....json  # 오프라인
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SNAPSHOT_SCHEMA = "sonagi.figma.snapshot/1"
CHECK_SCHEMA = "sonagi.figma.drift_check/1"

REST_URL = "https://api.figma.com/v1/files/{file}/variables/local"
TOKEN_ENV = "FIGMA_TOKEN"
TOKEN_FILE = "~/.secrets/figma-pat"

#: 별칭을 따라가는 최대 깊이. 순환은 여기서 끊고 issue로 보고한다.
MAX_ALIAS_DEPTH = 16


class FigmaTokenMissing(RuntimeError):
    """토큰이 없어 확인할 수 없다. '이상 없음'으로 넘기지 않는다."""


class FigmaAuthError(RuntimeError):
    """토큰 만료·권한 부족(401/403)."""


class FigmaFetchError(RuntimeError):
    """그 밖의 수집 실패(네트워크·5xx·형식 오류)."""


# ---------------------------------------------------------------- 정규화


def _hex_from_rgba(color: Dict[str, Any]) -> str:
    channels = []
    for key in ("r", "g", "b"):
        value = float(color.get(key, 0.0))
        channels.append(max(0, min(255, int(round(value * 255)))))
    body = "#" + "".join(f"{value:02x}" for value in channels)
    alpha = float(color.get("a", 1.0))
    if alpha < 1.0:
        body += f"{max(0, min(255, int(round(alpha * 255)))):02x}"
    return body


def _normalize_mode_value(raw: Any) -> Any:
    if isinstance(raw, dict):
        if raw.get("type") == "VARIABLE_ALIAS":
            return {"alias": raw.get("id")}
        if "r" in raw and "g" in raw and "b" in raw:
            return _hex_from_rgba(raw)
    return raw


def snapshot_from_rest(
    payload: Dict[str, Any], file_key: Optional[str] = None
) -> Dict[str, Any]:
    """REST 응답(`meta.variableCollections`·`meta.variables`) → 스냅샷.

    id를 그대로 보존한다. 이름은 컬렉션 사이에서 중복될 수 있어 키로 쓸 수 없다.
    """
    meta = payload.get("meta") or payload
    collections_raw = meta.get("variableCollections") or {}
    variables_raw = meta.get("variables") or {}
    if not isinstance(collections_raw, dict) or not isinstance(variables_raw, dict):
        raise FigmaFetchError(
            "예상과 다른 응답 형식입니다(meta.variableCollections 누락)."
        )

    collections = []
    for collection_id, item in collections_raw.items():
        modes = item.get("modes") or []
        collections.append(
            {
                "id": collection_id,
                "name": item.get("name"),
                "modes": [
                    {"id": mode.get("modeId"), "name": mode.get("name")}
                    for mode in modes
                ],
            }
        )
    variables = []
    for variable_id, item in variables_raw.items():
        values = {
            mode_id: _normalize_mode_value(raw)
            for mode_id, raw in (item.get("valuesByMode") or {}).items()
        }
        variables.append(
            {
                "id": variable_id,
                "name": item.get("name"),
                "collectionId": item.get("variableCollectionId"),
                "type": item.get("resolvedType"),
                "values": values,
            }
        )
    return {
        "schema": SNAPSHOT_SCHEMA,
        "fileKey": file_key,
        "collections": collections,
        "variables": variables,
    }


def load_snapshot(path: Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SNAPSHOT_SCHEMA:
        raise FigmaFetchError(
            f"스냅샷 스키마가 {SNAPSHOT_SCHEMA}가 아닙니다: {data.get('schema')!r}"
        )
    return data


# ---------------------------------------------------------------- 별칭 해석


class _Index:
    def __init__(self, snapshot: Dict[str, Any]):
        self.collections: Dict[str, Dict[str, Any]] = {
            item["id"]: item for item in snapshot.get("collections") or []
        }
        self.variables: Dict[str, Dict[str, Any]] = {
            item["id"]: item for item in snapshot.get("variables") or []
        }

    def mode_name(self, collection_id: str, mode_id: str) -> Optional[str]:
        collection = self.collections.get(collection_id) or {}
        for mode in collection.get("modes") or []:
            if mode.get("id") == mode_id:
                return mode.get("name")
        return None

    def mode_id(self, collection_id: str, mode_name: str) -> Optional[str]:
        collection = self.collections.get(collection_id) or {}
        wanted = str(mode_name).lower()
        for mode in collection.get("modes") or []:
            if str(mode.get("name", "")).lower() == wanted:
                return mode.get("id")
        return None

    def pick_mode(self, collection_id: str, wanted: Optional[str]) -> Optional[str]:
        """같은 이름의 모드가 있으면 그것, 없으면 첫 모드(단일 모드 컬렉션)."""
        if wanted:
            hit = self.mode_id(collection_id, wanted)
            if hit:
                return hit
        collection = self.collections.get(collection_id) or {}
        modes = collection.get("modes") or []
        return modes[0].get("id") if modes else None


def resolve_value(
    snapshot: Dict[str, Any],
    variable_id: str,
    mode_id: str,
    _index: Optional[_Index] = None,
    _depth: int = 0,
    _seen: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    """별칭을 모드 이름 따라 해석한다.

    반환: `{"value": "#rrggbb", "chain": [...]}` 또는 `{"issue": "...", "chain": [...]}`
    issue: alias-missing · alias-cycle · alias-depth · value-unknown · mode-missing
    """
    index = _index or _Index(snapshot)
    seen = tuple(_seen or ())
    variable = index.variables.get(variable_id)
    if variable is None:
        return {"issue": "alias-missing", "chain": [variable_id]}
    name = variable.get("name") or variable_id
    if variable_id in seen:
        return {"issue": "alias-cycle", "chain": list(seen[seen.index(variable_id):]) + [name]}
    seen = seen + (variable_id,)
    if _depth > MAX_ALIAS_DEPTH:
        return {"issue": "alias-depth", "chain": [name]}
    value = (variable.get("values") or {}).get(mode_id)
    if value is None:
        return {"issue": "mode-missing", "chain": [name]}
    if isinstance(value, str):
        return {"value": value, "chain": [name]}
    if isinstance(value, dict) and value.get("alias"):
        target_id = value["alias"]
        target = index.variables.get(target_id)
        if target is None:
            return {"issue": "alias-missing", "chain": [name, target_id]}
        target_collection = target.get("collectionId") or ""
        target_mode = index.pick_mode(
            target_collection,
            index.mode_name(variable.get("collectionId") or "", mode_id),
        )
        if target_mode is None:
            return {
                "issue": "mode-missing",
                "chain": [name, target.get("name") or target_id],
            }
        resolved = resolve_value(snapshot, target_id, target_mode, index, _depth + 1, seen)
        resolved["chain"] = [name] + resolved.get("chain", [])
        return resolved
    return {"issue": "value-unknown", "chain": [name]}


# ---------------------------------------------------------------- 대조


def _norm_hex(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    body = value.strip().lower()
    if not body.startswith("#"):
        return body
    raw = body[1:]
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) == 8 and raw.endswith("ff"):
        raw = raw[:6]
    return "#" + raw


def check_snapshot(
    snapshot: Dict[str, Any],
    request: Dict[str, Any],
    collection_name: Optional[str] = None,
) -> Dict[str, Any]:
    """스냅샷을 쓰기 요청(계획)과 대조한다. 별칭은 끝까지 해석해서 비교한다.

    `request`는 `token_sync.figma_write_request()`의 모양이다:
    `{"collection": "...", "modes": ["Light","Dark"], "variables": [{"name","modes"}]}`

    계획의 컬렉션명(`Semantic Colors`)과 파일의 컬렉션명(`Colors`)이 다를 수 있다.
    정확히 일치하는 컬렉션이 없으면 **기대 변수 이름이 가장 많이 겹치는 컬렉션**을 쓰고
    `collectionFallback="name-overlap"`으로 밝힌다(80% 이상일 때만). 조용히 다른
    컬렉션을 비교하지 않는다.
    """
    index = _Index(snapshot)
    wanted_name = collection_name or request.get("collection")
    collections = snapshot.get("collections") or []
    expected_names = [item["name"] for item in request.get("variables") or []]
    collection = next((c for c in collections if c.get("name") == wanted_name), None)
    fallback = None
    if collection is None and expected_names:
        best, best_hits = None, 0
        for candidate in collections:
            names = {
                item.get("name")
                for item in snapshot.get("variables") or []
                if item.get("collectionId") == candidate.get("id")
            }
            hits = sum(1 for name in expected_names if name in names)
            if hits > best_hits:
                best, best_hits = candidate, hits
        if best is not None and best_hits >= max(1, int(len(expected_names) * 0.8)):
            collection = best
            fallback = "name-overlap"
    if collection is None:
        return {
            "schema": CHECK_SCHEMA,
            "ok": False,
            "reason": "collection_missing",
            "collection": wanted_name,
            "expected": len(expected_names),
            "matched": 0,
            "missing": expected_names,
            "extra": [],
            "modeMissing": list(request.get("modes") or []),
            "drift": [],
            "aliasIssues": [],
            "aliasResolved": 0,
        }

    collection_id = collection["id"]
    in_collection = [
        item
        for item in snapshot.get("variables") or []
        if item.get("collectionId") == collection_id
    ]
    by_name: Dict[str, Dict[str, Any]] = {}
    for item in in_collection:
        by_name.setdefault(item.get("name") or "", item)

    actual_modes = [mode.get("name") for mode in collection.get("modes") or []]
    mode_missing = [mode for mode in request.get("modes") or [] if mode not in actual_modes]

    missing: List[str] = []
    drift: List[Dict[str, Any]] = []
    alias_issues: List[Dict[str, Any]] = []
    alias_resolved = 0
    matched = 0
    for expected in request.get("variables") or []:
        name = expected["name"]
        variable = by_name.get(name)
        if variable is None:
            missing.append(name)
            continue
        clean = True
        for mode_name in request.get("modes") or []:
            wanted = _norm_hex((expected.get("modes") or {}).get(mode_name))
            mode_id = index.mode_id(collection_id, mode_name)
            if mode_id is None:
                clean = False
                drift.append({"name": name, "mode": mode_name, "expected": wanted, "actual": None})
                continue
            resolved = resolve_value(snapshot, variable["id"], mode_id, index)
            if "issue" in resolved:
                clean = False
                alias_issues.append(
                    {
                        "name": name,
                        "mode": mode_name,
                        "issue": resolved["issue"],
                        "chain": resolved.get("chain"),
                    }
                )
                continue
            actual = _norm_hex(resolved.get("value"))
            if len(resolved.get("chain") or []) > 1:
                alias_resolved += 1
            if actual != wanted:
                clean = False
                drift.append(
                    {
                        "name": name,
                        "mode": mode_name,
                        "expected": wanted,
                        "actual": actual,
                        "chain": resolved.get("chain"),
                    }
                )
        if clean:
            matched += 1

    extra = sorted(name for name in by_name if name not in set(expected_names))
    return {
        "schema": CHECK_SCHEMA,
        "ok": not (missing or extra or mode_missing or drift or alias_issues),
        "collection": collection.get("name"),
        "requestedCollection": wanted_name,
        "collectionFallback": fallback,
        "expected": len(expected_names),
        "matched": matched,
        "missing": missing,
        "extra": extra,
        "modeMissing": mode_missing,
        "drift": drift,
        "aliasIssues": alias_issues,
        "aliasResolved": alias_resolved,
    }


# ---------------------------------------------------------------- 수집


def read_token(explicit: Optional[str] = None) -> str:
    """토큰은 인자 → `FIGMA_TOKEN` → `~/.secrets/figma-pat` 순. 없으면 예외."""
    token = (explicit or os.environ.get(TOKEN_ENV) or "").strip()
    if not token:
        path = Path(os.path.expanduser(TOKEN_FILE))
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise FigmaTokenMissing(
            f"Figma 토큰이 없습니다. {TOKEN_ENV} 또는 {TOKEN_FILE}에 넣어 주세요. "
            "확인 불가를 '이상 없음'으로 처리하지 않습니다."
        )
    return token


def fetch_local_variables(
    file_key: str,
    token: Optional[str] = None,
    timeout: float = 15.0,
    opener: Any = None,
) -> Dict[str, Any]:
    """REST로 Variables를 읽는다. 인증 실패는 FigmaAuthError로 끊는다."""
    secret = read_token(token)
    request = urllib.request.Request(
        REST_URL.format(file=file_key), headers={"X-Figma-Token": secret}
    )
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            body = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise FigmaAuthError(
                f"Figma 인증 실패({exc.code}). 토큰이 만료됐거나 file_variables:read 권한이 없습니다."
            ) from exc
        raise FigmaFetchError(f"Figma 응답 오류({exc.code}).") from exc
    except Exception as exc:  # 네트워크·타임아웃
        raise FigmaFetchError(f"Figma 요청 실패: {type(exc).__name__}") from exc
    if status != 200:
        raise FigmaFetchError(f"Figma 응답 상태 {status}.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FigmaFetchError("Figma 응답이 JSON이 아닙니다.") from exc
    return snapshot_from_rest(payload, file_key=file_key)


# ---------------------------------------------------------------- CLI


def _summary(report: Dict[str, Any]) -> str:
    if report.get("reason") == "collection_missing":
        return f"컬렉션 없음: {report.get('collection')}"
    lines = [
        f"collection={report.get('collection')} · matched {report.get('matched')}/{report.get('expected')}"
        f" · aliasResolved {report.get('aliasResolved')}"
    ]
    for key in ("missing", "extra", "modeMissing"):
        if report.get(key):
            lines.append(f"{key}: {report[key]}")
    for item in report.get("drift") or []:
        chain = " → ".join(item.get("chain") or [])
        lines.append(
            f"drift {item['name']}[{item['mode']}] {item.get('expected')} ≠ {item.get('actual')}"
            + (f"  ({chain})" if chain else "")
        )
    for item in report.get("aliasIssues") or []:
        lines.append(
            f"alias {item['name']}[{item['mode']}] {item['issue']}  {' → '.join(item.get('chain') or [])}"
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """0=일치, 1=드리프트, 2=확인 불가(토큰·네트워크·형식)."""
    parser = argparse.ArgumentParser(description="Figma Variables 별칭 감사(읽기 전용)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file-key", help="Figma 파일 키(실시간 REST)")
    source.add_argument("--snapshot", help="저장된 스냅샷 JSON(오프라인)")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--json", action="store_true", help="리포트 JSON 출력")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import token_sync as ts

    analysis, _ = ts.analyze(None, None)
    request = ts.figma_write_request(ts.figma_plan(analysis))
    try:
        snapshot = (
            load_snapshot(Path(args.snapshot))
            if args.snapshot
            else fetch_local_variables(args.file_key, timeout=args.timeout)
        )
    except (FigmaTokenMissing, FigmaAuthError, FigmaFetchError) as exc:
        print(f"[확인 불가] {exc}", file=sys.stderr)
        return 2

    report = check_snapshot(snapshot, request)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(_summary(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - 수동 실행
    raise SystemExit(main())
