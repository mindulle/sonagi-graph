"""design_token_sync 그래프 검증 — L2(fixture E2E) + L3(결정론).

토큰 정본은 `token_fixture`가 임시로 만들고, Figma 브리지 확인은 `patch_bridge`가
대신한다. 네트워크·Figma 앱·MDS 실저장소 없이 돈다. 그래프 파일은 수정하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import token_sync as ts
from graphs import design_token_sync_graph as dt

ARTIFACTS = {
    ts.ARTIFACT_CSS,
    ts.ARTIFACT_TAILWIND,
    ts.ARTIFACT_SWATCH_JSON,
    ts.ARTIFACT_SWATCH_SVG,
    ts.ARTIFACT_FIGMA_JS,
}


def _input(root: Path, tmp_path: Path, **extra):
    return {
        "tokens_dir": str(root),
        "projection": str(tmp_path / "absent-projection.json"),
        "out_dir": str(tmp_path / "out"),
        "bridge_ports": [9223],
        **extra,
    }


def _checkpoint_graph():
    from langgraph.checkpoint.memory import MemorySaver

    return dt.build_graph(checkpointer=MemorySaver())


def test_token_parse_contract_is_report_only(token_fixture, tmp_path):
    """token_parse는 검사 결과만 돌려주고 파일을 쓰지 않는다."""
    root = token_fixture(tmp_path / "canon")
    state = dt.token_parse_node(_input(root, tmp_path, run_id="parse-1"))

    assert state["status"] == "parsed"
    assert state["run_id"] == "parse-1"
    assert state["review"]["verdict"] == "compilable"
    assert state["summary"]["errors"] == 0
    codes = {check["code"] for check in state["checks"]}
    assert "projection_missing" in codes  # 경고는 실패가 아니다
    assert all(
        isinstance(check["errors"], int) and isinstance(check["warnings"], int)
        for check in state["checks"]
    )
    assert not (tmp_path / "out").exists()


def test_raw_hex_blocks_compilation_and_stops_the_route(token_fixture, tmp_path):
    """치명 결함(semantic 원시 hex)은 산출물을 만들지 않고 라우팅도 END다."""
    root = token_fixture(tmp_path / "broken", raw_hex=True)
    state = dt.token_parse_node(_input(root, tmp_path))

    assert state["status"] == "blocked_by_errors"
    assert state["review"]["verdict"] == "blocked"
    assert "raw_hex_in_semantic" in {f["code"] for f in state["review"]["errors"]}
    assert dt.route_after_parse(state) == dt.END

    transpiled = dt.transpile_node(_input(root, tmp_path))
    assert transpiled["status"] == "blocked_by_errors"
    assert not (tmp_path / "out").exists()


def test_hitl_node_refuses_unapproved_state(token_fixture, tmp_path):
    """승인 없이 hitl_checkpoint가 불리면 리포트를 쓰지 않는다."""
    root = token_fixture(tmp_path / "canon")
    state = dt.hitl_checkpoint_node(_input(root, tmp_path))

    assert state["status"] == "awaiting_approval"
    assert "approved" not in state
    assert dt.route_after_hitl(state) == dt.END
    assert not (tmp_path / "out" / ts.ARTIFACT_REPORT).exists()


def test_graph_pauses_twice_and_emits_apply_package_when_bridge_verified(
    token_fixture, tmp_path, patch_bridge, no_network
):
    """L2: transpile → 승인 → (브리지 확인됨) → 쓰기 묶음 준비까지."""
    probed = patch_bridge(verified=True, clients=2)
    root = token_fixture(tmp_path / "canon")
    graph = _checkpoint_graph()
    config = {"configurable": {"thread_id": "tokens-verified"}}

    state = graph.invoke(_input(root, tmp_path), config)
    assert graph.get_state(config).next == ("hitl_checkpoint",)
    assert state["status"] == "transpiled"
    assert set(state["artifacts"]) == ARTIFACTS
    for name, path in state["artifacts"].items():
        assert Path(path).is_file(), f"{name} 산출물이 없다"
    assert state["review"]["figmaPlan"]["dryRun"] is True
    assert Path(state["review"]["preview"]["swatchSvg"]).is_file()

    graph.update_state(config, {"approved": True})
    graph.invoke(None, config)
    assert graph.get_state(config).next == ("figma_apply",)
    state = graph.get_state(config).values
    assert state["status"] == "figma_planned"
    assert probed["ports"] == [9223]  # state.bridge_ports가 브리지에 전달됐다
    assert state["figma"]["bridge"]["verified"] is True
    assert state["figma"]["bridge"]["connectedClients"] == 2
    assert state["figma"]["alignedWithApproval"] is True
    assert Path(state["figma"]["requestPath"]).is_file()
    assert Path(state["figma"]["script"]).is_file()

    graph.update_state(config, {"apply_figma": True})
    graph.invoke(None, config)
    state = graph.get_state(config).values
    assert graph.get_state(config).next == ()
    assert state["status"] == "figma_apply_ready"
    apply = state["figma"]["apply"]
    assert apply["attempted"] is True
    assert apply["sequence"] == ["readback(pre)", "execute", "readback(post)"]

    report = json.loads(Path(state["report_path"]).read_text(encoding="utf-8"))
    assert report["approval"] == {"approved": True, "gate": "hitl_checkpoint"}
    assert report["artifactSetHash"] == state["artifact_set_hash"]
    assert report["figma"]["apply"]["attempted"] is True
    # 리포트에는 스크립트 본문·요청 전문을 넣지 않는다(해시·경로만).
    assert "executeScript" not in report["figma"]["apply"]
    assert report["figma"]["apply"]["executeSha256"] == ts.sha256_text(
        state["figma"]["executeScript"]
    )


def test_apply_is_skipped_without_explicit_request(
    token_fixture, tmp_path, patch_bridge
):
    """브리지가 확인돼도 `apply_figma` 요청이 없으면 쓰지 않는다."""
    patch_bridge(verified=True, clients=1)
    root = token_fixture(tmp_path / "canon")
    graph = _checkpoint_graph()
    config = {"configurable": {"thread_id": "tokens-no-request"}}

    graph.invoke(_input(root, tmp_path), config)
    graph.update_state(config, {"approved": True})
    graph.invoke(None, config)
    assert graph.get_state(config).next == ("figma_apply",)

    graph.invoke(None, config)
    state = graph.get_state(config).values
    assert state["status"] == "figma_apply_skipped"
    assert state["figma"]["apply"]["attempted"] is False
    assert state["figma"]["apply"]["reason"] == "호출자가 쓰기를 요청하지 않음"
    assert state["figma"]["apply"]["sequence"] == []


def test_apply_is_skipped_when_bridge_unverified(token_fixture, tmp_path, patch_bridge):
    """브리지 미확인(플러그인 미연결)이면 승인·요청이 있어도 쓰기 없음."""
    patch_bridge(verified=False, clients=0)
    root = token_fixture(tmp_path / "canon")
    graph = _checkpoint_graph()
    config = {"configurable": {"thread_id": "tokens-unverified"}}

    graph.invoke(_input(root, tmp_path), config)
    graph.update_state(config, {"approved": True, "apply_figma": True})
    graph.invoke(None, config)
    assert graph.get_state(config).next == ("figma_apply",)
    state = graph.get_state(config).values
    assert state["status"] == "figma_bridge_unverified"

    graph.invoke(None, config)
    state = graph.get_state(config).values
    assert state["status"] == "figma_apply_skipped"
    assert state["figma"]["apply"]["attempted"] is False
    assert "브리지 미확인" in state["figma"]["apply"]["reason"]


def test_apply_node_rejects_unapproved_write_even_with_request():
    """노드 단위: 브리지·요청이 모두 있어도 승인이 없으면 쓰기 묶음을 내보내지 않는다."""
    state = dt.figma_apply_node(
        {
            "figma": {
                "bridge": {"verified": True},
                "request": {"collection": "MDS", "counts": {"variables": 4}},
            },
            "approved": False,
            "apply_figma": True,
        }
    )

    assert state["status"] == "figma_apply_skipped"
    apply = state["figma"]["apply"]
    assert apply["attempted"] is False
    assert apply["reason"] == "승인되지 않음"
    assert apply["request"] is None
    assert apply["sequence"] == []


def test_approval_goes_stale_when_canon_changes_between_pause_and_resume(
    token_fixture, tmp_path, patch_bridge
):
    """일시정지와 재개 사이에 정본이 바뀌면 승인을 거부한다(지문 결속)."""
    patch_bridge(verified=False, clients=0)
    root = token_fixture(tmp_path / "canon")
    graph = _checkpoint_graph()
    config = {"configurable": {"thread_id": "tokens-stale"}}

    first = graph.invoke(_input(root, tmp_path), config)
    approved_hash = first["artifact_set_hash"]

    primitives = root / "tokens/primitives.json"
    doc = json.loads(primitives.read_text(encoding="utf-8"))
    doc["primitive"]["brand"]["green"] = {"$value": "#00ff00", "$type": "color"}
    primitives.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    graph.update_state(config, {"approved": True})
    state = graph.invoke(None, config)

    assert state["status"] == "approval_stale"
    assert graph.get_state(config).next == ()
    assert state["artifact_set_hash"] == approved_hash
    assert "report_path" not in state
    assert not (tmp_path / "out" / ts.ARTIFACT_REPORT).exists()


def test_l3_same_canon_twice_yields_identical_artifacts(
    token_fixture, tmp_path, patch_bridge
):
    """L3: 같은 정본 2회 → artifact_set_hash와 산출물 바이트가 동일하다."""
    patch_bridge(verified=False, clients=0)
    root = token_fixture(tmp_path / "canon")

    def run(thread: str, out: Path):
        graph = _checkpoint_graph()
        config = {"configurable": {"thread_id": thread}}
        state = graph.invoke(_input(root, tmp_path, out_dir=str(out)), config)
        return state

    first = run("l3-a", tmp_path / "out-a")
    second = run("l3-b", tmp_path / "out-b")

    assert first["artifact_set_hash"] == second["artifact_set_hash"]
    assert set(first["artifacts"]) == set(second["artifacts"])
    for name in sorted(first["artifacts"]):
        assert Path(first["artifacts"][name]).read_text(encoding="utf-8") == Path(
            second["artifacts"][name]
        ).read_text(encoding="utf-8"), name
    # 경고 목록도 같은 순서로 나온다(정렬된 findings).
    assert first["review"]["warnings"] == second["review"]["warnings"]
