"""core/exec — subprocess는 여기서만 실행한다.

`entry_pack`·`asset_craft`·`logo_design`·`video_ops`·`design_ops`가 각자 `_run`을
복제하면서 타임아웃·cwd·에러 문구가 조금씩 달라졌다. 여기서는 실패를 삼키지
않는다. 종료 코드가 0이 아니면 `CommandError`가 올라가고, 호출자는 argv·종료
코드·stdout·stderr·시도 횟수를 구조화된 형태로 받는다.

- 타임아웃: `TimeoutExpired`를 잡아 `timed_out=True` 결과로 만든 뒤 실패 처리
- 재시도: `retries`만큼 반복. `retry_codes`로 재시도할 종료 코드를 좁힌다
- 로그: `CommandLog`(JSONL)에 시도마다 한 줄. 경로를 넘기면 자동 생성

사용::

    from graphs.core import exec as core_exec

    result = core_exec.run(
        [sys.executable, "scripts/render_recipe.py", "--out", out],
        timeout=120,
        retries=1,
        log=run_dir / "commands.jsonl",
    )
    result.stdout, result.returncode

    core_exec.run_python("scripts/lint_render.py", [svg, "--tokens", tokens])
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

DEFAULT_TIMEOUT = 300
DEFAULT_TAIL = 600

LogTarget = Union["CommandLog", str, "os.PathLike[str]", None]


@dataclass(frozen=True)
class RunResult:
    """한 명령의 최종 결과(재시도 포함). 실패해도 값을 버리지 않는다."""

    argv: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    attempts: int = 1
    timed_out: bool = False
    timeout: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def command(self) -> str:
        return " ".join(self.argv)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "timed_out": self.timed_out,
            "timeout": self.timeout,
            "ok": self.ok,
        }


def format_failure(result: RunResult, *, tail: int = DEFAULT_TAIL) -> str:
    """사람이 읽는 실패 문구. argv·코드·양쪽 출력 꼬리를 한 번에 보여준다."""
    if result.timed_out:
        head = f"시간 초과({result.timeout}초): {result.command} (시도 {result.attempts}회)"
    else:
        head = f"실패: {result.command} (종료 코드 {result.returncode}, 시도 {result.attempts}회)"
    out = result.stdout[-tail:].strip() if result.stdout else ""
    err = result.stderr[-tail:].strip() if result.stderr else ""
    lines = [head]
    if out:
        lines += ["stdout:", out]
    if err:
        lines += ["stderr:", err]
    return "\n".join(lines)


class CommandError(RuntimeError):
    """0이 아닌 종료 코드 또는 타임아웃. 원본 결과를 그대로 들고 있다."""

    def __init__(self, result: RunResult, *, tail: int = DEFAULT_TAIL):
        self.result = result
        self.argv = result.argv
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        self.timed_out = result.timed_out
        self.attempts = result.attempts
        super().__init__(format_failure(result, tail=tail))

    def to_dict(self) -> Dict[str, Any]:
        return {"error": "CommandError", **self.result.to_dict()}


@dataclass
class CommandLog:
    """JSONL 명령 로그. `path=None`이면 아무것도 쓰지 않는다."""

    path: Optional[Path] = None
    _entries: List[Dict[str, Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)

    def record(self, result: RunResult) -> None:
        entry = result.to_dict()
        entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._entries.append(entry)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @property
    def entries(self) -> List[Dict[str, Any]]:
        return list(self._entries)

    def read(self) -> List[Dict[str, Any]]:
        if self.path is None or not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def _as_log(target: LogTarget) -> Optional[CommandLog]:
    if target is None:
        return None
    if isinstance(target, CommandLog):
        return target
    return CommandLog(Path(target))


def _once(
    argv: Tuple[str, ...],
    *,
    timeout: Optional[float],
    cwd: Optional[Union[str, "os.PathLike[str]"]],
    env: Optional[Dict[str, str]],
    stdin_text: Optional[str],
) -> Tuple[int, str, str, bool, int]:
    started = time.monotonic()
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            input=stdin_text,
        )
        elapsed = int((time.monotonic() - started) * 1000)
        return done.returncode, done.stdout or "", done.stderr or "", False, elapsed
    except subprocess.TimeoutExpired as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return (
            -9,
            _text(exc.stdout),
            _text(exc.stderr) or f"시간 초과: {timeout}초 안에 끝나지 않았다",
            True,
            elapsed,
        )


def _text(value: Union[str, bytes, None]) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run(
    argv: Sequence[Any],
    *,
    timeout: Optional[float] = DEFAULT_TIMEOUT,
    cwd: Optional[Union[str, "os.PathLike[str]"]] = None,
    env: Optional[Dict[str, str]] = None,
    retries: int = 0,
    retry_codes: Optional[Iterable[int]] = None,
    retry_on_timeout: bool = False,
    check: bool = True,
    log: LogTarget = None,
    stdin_text: Optional[str] = None,
) -> RunResult:
    """명령을 실행하고 결과를 돌려준다. 실패하면 `check`에 따라 예외를 던진다.

    `retries`는 추가 시도 횟수다. `retry_codes`가 None이면 실패 전부를
    재시도하고, 값을 주면 그 종료 코드만 재시도한다. 타임아웃 재시도는
    `retry_on_timeout=True`일 때만 한다(기본은 즉시 실패 — 오래 걸리는
    작업을 두 번 돌리면 대기 시간만 두 배가 된다).
    """
    if not argv:
        raise ValueError("argv가 비어 있다. 실행할 명령을 넘겨라")
    command: Tuple[str, ...] = tuple(str(arg) for arg in argv)
    if retries < 0:
        raise ValueError("retries는 0 이상이어야 한다")
    codes = None if retry_codes is None else set(int(code) for code in retry_codes)
    logger = _as_log(log)
    total = retries + 1

    result: Optional[RunResult] = None
    for attempt in range(1, total + 1):
        code, out, err, timed_out, elapsed = _once(
            command, timeout=timeout, cwd=cwd, env=env, stdin_text=stdin_text
        )
        result = RunResult(
            argv=command,
            returncode=code,
            stdout=out,
            stderr=err,
            duration_ms=elapsed,
            attempts=attempt,
            timed_out=timed_out,
            timeout=timeout,
        )
        if logger is not None:
            logger.record(result)
        if result.ok or attempt == total:
            break
        if timed_out and not retry_on_timeout:
            break
        if codes is not None and not timed_out and code not in codes:
            break

    assert result is not None  # total >= 1 이므로 항상 채워진다
    if check and not result.ok:
        raise CommandError(result)
    return result


def run_python(
    script: Union[str, "os.PathLike[str]"],
    args: Sequence[Any] = (),
    **kwargs: Any,
) -> RunResult:
    """현재 인터프리터로 스크립트를 실행한다(그래프들이 쓰던 `sys.executable` 패턴)."""
    return run([sys.executable, str(script), *args], **kwargs)


def which(program: str, default: Optional[str] = None) -> str:
    """PATH에서 실행 파일을 찾고, 없으면 `default`(또는 원래 이름)를 돌려준다."""
    import shutil

    return shutil.which(program) or default or program
