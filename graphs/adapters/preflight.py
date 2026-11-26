"""preflight — 렌더 전 자산 존재 확인과 누락 스킵 (GRAPH-24).

실카탈로그(14,938 items)의 `path`는 항상 실제 파일을 가리키지 않는다. 지금은
누락 자산 하나가 `render_recipe.py`의 `FileNotFoundError`로 이어져 엔트리 실행
전체를 죽인다. 이 모듈은 렌더 **전에** 레시피가 참조하는 자산을 모아 존재를
확인하고, 없는 참조만 결정론적으로 걷어낸 레시피와 리포트를 돌려준다.

```
recipe.json ─ collect_references ─▶ [AssetReference]
                                         │ catalog name → path
                         local: Path.is_file
                         remote: ssh 1회 (test -f 배치)
                                         │
                          filter_recipe ├─▶ 렌더 가능한 recipe
                                        └─▶ report {checked, skipped, renderable}
```

리포트 스키마(고정)::

    {
      "mode": "local" | "remote",
      "checked": 3,
      "skipped": [{"path": "/srv/.../x.png", "reason": "missing"}],
      "renderable": 2
    }

`skipped`의 `reason`은 `missing`(파일 없음) 또는 `not_in_catalog`(레시피가
카탈로그에 없는 이름을 참조)다. 순서는 레시피 등장 순서이며 같은 참조는
한 번만 센다 — 같은 입력이면 같은 리포트가 나온다.

원격 확인은 **ssh 한 번**으로 끝낸다. 파일마다 ssh를 띄우면 200개 확인에
200번 접속하게 되므로, 원격 셸에서 `for` 루프로 `test -f`를 돌리고
`ok`/`missing`을 탭으로 구분해 받는다. ssh가 실패하거나 출력을 해석할 수
없으면 "전부 누락"으로 조용히 넘기지 않고 `PreflightError`로 멈춘다.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)


class PreflightError(RuntimeError):
    """preflight를 수행할 수 없거나 렌더 가능한 자산이 하나도 없을 때."""


REASON_MISSING = "missing"
REASON_NOT_IN_CATALOG = "not_in_catalog"

#: `render_recipe.py`가 세트 우선순위로 이름을 고르는 순서와 같게 유지한다.
_SET_PRIORITY = {"lucide": 0, "tabler": 1, "phosphor": 2, "illlustrations": 3}

#: 이름 대신 경로를 직접 적은 참조를 구분하는 확장자.
_ASSET_EXTENSIONS = {
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".ai",
    ".eps",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}

#: 카탈로그 JSON은 12MB다. 같은 실행에서 변주마다 다시 읽지 않도록 캐시한다.
_CATALOG_CACHE: Dict[Tuple[str, int, int, int], Mapping[str, Any]] = {}


@dataclass(frozen=True)
class AssetReference:
    """레시피가 참조하는 자산 하나(카탈로그 이름 또는 직접 경로)."""

    name: str
    kind: str
    block_index: int
    field: str  # "illustration" | "ctaIcon" | "item.icon" | "gallery"


# ---------------------------------------------------------------------------
# 카탈로그 해석
# ---------------------------------------------------------------------------


def load_catalog(path: Path) -> Mapping[str, Any]:
    """카탈로그 JSON을 읽는다(mtime·크기가 같으면 캐시)."""
    resolved = Path(path).expanduser().resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise PreflightError(
            f"자산 카탈로그를 읽을 수 없습니다: {resolved} ({exc}) — "
            "scripts/sync_catalog.sh로 카탈로그를 동기화했는지 확인하세요"
        ) from exc
    key = (str(resolved), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _CATALOG_CACHE.get(key)
    if cached is None:
        try:
            cached = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PreflightError(
                f"자산 카탈로그를 해석할 수 없습니다: {resolved} ({exc})"
            ) from exc
        if not isinstance(cached, Mapping):
            raise PreflightError(f"자산 카탈로그 형식이 아닙니다: {resolved}")
        if len(_CATALOG_CACHE) >= 4:
            _CATALOG_CACHE.pop(next(iter(_CATALOG_CACHE)))
        _CATALOG_CACHE[key] = cached
    return cached


def _rank(item: Mapping[str, Any]) -> int:
    sets = [
        str(tag).split(":", 1)[1]
        for tag in item.get("tags") or []
        if str(tag).startswith("set:")
    ]
    return min((_SET_PRIORITY.get(name, 9) for name in sets), default=9)


def catalog_index(catalog: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    """이름 → 카탈로그 항목. `render_recipe.py`의 선택 순서를 그대로 따른다.

    먼저 아이콘·일러스트 항목을 세트 우선순위(이름 오름차순)로 채우고,
    그다음 나머지 항목을 채운다. 같은 이름은 먼저 들어온 항목이 이긴다.
    """
    items = [item for item in catalog.get("items") or [] if isinstance(item, Mapping)]
    ordered = sorted(items, key=lambda item: (_rank(item), str(item.get("name"))))
    index: Dict[str, Mapping[str, Any]] = {}
    for item in ordered:
        tags = {str(tag) for tag in item.get("tags") or []}
        if tags & {"kind:icon", "kind:illustration"}:
            index.setdefault(str(item.get("name")), item)
    for item in ordered:
        index.setdefault(str(item.get("name")), item)
    return index


def _looks_like_path(name: str) -> bool:
    return "/" in name or Path(name).suffix.lower() in _ASSET_EXTENSIONS


# ---------------------------------------------------------------------------
# 참조 수집·필터
# ---------------------------------------------------------------------------


def _as_blocks(recipe: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """정본 `blocks` 또는 레거시 `content` 블록 맵을 블록 목록으로 본다.

    `recipes/landing-hero-v2.json` 같은 레거시 레시피는 렌더러가 `content` 맵을
    `blocks`로 정규화해 읽는다(`scripts/recipe_schema.py`). preflight도 같은
    규칙을 따라야 누락 자산을 놓치지 않는다.
    """
    blocks = recipe.get("blocks")
    if isinstance(blocks, list) and blocks:
        return [block for block in blocks if isinstance(block, Mapping)]
    content = recipe.get("content")
    if isinstance(content, Mapping) and any(
        isinstance(value, Mapping) for value in content.values()
    ):
        return [
            {"type": kind, **dict(fields)}
            for kind, fields in content.items()
            if isinstance(fields, Mapping)
        ]
    return []


def collect_references(recipe: Mapping[str, Any]) -> List[AssetReference]:
    """레시피 블록에서 자산 참조를 등장 순서대로 모은다.

    렌더러가 이름으로 찾는 필드만 본다: `hero.illustration`, `gallery.items`,
    `*.ctaIcon`, `features.items[].icon`. 이름 자리에 경로를 적은 경우도
    참조로 인정한다.
    """
    references: List[AssetReference] = []

    def add(value: Any, kind: str, block_index: int, field: str) -> None:
        if isinstance(value, str) and value.strip():
            references.append(AssetReference(value.strip(), kind, block_index, field))

    for block_index, block in enumerate(_as_blocks(recipe)):
        illustration = block.get("illustration")
        if isinstance(illustration, str):
            add(illustration, "illustration", block_index, "illustration")
        elif isinstance(illustration, (list, tuple)):
            for name in illustration:
                add(name, "illustration", block_index, "illustration")
        add(block.get("ctaIcon"), "icon", block_index, "ctaIcon")
        items = block.get("items")
        if not isinstance(items, list):
            continue
        if block.get("type") == "gallery":
            for name in items:
                add(name, "illustration", block_index, "gallery")
        else:
            for item in items:
                if isinstance(item, Mapping):
                    add(item.get("icon"), "icon", block_index, "item.icon")
    return references


def filter_recipe(
    recipe: Mapping[str, Any],
    missing_names: Iterable[str],
    *,
    replacements: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """누락 참조를 걷어낸 레시피 사본을 돌려준다. 원본은 건드리지 않는다.

    - `illustration`(문자열/목록): 누락 이름을 빼고, 비면 키를 지운다.
    - `ctaIcon`: `replacements`에 대체 아이콘이 있으면 그것으로 바꾼다.
      히어로 렌더러는 `block["ctaIcon"]`을 무조건 읽으므로 키를 지우면 안 된다.
    - `gallery.items`: 누락 이름을 뺀다. 전부 빠지면 블록도 뺀다
      (빈 갤러리는 렌더러가 음수 높이를 계산한다).
    - `features.items[].icon`: 대체 아이콘이 있으면 바꾸고, 없으면 그 항목을 뺀다.

    레거시 `content` 블록 맵은 이때 정본 `blocks` 형태로 정규화한다 — 렌더러도
    같은 변환을 하므로 결과는 동일하다.
    """
    missing = {str(name).strip() for name in missing_names}
    fallbacks = {
        str(old).strip(): str(new).strip() for old, new in (replacements or {}).items()
    }
    filtered = copy.deepcopy(dict(recipe))
    if not missing and not fallbacks:
        return filtered

    if not (isinstance(filtered.get("blocks"), list) and filtered["blocks"]):
        legacy = _as_blocks(filtered)
        if legacy:
            filtered.pop("content", None)
            filtered["blocks"] = [dict(block) for block in legacy]

    kept_blocks: List[Any] = []
    for block in filtered.get("blocks") or []:
        if not isinstance(block, dict):
            kept_blocks.append(block)
            continue
        illustration = block.get("illustration")
        if isinstance(illustration, str):
            if illustration.strip() in missing:
                block.pop("illustration", None)
            else:
                block["illustration"] = illustration.strip()
        elif isinstance(illustration, (list, tuple)):
            kept = [
                str(name).strip()
                for name in illustration
                if not (isinstance(name, str) and str(name).strip() in missing)
            ]
            if kept:
                block["illustration"] = kept
            else:
                block.pop("illustration", None)
        icon = block.get("ctaIcon")
        if isinstance(icon, str):
            key = icon.strip()
            if key in fallbacks:
                block["ctaIcon"] = fallbacks[key]
            elif key in missing:
                # 대체 아이콘 없는 ctaIcon은 렌더러가 KeyError를 낸다.
                block.pop("ctaIcon", None)
        items = block.get("items")
        if not isinstance(items, list):
            kept_blocks.append(block)
            continue
        if block.get("type") == "gallery":
            block["items"] = [
                str(name).strip()
                for name in items
                if not (isinstance(name, str) and str(name).strip() in missing)
            ]
            if not block["items"]:
                continue
        else:
            kept_items = []
            for item in items:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("icon"), str)
                    and str(item["icon"]).strip() in missing
                ):
                    fallback = fallbacks.get(str(item["icon"]).strip())
                    if fallback is None:
                        continue
                    item["icon"] = fallback
                kept_items.append(item)
            block["items"] = kept_items
        kept_blocks.append(block)
    if isinstance(filtered.get("blocks"), list):
        filtered["blocks"] = kept_blocks
    return filtered


# ---------------------------------------------------------------------------
# 존재 확인
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Resolved:
    name: str
    path: str
    reason: str
    probe: bool
    fields: frozenset


def _resolve(
    references: Sequence[AssetReference],
    index: Mapping[str, Mapping[str, Any]],
    *,
    to_asset_path: Callable[[str], str],
) -> List[_Resolved]:
    resolved: List[_Resolved] = []
    positions: Dict[Tuple[str, str], int] = {}
    for reference in references:
        if reference.name in index:
            raw = index[reference.name].get("path")
            if isinstance(raw, str) and raw.strip():
                path = to_asset_path(raw.strip())
                reason, probe = REASON_MISSING, True
            else:
                path, reason, probe = (
                    reference.name,
                    REASON_NOT_IN_CATALOG,
                    False,
                )
        elif _looks_like_path(reference.name):
            path, reason, probe = to_asset_path(reference.name), REASON_MISSING, True
        else:
            # 카탈로그에 없는 이름은 원격에서 `test -f package`처럼 확인하지 않는다.
            path, reason, probe = reference.name, REASON_NOT_IN_CATALOG, False
        key = (reference.name, path)
        if key in positions:
            # 같은 자산을 여러 필드에서 참조한다. 한 번만 확인하되, 어떤
            # 필드(예: 필수 ctaIcon)에서 쓰였는지는 모두 기억한다.
            position = positions[key]
            current = resolved[position]
            resolved[position] = _Resolved(
                current.name,
                current.path,
                current.reason,
                current.probe,
                current.fields | {reference.field},
            )
            continue
        positions[key] = len(resolved)
        resolved.append(
            _Resolved(
                reference.name,
                path,
                reason,
                probe,
                frozenset({reference.field}),
            )
        )
    return resolved


def _build_report(
    mode: str,
    resolved: Sequence[_Resolved],
    found: Mapping[str, bool],
    *,
    replacements: int = 0,
) -> Dict[str, Any]:
    skipped: List[Dict[str, str]] = []
    skipped_names: List[str] = []
    for entry in resolved:
        if entry.probe and found.get(entry.path, False):
            continue
        skipped.append({"path": entry.path, "reason": entry.reason})
        skipped_names.append(entry.name)
    renderable = len(resolved) - len(skipped) + replacements
    return {
        "mode": mode,
        "checked": len(resolved),
        "skipped": skipped,
        "renderable": renderable,
        "_skipped_names": skipped_names,
    }


#: ctaIcon이 없을 때 먼저 시도할 아이콘 이름(있으면).
_PREFERRED_FALLBACK_ICONS = ("arrow-right", "arrow-up-right", "check", "plus", "circle")


def _fallback_icon_names(index: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """누락된 필수 아이콘을 대체할 후보(카탈로그에 있는 이름, 결정론 순서)."""
    names = [name for name in _PREFERRED_FALLBACK_ICONS if name in index]
    for name in sorted(index):
        item = index[name]
        tags = {str(tag) for tag in item.get("tags") or []}
        if "kind:icon" in tags and name not in names:
            names.append(name)
        if len(names) >= 6:
            break
    return names


def _icon_path(
    index: Mapping[str, Mapping[str, Any]],
    name: str,
    to_asset_path: Callable[[str], str],
) -> Optional[str]:
    raw = index[name].get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return to_asset_path(raw.strip())


def _pick_replacements(
    resolved: Sequence[_Resolved],
    found: Mapping[str, bool],
    index: Mapping[str, Mapping[str, Any]],
    *,
    to_asset_path: Callable[[str], str],
    is_present: Callable[[str], bool],
) -> Dict[str, str]:
    """누락된 `ctaIcon`을 실제로 존재하는 아이콘으로 바꾼다.

    히어로 렌더러는 `block["ctaIcon"]`을 무조건 읽으므로, 파일이 없다고 키를
    지우면 `KeyError`로 죽는다. 대체 아이콘도 없으면 렌더 불가로 멈춘다.
    """
    missing_icons = [
        entry
        for entry in resolved
        if "ctaIcon" in entry.fields and not found.get(entry.path, False)
    ]
    if not missing_icons:
        return {}
    candidates = _fallback_icon_names(index)
    replacements: Dict[str, str] = {}
    unresolved: List[_Resolved] = []
    for entry in missing_icons:
        for name in candidates:
            path = _icon_path(index, name, to_asset_path)
            if path is not None and (found.get(path, False) or is_present(path)):
                replacements[entry.name] = name
                break
        else:
            unresolved.append(entry)
    if unresolved:
        missing = ", ".join(entry.path for entry in unresolved)
        raise PreflightError(
            "ctaIcon을 대체할 아이콘이 없습니다 (필수 참조 누락: "
            f"{missing}). 아이콘 세트를 확인하세요."
        )
    return replacements


def _public(report: Mapping[str, Any]) -> Dict[str, Any]:
    """내부 필드(`_skipped_names`)를 뺀 공개 리포트."""
    return {key: value for key, value in report.items() if not key.startswith("_")}


def empty_report(mode: str) -> Dict[str, Any]:
    """확인할 참조가 없거나 확인을 건너뛴 실행의 리포트."""
    return {"mode": mode, "checked": 0, "skipped": [], "renderable": 0}


def ensure_renderable(report: Mapping[str, Any]) -> None:
    """확인한 참조가 전부 누락이면 전체 누락 목록과 함께 멈춘다."""
    if int(report.get("checked", 0)) > 0 and int(report.get("renderable", 0)) == 0:
        missing = ", ".join(str(entry["path"]) for entry in report.get("skipped") or [])
        raise PreflightError(
            f"렌더 가능한 자산이 없습니다 ({len(report.get('skipped') or [])}건 누락: {missing})"
        )


def preflight_local(
    recipe: Mapping[str, Any],
    *,
    catalog: Optional[Mapping[str, Any]] = None,
    catalog_path: Optional[Path] = None,
    assets_dir: Optional[Path] = None,
    exists: Optional[Callable[[str], bool]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """로컬 파일 시스템으로 존재를 확인한다.

    반환값은 `(필터된 레시피, 공개 리포트)`다. 참조가 하나도 없으면 카탈로그를
    읽지 않고 `checked=0`을 돌려준다(기존 실행에 비용을 더하지 않는다).
    """
    references = collect_references(recipe)
    if not references:
        return copy.deepcopy(dict(recipe)), empty_report("local")

    if catalog is None:
        if catalog_path is None:
            raise PreflightError("자산 카탈로그 경로가 필요합니다")
        catalog = load_catalog(Path(catalog_path))

    base = Path(assets_dir).expanduser() if assets_dir is not None else Path(".")

    def to_asset_path(value: str) -> str:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        return str(candidate)

    index = catalog_index(catalog)
    resolved = _resolve(references, index, to_asset_path=to_asset_path)
    checker = exists or (lambda path: Path(path).is_file())
    found = {entry.path: bool(checker(entry.path)) for entry in resolved if entry.probe}
    replacements = _pick_replacements(
        resolved,
        found,
        index,
        to_asset_path=to_asset_path,
        is_present=checker,
    )
    report = _build_report("local", resolved, found, replacements=len(replacements))
    filtered = filter_recipe(
        recipe, report["_skipped_names"], replacements=replacements
    )
    return filtered, _public(report)


def remote_probe(
    paths: Sequence[str],
    *,
    ssh_prefix: Sequence[str],
    run: Optional[Callable[..., str]] = None,
    timeout: float = 300,
) -> Dict[str, bool]:
    """ssh **한 번**으로 원격 파일 존재를 확인한다.

    `ssh_prefix`는 호스트까지 포함한 ssh argv다(`remote_assets.ssh_argv`).
    원격 셸에서 `for p in "$@"; do test -f ...; done`을 돌리고
    `ok<TAB>path` / `missing<TAB>path`를 받는다.
    """
    unique = list(dict.fromkeys(str(path) for path in paths))
    if not unique:
        return {}
    script = (
        'for p in "$@"; do '
        'if [ -f "$p" ]; then printf "ok\\t%s\\n" "$p"; '
        'else printf "missing\\t%s\\n" "$p"; fi; done'
    )
    remote_command = " ".join(
        ["/bin/sh", "-c", shlex.quote(script), "sonagi-preflight"]
        + [shlex.quote(path) for path in unique]
    )
    argv = [*ssh_prefix, remote_command]
    try:
        if run is not None:
            output = run([str(arg) for arg in argv], timeout=timeout)
        else:
            from graphs.core import exec as core_exec

            output = core_exec.run([str(arg) for arg in argv], timeout=timeout).stdout
    except Exception as exc:  # noqa: BLE001 - 원인을 보존해 PreflightError로 올린다
        raise PreflightError(f"원격 자산 확인(ssh)에 실패했습니다: {exc}") from exc

    found: Dict[str, bool] = {}
    for line in str(output or "").splitlines():
        status, separator, path = line.partition("\t")
        if not separator:
            continue
        if status == "ok":
            found[path] = True
        elif status == "missing":
            found[path] = False
    unknown = [path for path in unique if path not in found]
    if unknown:
        raise PreflightError(
            "원격 자산 확인 응답을 해석할 수 없습니다: " + ", ".join(unknown[:5])
        )
    return found


def preflight_remote(
    recipe: Mapping[str, Any],
    *,
    catalog: Optional[Mapping[str, Any]] = None,
    catalog_path: Optional[Path] = None,
    ssh_prefix: Sequence[str],
    to_remote_path: Optional[Callable[[str], str]] = None,
    run: Optional[Callable[..., str]] = None,
    timeout: float = 300,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """devops의 파일 시스템으로 존재를 확인한다(ssh 1회, 배치)."""
    references = collect_references(recipe)
    if not references:
        return copy.deepcopy(dict(recipe)), empty_report("remote")

    if catalog is None:
        if catalog_path is None:
            raise PreflightError("자산 카탈로그 경로가 필요합니다")
        catalog = load_catalog(Path(catalog_path))

    convert = to_remote_path or (lambda value: value)
    index = catalog_index(catalog)
    resolved = _resolve(references, index, to_asset_path=convert)
    found = remote_probe(
        [entry.path for entry in resolved if entry.probe],
        ssh_prefix=ssh_prefix,
        run=run,
        timeout=timeout,
    )
    # 필수 ctaIcon 대체 후보는 1차 배치에 없으므로, 필요할 때만 한 번 더 확인한다.
    missing_icons = [
        entry
        for entry in resolved
        if "ctaIcon" in entry.fields and not found.get(entry.path, False)
    ]
    if missing_icons:
        candidates = [
            path
            for name in _fallback_icon_names(index)
            if (path := _icon_path(index, name, convert)) is not None
            and path not in found
        ]
        if candidates:
            found.update(
                remote_probe(
                    candidates,
                    ssh_prefix=ssh_prefix,
                    run=run,
                    timeout=timeout,
                )
            )
    replacements = _pick_replacements(
        resolved,
        found,
        index,
        to_asset_path=convert,
        is_present=lambda path: found.get(path, False),
    )
    report = _build_report("remote", resolved, found, replacements=len(replacements))
    filtered = filter_recipe(
        recipe, report["_skipped_names"], replacements=replacements
    )
    return filtered, _public(report)


# ---------------------------------------------------------------------------
# 렌더 어댑터가 쓰는 얇은 래퍼
# ---------------------------------------------------------------------------


#: 같은 레시피·카탈로그·렌더 설정이면 web profile의 변주 루프에서 ssh를 반복하지
#: 않는다(최대 24변주 → 1회). 파일이 바뀌면 stamp가 달라져 자동 무효화된다.
_PREFLIGHT_CACHE: Dict[Any, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
_PREFLIGHT_CACHE_LIMIT = 8


def _file_stamp(path: Optional[Path]) -> Optional[Tuple[str, int, int, int]]:
    if path is None:
        return None
    resolved = Path(path).expanduser().resolve()
    try:
        stat = resolved.stat()
    except OSError:
        return (str(resolved), 0, 0, 0)
    return (str(resolved), stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _cache_key(
    *,
    mode: str,
    path: Path,
    catalog_path: Optional[Path],
    assets_dir: Optional[Path],
    ssh_prefix: Optional[Sequence[str]],
    run: Optional[Callable[..., str]],
    exists: Optional[Callable[[str], bool]],
    cache_key: Any,
) -> Any:
    return (
        mode,
        _file_stamp(path),
        _file_stamp(catalog_path),
        str(assets_dir) if assets_dir is not None else None,
        tuple(ssh_prefix) if ssh_prefix else None,
        id(run),
        id(exists),
        cache_key,
    )


def preflight_recipe_file(
    recipe_path: Path,
    *,
    mode: str = "local",
    catalog: Optional[Mapping[str, Any]] = None,
    catalog_path: Optional[Path] = None,
    assets_dir: Optional[Path] = None,
    ssh_prefix: Optional[Sequence[str]] = None,
    to_remote_path: Optional[Callable[[str], str]] = None,
    run: Optional[Callable[..., str]] = None,
    timeout: float = 300,
    exists: Optional[Callable[[str], bool]] = None,
    cache: bool = False,
    cache_key: Any = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """레시피 파일을 읽어 preflight하고 `(필터된 레시피, 리포트)`를 돌려준다."""
    if mode not in ("local", "remote"):
        raise PreflightError(f"알 수 없는 preflight 모드: {mode!r}")
    path = Path(recipe_path).expanduser().resolve()
    key = None
    if cache and catalog is None:
        key = _cache_key(
            mode=mode,
            path=path,
            catalog_path=catalog_path,
            assets_dir=assets_dir,
            ssh_prefix=ssh_prefix,
            run=run,
            exists=exists,
            cache_key=cache_key,
        )
        cached = _PREFLIGHT_CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached[0]), copy.deepcopy(cached[1])

    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PreflightError(f"레시피를 읽을 수 없습니다: {path} ({exc})") from exc
    if not isinstance(recipe, Mapping):
        raise PreflightError(f"레시피 형식이 아닙니다: {path}")
    if mode == "remote":
        if not ssh_prefix:
            raise PreflightError("원격 preflight에는 ssh 접두 인자가 필요합니다")
        result = preflight_remote(
            recipe,
            catalog=catalog,
            catalog_path=catalog_path,
            ssh_prefix=ssh_prefix,
            to_remote_path=to_remote_path,
            run=run,
            timeout=timeout,
        )
    else:
        result = preflight_local(
            recipe,
            catalog=catalog,
            catalog_path=catalog_path,
            assets_dir=assets_dir,
            exists=exists,
        )
    if key is not None:
        if len(_PREFLIGHT_CACHE) >= _PREFLIGHT_CACHE_LIMIT:
            _PREFLIGHT_CACHE.pop(next(iter(_PREFLIGHT_CACHE)))
        _PREFLIGHT_CACHE[key] = copy.deepcopy(result)
    return result


def write_filtered_recipe(
    recipe_path: Path, recipe: Mapping[str, Any], report: Mapping[str, Any]
) -> Path:
    """누락이 있으면 렌더러에 넘길 `<stem>.preflight.json`을 남긴다.

    누락이 없으면 원본 경로를 그대로 돌려준다(불필요한 파일을 만들지 않는다).
    """
    if not report.get("skipped"):
        return Path(recipe_path)
    source = Path(recipe_path).expanduser().resolve()
    target = source.with_name(f"{source.stem}.preflight.json")
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(recipe, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target
