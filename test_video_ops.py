"""VideoOpsGraph 단위 테스트 — 크래프트 CLI를 실제로 부르지 않는다.

실제 렌더 검증은 `scripts/verify_video_ops.py`가 맡는다(CLI·CDN 권한이 있는 호스트에서만 돈다).
여기서는 노드 배선·HITL 게이트·CDN 배포 URL 규격만 결정론적으로 확인한다.
"""

import subprocess

import pytest

from graphs import video_ops_graph as mod
from langgraph.checkpoint.memory import MemorySaver


def _out_target(cmd):
    """명령에서 결과 파일 경로를 뽑는다 (`--out X` 또는 `export X`)."""
    if "--out" in cmd:
        return cmd[cmd.index("--out") + 1]
    if "--save-as" in cmd:
        return cmd[cmd.index("--save-as") + 1]
    if "export" in cmd:
        return cmd[cmd.index("export") + 1]
    return None


@pytest.fixture
def env(monkeypatch, tmp_path):
    """모든 경로를 tmp로 돌리고, CLI 호출을 가짜 결과 파일 생성으로 대체한다."""
    out_root = tmp_path / "out"
    cdn_dir = tmp_path / "cdn"
    calls = []

    def fake_run(cmd, check=True, capture_output=True, **kwargs):
        calls.append(cmd)
        target = _out_target(cmd)
        if target:
            path = mod.Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00" * 4096)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(mod, "OUTPUT_TEMP_ROOT", out_root)
    monkeypatch.setattr(mod, "CDN_VIDEO_DIR", cdn_dir)
    monkeypatch.setattr(mod, "CDN_PUBLIC_BASE", "https://cdn.example/video")
    monkeypatch.setattr(
        mod, "subprocess", type("S", (), {"run": staticmethod(fake_run)})
    )
    return {"calls": calls, "out_root": out_root, "cdn_dir": cdn_dir}


def test_brief_node_builds_four_scenes():
    result = mod.brief_node(
        {"brief": "Sonagi: Showreel", "aspect_ratio": "16:9", "duration_seconds": 15.0}
    )

    assert len(result["scenes"]) == 4
    assert result["scenes"][0]["start"] == 0.0
    assert result["scenes"][-1]["end"] == 15.0
    assert result["status"] == "brief_designed"


def test_graph_pauses_before_export(env):
    graph = mod.build_video_ops_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "unit-pause"}}

    list(graph.stream({"run_id": "r1", "brief": "Demo: Unit"}, config=config))

    state = graph.get_state(config)
    assert state.next == ("export_and_publish",)
    assert mod.Path(state.values["motion_asset_path"]).stat().st_size > 0
    assert mod.Path(state.values["preview_frame_path"]).stat().st_size > 0
    assert state.values["status"] == "awaiting_approval"

    # HITL 전에는 인코딩 명령이 나가지 않는다
    assert not any("export" in cmd for cmd in env["calls"])


def test_resume_after_approval_publishes_to_cdn(env):
    graph = mod.build_video_ops_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "unit-approve"}}

    list(graph.stream({"run_id": "r2", "brief": "Demo: Unit"}, config=config))
    graph.update_state(config, {"approval_status": "approved"})
    list(graph.stream(None, config=config))

    state = graph.get_state(config).values
    assert state["status"] == "completed"
    assert state["published_url"] == "https://cdn.example/video/showreel_r2.mp4"

    published = env["cdn_dir"] / "showreel_r2.mp4"
    assert published.exists()
    assert published.stat().st_size > 0


def test_hitl_without_approval_stays_pending():
    result = mod.hitl_checkpoint_node({"approval_status": "pending"})

    assert result["status"] == "awaiting_approval"
    assert result["approval_status"] == "pending"
