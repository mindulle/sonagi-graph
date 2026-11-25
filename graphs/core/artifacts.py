"""core/artifacts — 실행 흔적을 남긴다.

산출물은 지워져도 "무엇으로 무엇을 만들었나"는 남아야 한다. 실행마다
디렉터리를 만들고 그 안에 `manifest.json`을 쓴다. 해시는 두 경로만 쓴다 —
파일 바이트, 그리고 정규화한 JSON(키 정렬·공백 제거).

구조::

    OUT_ROOT/run-20261125T163000Z-a1b2c3/
      manifest.json
      commands.jsonl     # core/exec가 쓰는 로그(선택)
      ...

manifest는 결정론을 해치지 않는다. `manifest_digest()`는 `run_id`·`created_at`
같은 실행마다 달라지는 키를 빼고 계산하므로, 같은 입력 두 번이면 같은 해시다.

사용::

    from graphs.core import artifacts

    run_dir = artifacts.create_run_dir(env.out_root("entry_pack"), prefix="entry")
    manifest = artifacts.build_manifest(
        run_id=run_dir.name,
        kind="entry_pack",
        inputs={"brief": artifacts.sha256_bytes(brief.encode())},
        artifacts=[artifacts.artifact_entry(run_dir / "pack/meta.json")],
    )
    artifacts.write_manifest(run_dir, manifest)
    artifacts.prune(env.out_root("entry_pack"), keep=20)
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

SCHEMA = 1
MANIFEST_NAME = "manifest.json"
RUN_PATTERN = "run-*"
CHUNK = 1 << 20

#: 실행마다 달라지는 키. 지문 계산에서 제외한다.
VOLATILE_KEYS = (
    "run_id",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "duration_ms",
    "host",
    "pid",
)

PathLike = Union[str, "Path"]


def sha256_bytes(data: bytes) -> str:
    """바이트의 sha256. `artifacts.sha256_bytes(b"")`는 항상 같은 값이다."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: PathLike, *, chunk_size: int = CHUNK) -> str:
    """파일을 스트리밍으로 읽어 sha256을 만든다(큰 산출물도 메모리 안전)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(payload: Any) -> str:
    """결정론 직렬화 — 키 정렬, 구분자 고정, 한글 그대로."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def sha256_json(payload: Any) -> str:
    """JSON 값의 정규화 해시. 같은 값이면 키 순서와 무관하게 같은 해시."""
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def new_run_id(prefix: str = "run") -> str:
    """정렬 가능한 실행 id — `<prefix>-<UTC초>-<랜덤6>`. 같은 초에도 겹치지 않는다."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def create_run_dir(
    root: PathLike,
    run_id: Optional[str] = None,
    *,
    prefix: str = "run",
    exist_ok: bool = False,
) -> Path:
    """실행 디렉터리를 만든다. 이미 있으면 기본은 실패한다(덮어쓰지 않는다)."""
    directory = Path(root) / (run_id or new_run_id(prefix))
    directory.mkdir(parents=True, exist_ok=exist_ok)
    return directory


def artifact_entry(
    path: PathLike,
    *,
    root: Optional[PathLike] = None,
    name: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """산출물 하나의 manifest 항목. `root`가 있으면 상대 경로로 남긴다."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"산출물이 없다: {target}")
    recorded = str(target.relative_to(root)) if root is not None else str(target)
    entry: Dict[str, Any] = {
        "name": name or target.name,
        "path": recorded,
        "sha256": sha256_file(target),
        "size": target.stat().st_size,
    }
    if extra:
        entry.update(dict(extra))
    return entry


def build_manifest(
    *,
    run_id: str,
    kind: str,
    inputs: Optional[Mapping[str, Any]] = None,
    artifacts: Optional[Sequence[Mapping[str, Any]]] = None,
    gates: Optional[Sequence[Mapping[str, Any]]] = None,
    commands: Optional[Sequence[Mapping[str, Any]]] = None,
    status: str = "ok",
    meta: Optional[Mapping[str, Any]] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """manifest 본문. 시각은 UTC ISO로만 넣는다(로컬 타임존 비의존)."""
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "kind": kind,
        "status": status,
        "created_at": created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": dict(inputs or {}),
        "artifacts": [dict(item) for item in artifacts or ()],
        "gates": [dict(item) for item in gates or ()],
        "commands": [dict(item) for item in commands or ()],
        "meta": dict(meta or {}),
    }


def _manifest_path(target: PathLike) -> Path:
    path = Path(target)
    return path if path.suffix == ".json" else path / MANIFEST_NAME


def write_manifest(target: PathLike, manifest: Mapping[str, Any]) -> Path:
    """manifest를 쓴다. `target`은 디렉터리나 `*.json` 파일 모두 가능하다."""
    path = _manifest_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, **dict(manifest)}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_manifest(target: PathLike) -> Dict[str, Any]:
    """manifest를 읽는다. 없으면 `FileNotFoundError`(조용한 빈 dict 금지)."""
    path = _manifest_path(target)
    if not path.is_file():
        raise FileNotFoundError(f"manifest가 없다: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """실행별 잡음(run_id·시각)을 뺀 지문. L3 결정론 비교에 쓴다."""
    stable = {key: value for key, value in manifest.items() if key not in VOLATILE_KEYS}
    return sha256_json(stable)


@dataclass(frozen=True)
class RunArtifact:
    """`create_run()`이 돌려주는 묶음 — 디렉터리와 manifest 경로."""

    dir: Path
    manifest_path: Path

    def write(self, manifest: Mapping[str, Any]) -> Path:
        return write_manifest(self.manifest_path, manifest)

    def read(self) -> Dict[str, Any]:
        return read_manifest(self.manifest_path)


def create_run(
    root: PathLike,
    *,
    kind: str,
    prefix: str = "run",
    inputs: Optional[Mapping[str, Any]] = None,
    meta: Optional[Mapping[str, Any]] = None,
    run_id: Optional[str] = None,
) -> RunArtifact:
    """디렉터리 생성 + 초기 manifest까지 한 번에. 산출물은 나중에 덧붙인다."""
    directory = create_run_dir(root, run_id, prefix=prefix)
    manifest = build_manifest(
        run_id=directory.name, kind=kind, inputs=inputs, meta=meta, status="running"
    )
    return RunArtifact(dir=directory, manifest_path=write_manifest(directory, manifest))


def prune(
    root: PathLike,
    keep: int = 20,
    *,
    pattern: str = RUN_PATTERN,
    dry_run: bool = False,
) -> List[Path]:
    """최근 `keep`개만 남기고 오래된 실행 디렉터리를 지운다.

    이름이 시각순으로 정렬되므로(`new_run_id`) mtime에 기대지 않는다.
    `root` 바로 아래에서 `pattern`에 맞는 디렉터리만 지운다 — 경로 탈출 없음.
    """
    if keep < 0:
        raise ValueError("keep은 0 이상이어야 한다")
    base = Path(root)
    if not base.is_dir():
        return []
    runs: List[Path] = sorted(
        (child for child in base.glob(pattern) if child.is_dir()),
        key=lambda child: child.name,
        reverse=True,
    )
    removed: List[Path] = []
    for stale in runs[keep:]:
        if stale.parent != base:  # 방어: glob 결과가 루트를 벗어나면 건드리지 않는다
            continue
        removed.append(stale)
        if not dry_run:
            shutil.rmtree(stale, ignore_errors=True)
    return removed


def list_runs(root: PathLike, *, pattern: str = RUN_PATTERN) -> List[Path]:
    """최신순 실행 디렉터리 목록. `graph_cli runs`(W5)가 쓸 조회 지점."""
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(
        (child for child in base.glob(pattern) if child.is_dir()),
        key=lambda child: child.name,
        reverse=True,
    )


def totals(entries: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """산출물 항목들의 개수·바이트 합계(요약 리포트용)."""
    rows = list(entries)
    return {
        "count": len(rows),
        "bytes": sum(int(row.get("size") or 0) for row in rows),
    }
