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
 figma_bridge   CDP 브리지(/json/version) 확인 + 콘솔 스크립트 생성. 쓰지는 않는다
     │
 ⏸ figma_apply      ← interrupt_before. 브리지가 확인된 경우에만 Variables를 쓴다
```

이 그래프는 **LLM을 쓰지 않는다**. 토큰 컴파일은 결정론적 계산이고, 판단은
사람이 승인 게이트에서 한다. 그래서 노드가 전부 순수 함수 + 파일 쓰기다.

브리지 확인은 `FIGMA_CDP_PORTS`(기본 `9223,9224,9225`)로 한다. 지금 이 호스트는
`/json/version`이 404라 **미확인**이고, 그래서 `figma_apply`는 계획만 남기고
아무것도 쓰지 않는다. 확인된 환경에서는 `FIGMA_BRIDGE_VERIFIED=1`로 강제할 수
있지만, 실제 쓰기 경로는 아직 검증되지 않았다(PR 본문 참고).

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

#: 미확인 브리지에서도 계획만 확인하고 싶을 때. 쓰기 강제가 아니다.
FORCE_VERIFIED_ENV = "FIGMA_BRIDGE_VERIFIED"


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
    forced = os.environ.get(FORCE_VERIFIED_ENV, "").lower() in {"1", "true", "yes"}
    verified = probe["verified"] or forced

    script_text = ts.transpile_figma_script(analysis, plan)
    target = Path(directory or ts.out_dir()) / ts.ARTIFACT_FIGMA_JS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script_text, encoding="utf-8")

    fresh_hash = ts.artifact_set_hash(
        ts.artifact_manifest({**ts.transpile(analysis), ts.ARTIFACT_FIGMA_JS: script_text})
    )
    figma = {
        "plan": plan,
        "alignedWithApproval": (not state.get("artifact_set_hash"))
        or fresh_hash == state.get("artifact_set_hash"),
        "bridge": {
            "verified": verified,
            "forced": forced,
            "evidence": probe["evidence"],
            "probes": probe["probes"],
        },
        "script": str(target),
        "scriptSha256": ts.sha256_text(script_text),
    }
    return {
        "figma": figma,
        "status": "figma_planned" if verified else "figma_bridge_unverified",
        "note": (
            "브리지 확인됨 — 승인하면 쓸 수 있다."
            if verified
            else "브리지 미확인(/json/version 실패) — 콘솔 스크립트만 남긴다."
        ),
    }


def figma_apply_node(state: DesignTokenSyncState) -> Dict[str, Any]:
    """실제 쓰기 게이트. 브리지가 확인되지 않으면 아무것도 하지 않는다."""
    bridge = state.get("figma", {}).get("bridge", {})
    verified = bool(bridge.get("verified"))
    approved = bool(state.get("approved"))
    requested = bool(state.get("apply_figma"))

    # 리포트에 시도 결과를 남긴다(쓰지 않았더라도 근거를 남긴다)
    report_path = state.get("report_path")
    if report_path and Path(report_path).is_file():
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        report["figma"] = {key: value for key, value in state.get("figma", {}).items()}
        report["figma"]["apply"] = {
            "attempted": requested and verified and approved,
            "verified": verified,
            "approved": approved,
            "requested": requested,
            "reason": (
                "브리지 미확인 — Variables 쓰기 경로를 실행하지 않았다."
                if not verified
                else (
                    "승인되지 않음"
                    if not approved
                    else ("호출자가 쓰기를 요청하지 않음" if not requested else "실행")
                )
            ),
        }
        Path(report_path).write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    if not verified:
        return {
            "status": "figma_apply_skipped",
            "note": "브리지 미확인 — 쓰기 없음. 콘솔 스크립트를 Figma에서 직접 실행해야 한다.",
        }
    if not (approved and requested):
        return {
            "status": "figma_apply_skipped",
            "note": "쓰기 없음. 스크립트를 Figma 콘솔에서 DRY_RUN=false로 실행해야 반영된다.",
        }
    return {
        "status": "figma_apply_blocked",
        "note": (
            "브리지가 확인됐지만 CDP 쓰기 어댑터는 아직 검증되지 않아 실행하지 않는다. "
            "콘솔 스크립트를 쓴다."
        ),
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
