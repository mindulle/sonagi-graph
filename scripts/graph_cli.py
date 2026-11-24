#!/usr/bin/env python3
"""LangGraph 서버 원격 제어 CLI — Kimaki/Discord에서 그래프를 실행하고 HITL을 승인한다.

서버(:8123)의 HTTP API만 쓴다. 그래프 코드를 import하지 않으므로, Kimaki가 어느
디렉터리에서 실행되든 동작한다.

사용 예
    python3 scripts/graph_cli.py list
    python3 scripts/graph_cli.py run error_triage \
        --input '{"messages":[{"role":"user","content":"TypeError: ..."}]}' --wait
    python3 scripts/graph_cli.py status <thread_id>
    python3 scripts/graph_cli.py approve <thread_id> --graph copywriting_craft \
        --values '{"selected_option_index":1}' --wait

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

BASE = os.environ.get("LANGGRAPH_URL", "http://127.0.0.1:8123").rstrip("/")
TERMINAL = {"success", "error", "failed", "interrupted", "timeout"}


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


def _print_summary(thread_id, state, run, as_json=False):
    next_nodes = state.get("next") or []
    values = state.get("values") or {}
    summary = {
        "thread_id": thread_id,
        "run_status": run.get("status"),
        "next": next_nodes,
        "status": values.get("status"),
    }
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
    runs = _runs(args.thread_id)
    print(
        json.dumps(
            [{"run_id": r.get("run_id"), "status": r.get("status")} for r in runs],
            ensure_ascii=False,
        )
    )


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

    p = sub.add_parser("runs", help="스레드 실행 이력")
    p.add_argument("thread_id")
    p.set_defaults(func=cmd_runs)

    args = parser.parse_args(argv)
    if args.url:
        BASE = args.url.rstrip("/")
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
