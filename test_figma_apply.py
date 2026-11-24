"""GRAPH-7 — Figma Variables 쓰기 어댑터.

쓰기 자체는 MCP(`figma_execute`)가 한다. 여기서 고정하는 것은 그 앞뒤의 순수 계산이다:
계획 → 쓰기 요청(hex·모드 라벨) → 적용 스크립트, 그리고 재읽기 대조.

실제 Figma 없이도 판정 규칙(모드 누락·동명 충돌·값 드리프트·잔여 변수)을 검증한다.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import token_sync as ts  # noqa: E402
from test_token_sync import fixture_root  # noqa: E402


def plan_for(tmp_path: Path):
    root = fixture_root(tmp_path)
    analysis, _ = ts.analyze(root, tmp_path / "nope.json")
    return ts.figma_plan(analysis)


def snapshot_from(request, **overrides):
    """요청을 그대로 반영한 스냅샷. Figma가 돌려줄 모양이다."""
    snap = {
        "collection": request["collection"],
        "exists": True,
        "modes": list(request["modes"]),
        "variables": [
            {
                "name": item["name"],
                "type": item["type"],
                "modes": dict(item["modes"]),
            }
            for item in request["variables"]
        ],
    }
    snap.update(overrides)
    return snap


# ---------------------------------------------------------------- 요청 만들기


def test_write_request_uses_figma_mode_labels_and_hex(tmp_path):
    request = ts.figma_write_request(plan_for(tmp_path))

    assert request["schema"] == ts.FIGMA_WRITE_SCHEMA
    assert request["modes"] == ["Light", "Dark"]
    assert request["applyMode"] == "merge"
    brand = next(v for v in request["variables"] if v["name"] == "brand/primary")
    assert brand["modes"] == {"Light": "#0055ff", "Dark": "#ffcc00"}
    assert request["counts"]["incomplete"] == 0


def test_write_request_keeps_incomplete_modes_visible(tmp_path):
    plan = plan_for(tmp_path)
    plan["variables"][0]["modes"].pop("dark")
    request = ts.figma_write_request(plan)

    assert plan["variables"][0]["name"] in request["incomplete"]
    assert request["counts"]["incomplete"] == 1


def test_execute_script_is_deterministic_and_idempotent(tmp_path):
    request = ts.figma_write_request(plan_for(tmp_path))
    first = ts.figma_execute_script(request)
    second = ts.figma_execute_script(request)

    assert first == second
    # 멱등: 만들기 전에 같은 이름을 찾고, 있으면 값만 갱신한다
    assert "figma.variables.createVariable" in first
    assert "setValueForMode" in first
    assert "conflicts.push" in first
    assert "DRY_RUN = false" in first


def test_snapshot_script_is_read_only(tmp_path):
    """스냅샷은 파일 전체를 id로 담는다(별칭 대상을 찾아야 하므로)."""
    script = ts.figma_snapshot_script()

    assert "createVariable" not in script
    assert "setValueForMode" not in script
    assert "sonagi.figma.snapshot/1" in script
    assert "getLocalVariableCollectionsAsync" in script


def test_apply_package_orders_snapshot_write_snapshot(tmp_path):
    package = ts.figma_apply_package(plan_for(tmp_path))

    assert package["sequence"] == [
        "readbackScript(쓰기 전)",
        "executeScript",
        "readbackScript(쓰기 후)",
    ]
    assert package["readbackScript"] in (package["readbackScript"],)
    assert "createVariable" not in package["readbackScript"]


# ---------------------------------------------------------------- 재읽기 대조


def test_verify_passes_when_readback_matches(tmp_path):
    request = ts.figma_write_request(plan_for(tmp_path))
    result = ts.verify_figma_readback(request, snapshot_from(request))

    assert result["ok"] is True
    assert result["matched"] == result["expected"]
    assert result["missing"] == []
    assert result["drift"] == []


def test_verify_reports_missing_variable(tmp_path):
    request = ts.figma_write_request(plan_for(tmp_path))
    snap = snapshot_from(request)
    dropped = snap["variables"].pop(0)["name"]

    result = ts.verify_figma_readback(request, snap)

    assert result["ok"] is False
    assert result["missing"] == [dropped]


def test_verify_reports_missing_mode(tmp_path):
    """모드가 통째로 빠지면 값이 같아도 통과시키지 않는다."""
    request = ts.figma_write_request(plan_for(tmp_path))
    snap = snapshot_from(request, modes=["Light"])
    for item in snap["variables"]:
        item["modes"].pop("Dark", None)

    result = ts.verify_figma_readback(request, snap)

    assert result["ok"] is False
    assert result["modeMissing"] == ["Dark"]
    assert {d["mode"] for d in result["drift"]} == {"Dark"}


def test_verify_reports_value_drift(tmp_path):
    request = ts.figma_write_request(plan_for(tmp_path))
    snap = snapshot_from(request)
    snap["variables"][0]["modes"]["Light"] = "#123456"

    result = ts.verify_figma_readback(request, snap)

    assert result["ok"] is False
    assert result["drift"] == [
        {
            "name": snap["variables"][0]["name"],
            "mode": "Light",
            "expected": request["variables"][0]["modes"]["Light"],
            "actual": "#123456",
        }
    ]


def test_verify_reports_extra_variable(tmp_path):
    """정본에서 사라진 변수가 Figma에 남아 있으면 지울 대상을 알려준다."""
    request = ts.figma_write_request(plan_for(tmp_path))
    snap = snapshot_from(request)
    snap["variables"].append(
        {"name": "legacy/gone", "type": "COLOR", "modes": {"Light": "#000000"}}
    )

    result = ts.verify_figma_readback(request, snap)

    assert result["ok"] is False
    assert result["extra"] == ["legacy/gone"]


def test_verify_reports_alias_value_as_drift(tmp_path):
    """별칭(VARIABLE_ALIAS)이 남아 있으면 hex가 아니므로 값 불일치로 본다."""
    request = ts.figma_write_request(plan_for(tmp_path))
    snap = snapshot_from(request)
    snap["variables"][0]["modes"]["Dark"] = {"alias": True}

    result = ts.verify_figma_readback(request, snap)

    assert result["ok"] is False
    assert result["drift"][0]["actual"] == {"alias": True}


def test_verify_reports_missing_collection(tmp_path):
    request = ts.figma_write_request(plan_for(tmp_path))

    result = ts.verify_figma_readback(
        request, {"collection": request["collection"], "exists": False}
    )

    assert result["ok"] is False
    assert result["reason"] == "collection_missing"
    assert len(result["missing"]) == request["counts"]["variables"]


def test_verify_accepts_case_insensitive_hex(tmp_path):
    request = ts.figma_write_request(plan_for(tmp_path))
    snap = snapshot_from(request)
    snap["variables"][0]["modes"]["Light"] = snap["variables"][0]["modes"][
        "Light"
    ].upper()

    assert ts.verify_figma_readback(request, snap)["ok"] is True


# ---------------------------------------------------------------- 그래프 연결


def test_graph_emits_apply_package_only_when_bridge_verified(tmp_path, monkeypatch):
    """브리지가 확인되면 승인 게이트 뒤에 쓰기 묶음이 나가고, 아니면 계획만 남는다."""
    pytest.importorskip("langgraph")
    from graphs.design_token_sync_graph import build_graph

    monkeypatch.setattr(
        ts,
        "probe_bridge",
        lambda ports=None, timeout=0.4: {
            "verified": True,
            "connectedClients": 1,
            "evidence": "GET /health 200 + connectedClients>0",
            "probes": [],
            "checkedAt": None,
        },
    )
    root = fixture_root(tmp_path)
    graph = build_graph()
    config = {"configurable": {"thread_id": f"g7-{tmp_path.name}"}}

    graph.invoke(
        {
            "tokens_dir": str(root),
            "projection": str(tmp_path / "nope.json"),
            "out_dir": str(tmp_path / "out"),
            "bridge_ports": [9224],
        },
        config,
    )
    graph.update_state(config, {"approved": True, "apply_figma": True})
    graph.invoke(None, config)
    state = graph.get_state(config).values
    assert state["status"] == "figma_planned"
    assert state["figma"]["bridge"]["verified"] is True

    graph.invoke(None, config)
    state = graph.get_state(config).values
    assert state["status"] == "figma_apply_ready"
    apply_block = state["figma"]["apply"]
    assert apply_block["attempted"] is True
    assert apply_block["variables"] == state["figma"]["request"]["counts"]["variables"]
    assert "createVariable" in apply_block["executeScript"]
    assert "createVariable" not in apply_block["readbackScript"]

    report = json.loads(Path(state["report_path"]).read_text(encoding="utf-8"))
    stored = report["figma"]["apply"]
    assert stored["attempted"] is True
    assert "executeScript" not in stored  # 리포트에는 해시만
    assert len(stored["executeSha256"]) == 64
    assert Path(state["figma"]["requestPath"]).is_file()


def test_graph_skips_apply_without_apply_figma_flag(tmp_path):
    """승인만으로는 쓰지 않는다. 호출자가 apply_figma를 켜야 한다."""
    pytest.importorskip("langgraph")
    from graphs.design_token_sync_graph import build_graph

    root = fixture_root(tmp_path)
    graph = build_graph()
    config = {"configurable": {"thread_id": f"g7-skip-{tmp_path.name}"}}

    graph.invoke(
        {
            "tokens_dir": str(root),
            "projection": str(tmp_path / "nope.json"),
            "out_dir": str(tmp_path / "out"),
            "bridge_ports": [],
        },
        config,
    )
    graph.update_state(config, {"approved": True})
    graph.invoke(None, config)
    graph.invoke(None, config)
    state = graph.get_state(config).values

    assert state["status"] == "figma_apply_skipped"
    assert state["figma"]["apply"]["attempted"] is False


# ---------------------------------------------------------------- 실제 정본


@pytest.mark.skipif(
    not (Path(ts.tokens_dir()) / "tokens" / "semantics.json").is_file(),
    reason="MDS 토큰 정본이 없는 환경",
)
def test_real_mds_plan_round_trips():
    """정본 29개 변수(301 leaf에서 색상만)가 hex 왕복에서 손실되지 않는다."""
    analysis, _ = ts.analyze(None, None)
    request = ts.figma_write_request(ts.figma_plan(analysis))

    assert request["counts"]["variables"] > 0
    assert request["counts"]["incomplete"] == 0
    assert ts.verify_figma_readback(request, snapshot_from(request))["ok"] is True
