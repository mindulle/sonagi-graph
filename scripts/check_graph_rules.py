#!/usr/bin/env python3
"""graphs/ 직접 실행 금지 가드레일 (GRAPH-15 / P5).

규칙
----
`graphs/**/*.py` 는 프로세스를 **직접** 실행하지 않는다.
실행은 `graphs/core/exec.py` 한 곳을 통해서만 한다.
(타임아웃·에러 형식·로그가 그래프마다 제각각이 되는 것을 막는다 — docs/GRAPH_REGISTRY.md §2)

금지 대상(호출 기준, import 자체는 허용):
  - subprocess.run / Popen / call / check_call / check_output / getoutput / getstatusoutput
  - `from subprocess import run` 같은 from-import 후의 직접 호출
  - os.system / os.popen (from-import 포함)
  - asyncio.create_subprocess_exec / create_subprocess_shell

허용 목록
--------
ALLOWED_FILES        영구 허용 경로. 지금은 `graphs/core/exec.py` 뿐이며,
                     아직 없어도 된다(core 도입 전에도 lint가 통과해야 한다).
LEGACY_BASELINE      P0 인벤토리 시점의 **기존** 위반 파일과 허용 개수.
                     개수를 넘으면 실패하고, 줄어들면 "줄일 수 있음" 안내만 낸다.
                     항목을 늘리는 것은 금지. W2 core 이관이 끝나면 항목을 삭제한다.

사용법
-----
  python3 scripts/check_graph_rules.py                 # 저장소 graphs/ 검사 (CI)
  python3 scripts/check_graph_rules.py --strict         # baseline 무시 (core 이관 완료 후 목표)
  python3 scripts/check_graph_rules.py --path FILE_OR_DIR  # 특정 경로만 검사 (로컬 검증용)

종료 코드: 0 = 통과, 1 = 위반.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 영구 허용: 프로세스 실행은 이 파일에서만.
ALLOWED_FILES: frozenset[str] = frozenset({"graphs/core/exec.py"})

# P0(GRAPH-15) 시점의 부채. 숫자는 "허용 상한"이며 늘리지 않는다.
LEGACY_BASELINE: dict[str, int] = {
    "graphs/entry_pack_graph.py": 2,
    "graphs/video_ops_graph.py": 5,
    "graphs/design_ops_graph.py": 4,
    "graphs/logo_design_graph.py": 1,
    "graphs/asset_craft_graph.py": 1,
}

SUBPROCESS_CALLS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
    }
)
OS_CALLS = frozenset({"system", "popen"})
ASYNCIO_CALLS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})


class Violation:
    __slots__ = ("path", "line", "rule", "snippet")

    def __init__(self, path: str, line: int, rule: str, snippet: str) -> None:
        self.path = path
        self.line = line
        self.rule = rule
        self.snippet = snippet

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} — {self.snippet}"


def _rel(path: Path) -> str:
    """저장소 기준 상대 경로. 저장소 밖이면 절대 경로(따라서 baseline 미적용)."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _from_imports(tree: ast.AST) -> dict[str, str]:
    """`from X import y as z` 형태에서 로컬 이름 → 원래 이름 매핑을 모은다."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "subprocess",
            "os",
            "asyncio",
        }:
            for alias in node.names:
                local = alias.asname or alias.name
                aliases[local] = f"{node.module}.{alias.name}"
    return aliases


def _call_target(node: ast.Call, aliases: dict[str, str]) -> str | None:
    """호출 대상의 정규화된 이름(`subprocess.run` 등)을 돌려준다."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    return None


def _rule_of(target: str | None) -> str | None:
    if target is None or "." not in target:
        return None
    module, _, name = target.rpartition(".")
    if module == "subprocess" and name in SUBPROCESS_CALLS:
        return "subprocess-direct"
    if module == "os" and name in OS_CALLS:
        return "os-exec-direct"
    if module == "asyncio" and name in ASYNCIO_CALLS:
        return "asyncio-subprocess-direct"
    return None


def scan_file(path: Path) -> list[Violation]:
    rel = _rel(path)
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=rel)
    except (
        OSError,
        SyntaxError,
    ) as exc:  # 파싱 불가 파일은 위반으로 보고하지 않고 경고만.
        print(f"warning: 건너뜀 ({rel}): {exc}", file=sys.stderr)
        return []

    lines = source.splitlines()
    aliases = _from_imports(tree)
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        rule = _rule_of(_call_target(node, aliases))
        if rule is None:
            continue
        lineno = node.lineno
        snippet = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        found.append(Violation(rel, lineno, rule, snippet[:120]))
    return found


def iter_python_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target] if target.suffix == ".py" else []
    files = []
    for candidate in sorted(target.rglob("*.py")):
        if "__pycache__" in candidate.parts:
            continue
        files.append(candidate)
    return files


def check(target: Path, strict: bool) -> tuple[list[Violation], list[str]]:
    problems: list[Violation] = []
    notes: list[str] = []

    for path in iter_python_files(target):
        rel = _rel(path)
        violations = scan_file(path)

        if rel in ALLOWED_FILES:
            if violations:
                notes.append(f"{rel}: 허용 경로 (core) — 직접 실행 {len(violations)}건")
            continue

        if not violations:
            if rel in LEGACY_BASELINE:
                notes.append(
                    f"{rel}: baseline {LEGACY_BASELINE[rel]}건 → 0건, 항목을 삭제하세요"
                )
            continue

        if strict:
            problems.extend(violations)
            continue

        budget = LEGACY_BASELINE.get(rel, 0)
        if len(violations) <= budget:
            notes.append(f"{rel}: baseline {len(violations)}/{budget}건 소진")
        if len(violations) > budget:
            remaining = violations[budget:]
            problems.extend(remaining)
            if budget:
                notes.append(
                    f"{rel}: baseline {budget}건 초과 (직접 실행 {len(violations)}건) "
                    "— 신규 호출은 graphs/core/exec.py로 옮기세요"
                )

    return problems, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="graphs/ 직접 subprocess 실행 금지 lint (GRAPH-15)"
    )
    parser.add_argument(
        "--path",
        default=str(REPO_ROOT / "graphs"),
        help="검사 대상 파일 또는 디렉터리 (기본: 저장소 graphs/)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="LEGACY_BASELINE을 무시하고 위반 0건을 요구 (core 이관 완료 후 목표)",
    )
    args = parser.parse_args(argv)

    target = Path(args.path)
    if not target.exists():
        print(f"error: 경로 없음: {target}", file=sys.stderr)
        return 1

    files = iter_python_files(target)
    problems, notes = check(target, args.strict)

    for note in notes:
        print(f"note: {note}")

    if problems:
        print(f"\n❌ graphs/ 직접 실행 위반 {len(problems)}건\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem.render()}", file=sys.stderr)
        print(
            "\n규칙: 프로세스 실행은 graphs/core/exec.py 한 곳에서만.\n"
            "      그래프에서 직접 subprocess/os.system/asyncio subprocess를 호출하지 않습니다.\n"
            "      (docs/GRAPH_REGISTRY.md §7)",
            file=sys.stderr,
        )
        return 1

    print(f"✅ graphs/ 직접 실행 lint 통과 — 파일 {len(files)}개 검사")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
