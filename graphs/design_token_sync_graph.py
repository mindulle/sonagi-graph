"""Design Token Sync Graph — 정본 토큰을 소비처로 컴파일하고, 사람이 눈으로 보고 승인한다.

```
tokens/*.json
     │
 token_parse ──(error 있음)──────────────────────────────▶ END  status=blocked_by_errors
     │
 transpile      CSS 변수 · Tailwind · Craft 스와치(JSON+SVG) · Figma 배치 계획(드라이런)
     │
 ⏸ hitl_checkpoint   ← interrupt_before. 스와치 SVG를 눈으로 보고 승인한다
     │                승인하면 리포트 JSON(해시·경고·산출물 경로)을 남긴다
 figma_bridge   Desktop Bridge(/health) 확인 + 적용 묶음 생성. 쓰지는 않는다
     │
 ⏸ figma_apply      ← interrupt_before. 브리지가 확인된 경우에만 쓰기 묶음을 내보낸다
```

이 그래프는 **LLM을 쓰지 않는다**. 토큰 컴파일은 결정론적 계산이고, 판단은
사람이 승인 게이트에서 한다. 그래서 노드가 전부 순수 함수 + 파일 쓰기다.

브리지 확인은 `FIGMA_BRIDGE_PORTS`(기본 `9223~9226`)의 `/health`로 한다. CDP
(`/json/version`)가 아니다 — figma-console은 local 모드에서 WebSocket 서버를 띄우고
`connectedClients`로 플러그인 연결을 알린다. 포트가 열려 있어도 `connectedClients`가
0이면 쓰기는 아무 일도 일어나지 않으므로 **검증됨 = /health 200 + connectedClients>0**
이다. 우회 env는 두지 않는다. 실제 쓰기는 MCP(`figma_execute`)를 가진 쪽이
`figma.apply.request`를 옮기고, `figma.readback`으로 다시 읽어 대조한다.

사용::

    from graphs.design_token_sync_graph import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": "tokens-1"}}
    graph.invoke({"out_dir": "/tmp/token_sync"}, config)
    # hitl_checkpoint 앞에서 멈춘다. 스와치 SVG를 보고:
    graph.update_state(config, {"approved": True})
    graph.invoke(None, config)
    state = graph.get_state(config).values
    print(state["report_path"], state["artifact_set_hash"])
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import token_sync as ts  # noqa: E402

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

#: 우회 env는 두지 않는다. 브리지 검증은 /health의 connectedClients만 믿는다.


class DesignTokenSyncState(TypedDict, total=False):
    run_id: str
    tokens_dir: str
    projection: str
    out_dir: str
    bridge_ports: List[int]
    approved: bool
    apply_figma: bool
    summary: Dict[str, Any]
    checks: List[Dict[str, Any]]
    review: Dict[str, Any]
    artifacts: Dict[str, str]
    artifact_set_hash: str
    figma: Dict[str, Any]
    report_path: str
    status: str
    note: str


# ---------------------------------------------------------------- 노드


def token_parse_node(state: DesignTokenSyncState) -> Dict[str, Any]:
    """정본을 읽어 참조를 해석하고 검사 4종을 돌린다. 파일은 쓰지 않는다."""
    root = Path(state["tokens_dir"]) if state.get("tokens_dir") else None
    projection = Path(state["projection"]) if state.get("projection") else None
    analysis, detail = ts.analyze(root, projection)

    checks = [
        {
            "code": code,
            "errors": sum(1 for f in analysis.errors if f.code == code),
            "warnings": sum(1 for f in analysis.warnings if f.code == code),
        }
        for code in sorted({f.code for f in analysis.findings})
    ]
    blocked = bool(analysis.errors)
    return {
        "run_id": state.get("run_id") or "token-sync",
        "summary": analysis.summary(),
        "checks": checks,
        "review": {
            "tokenVersion": analysis.token_version,
            "summary": analysis.summary(),
            "errors": [f.as_dict() for f in analysis.errors][:20],
            "warnings": [f.as_dict() for f in analysis.warnings][:20],
            "truncated": {
                "errors": max(0, len(analysis.errors) - 20),
                "warnings": max(0, len(analysis.warnings) - 20),
            },
            "scales": detail["scales"],
            "projection": detail["projection"],
            "verdict": "blocked" if blocked else "compilable",
        },
        "status": "blocked_by_errors" if blocked else "parsed",
        "note": (
            f"치명 {len(analysis.errors)}건 — 산출물을 만들지 않는다."
            if blocked
            else f"경고 {len(analysis.warnings)}건 — 산출물은 만들되 승인 게이트에서 확인한다."
        ),
    }


def transpile_node(state: DesignTokenSyncState) -> Dict[str, Any]:
    """소비처 산출물을 만든다. Figma는 배치 계획(드라이런)까지만."""
    root = Path(state["tokens_dir"]) if state.get("tokens_dir") else None
    projection = Path(state["projection"]) if state.get("projection") else None
    directory = Path(state["out_dir"]) if state.get("out_dir") else None

    preview = ts.preview_artifacts(root, projection)
    if preview["blocked"]:
        return {
            "status": "blocked_by_errors",
            "note": "치명 결함이 있어 산출물을 쓰지 않는다.",
        }
    target = Path(directory) if directory else ts.out_dir()
    paths = ts.write_artifacts(preview["artifacts"], target)
    report = {
        "artifactSetHash": preview["artifactSetHash"],
        "artifacts": preview["manifest"],
    }
    return {
        "artifacts": paths,
        "artifact_set_hash": report["artifactSetHash"],
        "figma": {"plan": preview["figma"]},
        "review": {
            **state.get("review", {}),
            "artifactSetHash": report["artifactSetHash"],
            "artifacts": [
                {"name": item["name"], "sha256": item["sha256"], "bytes": item["bytes"]}
                for item in report["artifacts"]
            ],
            "preview": {
                "swatchSvg": paths.get(ts.ARTIFACT_SWATCH_SVG),
                "css": paths.get(ts.ARTIFACT_CSS),
            },
            "figmaPlan": {
                "collection": preview["figma"]["collection"],
                "variables": preview["figma"]["counts"]["variables"],
                "incomplete": preview["figma"]["counts"]["incomplete"],
                "dryRun": True,
            },
        },
        "status": "transpiled",
        "note": "산출물 5종 생성. 승인 전에는 Figma에 아무것도 쓰지 않는다.",
    }


def hitl_checkpoint_node(state: DesignTokenSyncState) -> Dict[str, Any]:
    """사람 승인 뒤에 돈다(interrupt_before). 승인 기록을 리포트 JSON으로 남긴다.

    승인은 **산출물 지문에 결속**된다. 일시정지와 재개 사이에 정본이 바뀌면
    사람이 보지 않은 산출물에 도장이 찍히므로, 지문이 다르면 승인을 거부한다.
    """
    if not state.get("approved"):
        return {
            "status": "awaiting_approval",
            "note": "승인되지 않았다. update_state({'approved': True})로 재개한다.",
        }

    root = Path(state["tokens_dir"]) if state.get("tokens_dir") else None
    projection = Path(state["projection"]) if state.get("projection") else None
    directory = Path(state["out_dir"]) if state.get("out_dir") else None

    approved_hash = state.get("artifact_set_hash")
    fresh = ts.preview_artifacts(root, projection)
    if approved_hash and fresh["artifactSetHash"] != approved_hash:
        return {
            "status": "approval_stale",
            "note": (
                "승인 뒤 정본이 바뀌어 산출물 지문이 달라졌다. "
                f"승인 {approved_hash[:12]} ≠ 현재 {fresh['artifactSetHash'][:12]} — 다시 검수해야 한다."
            ),
        }

    result = ts.compile_all(root, projection, directory)
    report = result["report"]
    if state.get("figma", {}).get("plan"):
        report["figma"] = {"plan": state["figma"]["plan"]}
    report["approval"] = {"approved": True, "gate": "hitl_checkpoint"}
    report_path = Path(result["paths"][ts.ARTIFACT_REPORT])
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "report_path": str(report_path),
        "artifact_set_hash": report["artifactSetHash"],
        "artifacts": result["paths"],
        "status": "approved",
        "note": f"리포트 기록: {report_path}",
    }


def figma_bridge_node(state: DesignTokenSyncState) -> Dict[str, Any]:
    """Variables 배치를 준비하고 브리지를 확인한다. 여기서는 쓰지 않는다."""
    root = Path(state["tokens_dir"]) if state.get("tokens_dir") else None
    directory = Path(state["out_dir"]) if state.get("out_dir") else None
    analysis, _ = ts.analyze(
        root, Path(state["projection"]) if state.get("projection") else None
    )
    plan = ts.figma_plan(analysis)

    probe = ts.probe_bridge(state.get("bridge_ports"))
    verified = probe["verified"]

    script_text = ts.transpile_figma_script(analysis, plan)
    target = Path(directory or ts.out_dir()) / ts.ARTIFACT_FIGMA_JS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script_text, encoding="utf-8")

    package = ts.figma_apply_package(plan)
    request_path = target.parent / "figma-apply-request.json"
    request_path.write_text(
        json.dumps(package["request"], indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    fresh_hash = ts.artifact_set_hash(
        ts.artifact_manifest(
            {**ts.transpile(analysis), ts.ARTIFACT_FIGMA_JS: script_text}
        )
    )
    figma = {
        "plan": plan,
        "alignedWithApproval": (not state.get("artifact_set_hash"))
        or fresh_hash == state.get("artifact_set_hash"),
        "bridge": {
            "verified": verified,
            "connectedClients": probe.get("connectedClients", 0),
            "evidence": probe["evidence"],
            "probes": probe["probes"],
        },
        "request": package["request"],
        "requestPath": str(request_path),
        "executeScript": package["executeScript"],
        "readbackScript": package["readbackScript"],
        "script": str(target),
        "scriptSha256": ts.sha256_text(script_text),
    }
    return {
        "figma": figma,
        "status": "figma_planned" if verified else "figma_bridge_unverified",
        "note": (
            "브리지 확인됨 — 승인하면 쓰기 묶음을 내보낸다."
            if verified
            else "브리지 미확인(/health connectedClients=0 또는 무응답) — 계획만 남긴다."
        ),
    }


def figma_apply_node(state: DesignTokenSyncState) -> Dict[str, Any]:
    """쓰기 게이트. 브리지가 확인되지 않으면 아무것도 내보내지 않는다.

    LangGraph 프로세스에는 MCP 클라이언트가 없으므로 여기서 Variables를 직접 쓰지
    않는다. 승인·브리지·요청이 모두 맞으면 `figma.apply`에 실행 순서와 스크립트를
    담아 내보내고, MCP 쪽이 실행한 뒤 `figma_readback`으로 대조한다.
    """
    figma = state.get("figma", {})
    bridge = figma.get("bridge", {})
    verified = bool(bridge.get("verified"))
    approved = bool(state.get("approved"))
    requested = bool(state.get("apply_figma"))
    ready = verified and approved and requested

    reason = (
        "브리지 미확인 — 쓰기 없음."
        if not verified
        else "승인되지 않음"
        if not approved
        else "호출자가 쓰기를 요청하지 않음"
        if not requested
        else "쓰기 묶음 내보냄 — MCP 실행 후 재읽기 대조 필요"
    )
    apply_block = {
        "attempted": ready,
        "verified": verified,
        "approved": approved,
        "requested": requested,
        "reason": reason,
        "sequence": (["readback(pre)", "execute", "readback(post)"] if ready else []),
        "collection": (figma.get("request") or {}).get("collection"),
        "variables": (figma.get("request") or {}).get("counts", {}).get("variables"),
        "request": figma.get("request") if ready else None,
        "executeScript": figma.get("executeScript") if ready else None,
        "readbackScript": figma.get("readbackScript") if ready else None,
    }

    # 리포트에는 큰 스크립트 본문 대신 해시·경로만 남긴다(리포트가 수 MB가 되지 않게).
    report_apply = {
        key: value
        for key, value in apply_block.items()
        if key not in {"request", "executeScript", "readbackScript"}
    }
    report_apply["requestPath"] = figma.get("requestPath")
    report_apply["executeSha256"] = ts.sha256_text(figma.get("executeScript") or "")

    # 리포트에 시도 결과를 남긴다(쓰지 않았더라도 근거를 남긴다)
    report_path = state.get("report_path")
    if report_path and Path(report_path).is_file():
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        current = report.get("figma") or {}
        current.update(
            {
                key: value
                for key, value in figma.items()
                if key not in {"executeScript", "readbackScript", "request"}
            }
        )
        current["apply"] = report_apply
        report["figma"] = current
        Path(report_path).write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    if not verified:
        return {
            "status": "figma_apply_skipped",
            "note": "브리지 미확인 — 쓰기 없음. 계획·요청만 남긴다.",
            "figma": {**figma, "apply": apply_block},
        }
    if not (approved and requested):
        return {
            "status": "figma_apply_skipped",
            "note": "쓰기 없음. 승인과 apply_figma 요청이 모두 있어야 실행 묶음이 나간다.",
            "figma": {**figma, "apply": apply_block},
        }
    return {
        "status": "figma_apply_ready",
        "note": (
            "쓰기 묶음을 내보냈다. MCP figma_execute로 executeScript를 옮긴 뒤 "
            "readback 결과를 verify_figma_readback()으로 대조해야 '반영됨'이다."
        ),
        "figma": {**figma, "apply": apply_block},
    }


def route_after_parse(state: DesignTokenSyncState) -> str:
    return "transpile" if state.get("status") == "parsed" else END


def route_after_hitl(state: DesignTokenSyncState) -> str:
    """승인·신선도 검사를 통과한 경우에만 Figma 단계로 간다."""
    return "figma_bridge" if state.get("status") == "approved" else END


# ---------------------------------------------------------------- 그래프


def build_graph(checkpointer: Optional[Any] = None):
    """컴파일된 그래프. 승인 게이트 두 곳에서 멈춘다."""
    builder = StateGraph(DesignTokenSyncState)
    builder.add_node("token_parse", token_parse_node)
    builder.add_node("transpile", transpile_node)
    builder.add_node("hitl_checkpoint", hitl_checkpoint_node)
    builder.add_node("figma_bridge", figma_bridge_node)
    builder.add_node("figma_apply", figma_apply_node)

    builder.add_edge(START, "token_parse")
    builder.add_conditional_edges(
        "token_parse", route_after_parse, {"transpile": "transpile", END: END}
    )
    builder.add_edge("transpile", "hitl_checkpoint")
    builder.add_conditional_edges(
        "hitl_checkpoint", route_after_hitl, {"figma_bridge": "figma_bridge", END: END}
    )
    builder.add_edge("figma_bridge", "figma_apply")
    builder.add_edge("figma_apply", END)

    return builder.compile(
        checkpointer=checkpointer or MemorySaver(),
        interrupt_before=["hitl_checkpoint", "figma_apply"],
    )


graph = build_graph()


if __name__ == "__main__":  # pragma: no cover - 수동 실행
    config = {
        "configurable": {
            "thread_id": os.environ.get("TOKEN_SYNC_THREAD", "token-sync-0")
        }
    }
    state = graph.invoke({}, config)
    print("PAUSED at", graph.get_state(config).next)
    print(json.dumps(state.get("review", {}).get("summary", {}), ensure_ascii=False))
    print(
        json.dumps(
            state.get("review", {}).get("checks", state.get("checks")),
            ensure_ascii=False,
        )
    )
    graph.update_state(config, {"approved": True})
    state = graph.invoke(None, config)
    print("PAUSED at", graph.get_state(config).next)
    print("REPORT", state.get("report_path"))
    print("HASH", state.get("artifact_set_hash"))
    print(
        "FIGMA",
        json.dumps(state.get("figma", {}).get("bridge", {}), ensure_ascii=False),
    )
    state = graph.invoke(None, config)
    print("FINAL", state.get("status"), "|", state.get("note"))
