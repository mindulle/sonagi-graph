#!/usr/bin/env python3
"""LangGraph 서버 원격 제어 CLI — Kimaki/Discord에서 그래프를 실행하고 HITL을 승인한다.

서버(:8123)의 HTTP API만 쓴다. 그래프 모듈은 import하지 않으므로, Kimaki가 어느
디렉터리에서 실행되든 동작한다. 원장 조회(`runs`·`show`)는 manifest를 읽기 위해
표준 라이브러리만 쓰는 `graphs.core.artifacts`만 가져온다.

사용 예
    python3 scripts/graph_cli.py list
    python3 scripts/graph_cli.py run error_triage \
        --input '{"messages":[{"role":"user","content":"TypeError: ..."}]}' --wait
    python3 scripts/graph_cli.py status <thread_id>
    python3 scripts/graph_cli.py approve <thread_id> --graph copywriting_craft \
        --values '{"selected_option_index":1}' --wait
    python3 scripts/graph_cli.py runs --limit 10
    python3 scripts/graph_cli.py show <thread_id>

환경 변수
    LANGGRAPH_URL   서버 주소 (기본 http://127.0.0.1:8123)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from graphs.core import artifacts  # noqa: E402  (저장소 루트의 core만 읽는다)

BASE = os.environ.get("LANGGRAPH_URL", "http://127.0.0.1:8123").rstrip("/")
TERMINAL = {"success", "error", "failed", "interrupted", "timeout"}

#: 상태에서 manifest 경로로 볼 키. 값은 문자열·문자열 목록·`{"path": ...}` 모두 가능.
MANIFEST_KEYS = frozenset(
    {
        "manifest",
        "manifests",
        "manifest_path",
        "manifest_paths",
        "manifest_file",
        "manifest_files",
        "run_manifest",
        "artifacts_manifest",
    }
)


def _request(method, path, payload=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise SystemExit(f"HTTP {e.code} {path}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"서버에 연결할 수 없습니다 ({BASE}): {e.reason}")
    return json.loads(body) if body else None


def _state(thread_id):
    return _request("GET", f"/threads/{thread_id}/state")


def _runs(thread_id):
    return _request("GET", f"/threads/{thread_id}/runs")


def _latest_run(thread_id):
    runs = _runs(thread_id)
    return runs[0] if runs else {}


def _first_present(*values):
    """처음으로 비어 있지 않은 값. 없으면 None."""
    for value in values:
        if value not in (None, ""):
            return value
    return None


def short_time(value):
    """시각을 `YYYY-MM-DDTHH:MM:SSZ`(UTC)로 줄인다. 못 읽으면 원문 그대로 둔다."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if abs(value) > 1e11 else value  # 밀리초도 받아준다
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    text = str(value)
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return text
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_row(thread, latest_run=None):
    """스레드(+마지막 실행) 하나를 원장 한 줄로 정규화한다."""
    thread = thread or {}
    latest_run = latest_run or {}
    metadata = thread.get("metadata") or {}
    return {
        "thread_id": _first_present(thread.get("thread_id"), thread.get("id")),
        "graph_id": _first_present(
            metadata.get("graph_id"),
            metadata.get("assistant_id"),
            thread.get("graph_id"),
            thread.get("assistant_id"),
            latest_run.get("assistant_id"),
        ),
        "status": _first_present(thread.get("status"), latest_run.get("status")),
        "created_at": short_time(
            _first_present(
                thread.get("created_at"),
                thread.get("updated_at"),
                latest_run.get("created_at"),
            )
        ),
    }


def ledger_rows(threads, limit=None):
    """스레드 목록 → 최신순 원장. `limit`이 있으면 그만큼만 남긴다."""
    rows = [ledger_row(thread) for thread in threads or ()]
    rows.sort(
        key=lambda row: (row.get("created_at") or "", row.get("thread_id") or ""),
        reverse=True,
    )
    if limit and limit > 0:
        return rows[:limit]
    return rows


def _table(headers, rows):
    """열 폭을 맞춘 평문 표."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for row in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip())
    return "\n".join(lines)


def format_ledger(rows):
    """원장 표. 비어 있으면 사람이 읽을 수 있는 한 줄."""
    if not rows:
        return "실행 기록 없음"
    body = [
        [
            row.get("thread_id") or "-",
            row.get("graph_id") or "-",
            row.get("status") or "-",
            row.get("created_at") or "-",
        ]
        for row in rows
    ]
    return _table(("thread_id", "graph", "status", "created"), body)


def _looks_like_manifest_path(text):
    """공백 없는 문자열이 `manifest.json`으로 끝나면 경로로 본다(산문 제외)."""
    if not text or any(char.isspace() for char in text):
        return False
    return text.replace("\\", "/").rstrip("/").endswith("manifest.json")


def manifest_paths_from_values(values):
    """상태 값 어디에 있든 manifest 경로를 모은다(중첩·목록 포함, 순서 유지)."""
    found = []

    def add(value):
        if not isinstance(value, str):
            return
        candidate = value.strip()
        if candidate and candidate not in found:
            found.append(candidate)

    def walk(node, depth=0):
        if depth > 20:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in MANIFEST_KEYS:
                    items = value if isinstance(value, (list, tuple, set)) else [value]
                    for item in items:
                        if isinstance(item, dict):
                            add(item.get("path") or item.get("manifest_path"))
                        else:
                            add(item)
                walk(value, depth + 1)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, str) and _looks_like_manifest_path(node):
            add(node)

    walk(values)
    return found


def manifest_report(paths):
    """manifest 경로별 존재 여부·파일 sha256·manifest 지문·요약."""
    rows = []
    for raw in paths:
        path = Path(str(raw)).expanduser()
        row = {"path": str(path), "exists": False}
        rows.append(row)
        if not path.exists():
            continue
        row["exists"] = True
        try:
            manifest = artifacts.read_manifest(path)
            manifest_path = (
                path if path.suffix == ".json" else path / artifacts.MANIFEST_NAME
            )
            row["manifest_path"] = str(manifest_path)
            row["sha256"] = artifacts.sha256_file(manifest_path)
            row["digest"] = artifacts.manifest_digest(manifest)
        except (OSError, ValueError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            continue
        row["run_id"] = manifest.get("run_id")
        row["kind"] = manifest.get("kind")
        row["status"] = manifest.get("status")
        row["artifacts"] = len(manifest.get("artifacts") or ())
    return rows


def format_manifests(rows):
    """manifest 목록을 사람이 읽는 형태로. 없으면 `manifest 없음`."""
    if not rows:
        return "manifest 없음"
    lines = []
    for row in rows:
        lines.append(row["path"])
        if not row.get("exists"):
            lines.append("  파일 없음")
            continue
        if row.get("error"):
            lines.append(f"  읽기 실패: {row['error']}")
            continue
        lines.append(f"  sha256: {row.get('sha256')}")
        lines.append(f"  digest: {row.get('digest')}")
        lines.append(
            "  kind: {}  status: {}  artifacts: {}".format(
                row.get("kind") or "-",
                row.get("status") or "-",
                row.get("artifacts") or 0,
            )
        )
    return "\n".join(lines)


def _wait(thread_id, timeout=180, interval=3):
    """실행이 끝나거나 HITL에서 멈출 때까지 기다린다.

    실행 중에도 `next`에는 대기 중인 노드가 들어 있으므로, `next`가 아니라
    run status(terminal)를 기준으로 멈춘다. 멈춘 뒤 `next`가 비어 있지 않으면
    HITL 게이트에서 대기 중이라는 뜻이다.
    """
    deadline = time.time() + timeout
    state = {}
    run = {}
    while time.time() < deadline:
        state = _state(thread_id)
        run = _latest_run(thread_id)
        if run.get("status") in TERMINAL:
            return state, run
        time.sleep(interval)
    return _state(thread_id), _latest_run(thread_id)


def state_summary(thread_id, state, run):
    """스레드 상태 요약 — `status`·`show` 공통."""
    state = state or {}
    values = state.get("values") if isinstance(state.get("values"), dict) else {}
    return {
        "thread_id": thread_id,
        "run_status": (run or {}).get("status"),
        "next": state.get("next") or [],
        "status": values.get("status"),
    }


def _print_summary(thread_id, state, run, as_json=False):
    summary = state_summary(thread_id, state, run)
    next_nodes = summary["next"]
    values = (state or {}).get("values") or {}
    if as_json:
        print(
            json.dumps(
                {"summary": summary, "values": values}, ensure_ascii=False, default=str
            )
        )
        return
    print(json.dumps(summary, ensure_ascii=False))
    if next_nodes:
        print(f"⏸️  HITL 대기: next={next_nodes}")
        print(
            f"   재개: python3 scripts/graph_cli.py approve {thread_id} --graph <graph> --values '<json>'"
        )
    else:
        print("✅ 완료")


def cmd_list(args):
    assistants = _request("POST", "/assistants/search", {"limit": 100})
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "graph_id": a.get("graph_id"),
                        "assistant_id": a.get("assistant_id"),
                    }
                    for a in assistants
                ],
                ensure_ascii=False,
            )
        )
        return
    for a in sorted(assistants, key=lambda x: x.get("graph_id") or ""):
        print(f"{a.get('graph_id')}\t{a.get('assistant_id')}")


def cmd_run(args):
    payload = json.loads(args.input)
    thread_id = _request("POST", "/threads", {})["thread_id"]
    _request(
        "POST",
        f"/threads/{thread_id}/runs",
        {"assistant_id": args.graph, "input": payload},
    )
    if not args.wait:
        print(
            json.dumps(
                {"thread_id": thread_id, "status": "started"}, ensure_ascii=False
            )
        )
        return
    state, run = _wait(thread_id, timeout=args.timeout)
    _print_summary(thread_id, state, run, as_json=args.json)


def cmd_status(args):
    state = _state(args.thread_id)
    run = _latest_run(args.thread_id)
    _print_summary(args.thread_id, state, run, as_json=args.json)


def cmd_approve(args):
    values = json.loads(args.values)
    # 1) 사람이 고른 값을 상태에 반영
    _request("POST", f"/threads/{args.thread_id}/state", {"values": values})
    # 2) input=None으로 재개 (플랫폼이 마지막 체크포인트부터 이어서 실행)
    _request(
        "POST",
        f"/threads/{args.thread_id}/runs",
        {"assistant_id": args.graph, "input": None},
    )
    if not args.wait:
        print(
            json.dumps(
                {"thread_id": args.thread_id, "status": "resumed"}, ensure_ascii=False
            )
        )
        return
    state, run = _wait(args.thread_id, timeout=args.timeout)
    _print_summary(args.thread_id, state, run, as_json=args.json)


def cmd_runs(args):
    # 하위 호환: `runs <thread_id>`는 예전처럼 그 스레드의 실행 이력만 보여준다.
    if getattr(args, "thread_id", None):
        runs = _runs(args.thread_id)
        print(
            json.dumps(
                [{"run_id": r.get("run_id"), "status": r.get("status")} for r in runs],
                ensure_ascii=False,
            )
        )
        return
    limit = max(1, int(getattr(args, "limit", 10)))
    rows = ledger_rows(_search_threads(limit), limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    print(format_ledger(rows))


def _search_threads(limit):
    """최근 스레드를 최신순으로. 정렬 인자를 모르는 서버면 limit만 보낸다."""
    payload = {"limit": limit, "sort_by": "thread_id", "sort_order": "desc"}
    try:
        threads = _request("POST", "/threads/search", payload)
    except SystemExit:
        threads = _request("POST", "/threads/search", {"limit": limit})
    return threads or []


def cmd_show(args):
    state = _state(args.thread_id)
    run = _latest_run(args.thread_id)
    values = (state or {}).get("values")
    manifests = manifest_report(manifest_paths_from_values(values))
    if args.json:
        print(
            json.dumps(
                {
                    "summary": state_summary(args.thread_id, state, run),
                    "values": values,
                    "manifests": manifests,
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return
    for key, value in state_summary(args.thread_id, state, run).items():
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        print(f"{key}: {value}")
    print(format_manifests(manifests))


def main(argv=None):
    global BASE
    parser = argparse.ArgumentParser(description="LangGraph 서버 원격 제어 CLI")
    parser.add_argument("--url", help=f"서버 주소 (기본 {BASE})")
    parser.add_argument("--json", action="store_true", help="JSON으로 출력")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="등록된 assistant 목록")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run", help="그래프 실행")
    p.add_argument("graph", help="graph_id (예: error_triage)")
    p.add_argument("--input", required=True, help="초기 상태 JSON")
    p.add_argument("--wait", action="store_true", help="완료/HITL까지 대기")
    p.add_argument("--timeout", type=int, default=180, help="대기 시간(초)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="스레드 상태")
    p.add_argument("thread_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve", help="HITL 승인 후 재개")
    p.add_argument("thread_id")
    p.add_argument("--graph", required=True, help="graph_id")
    p.add_argument(
        "--values",
        required=True,
        help='반영할 상태 JSON (예: {"selected_option_index":1})',
    )
    p.add_argument("--wait", action="store_true", help="완료/HITL까지 대기")
    p.add_argument("--timeout", type=int, default=180, help="대기 시간(초)")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("runs", help="최근 실행 원장 (thread_id를 주면 그 스레드 이력)")
    p.add_argument("thread_id", nargs="?", help="있으면 그 스레드의 실행 이력")
    p.add_argument("--limit", type=int, default=10, help="최근 N개 (기본 10)")
    p.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="JSON으로 출력",
    )
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("show", help="스레드 상태 + 산출 manifest")
    p.add_argument("thread_id")
    p.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="JSON으로 출력",
    )
    p.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    if args.url:
        BASE = args.url.rstrip("/")
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
