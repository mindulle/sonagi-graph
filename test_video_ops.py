"""VideoOpsGraph 단위 테스트 — 크래프트 CLI를 실제로 부르지 않는다.

실제 렌더 검증(CLI·CDN 권한이 있는 호스트)은 그래프의 `verify()`가 맡는다.
여기서는 노드 배선·HITL 게이트·CDN 배포 URL 규격·실패 전파를 결정론적으로 확인한다.
"""

from pathlib import Path

import pytest

from graphs import video_ops_graph as mod
from graphs.core import artifacts as core_artifacts
from graphs.core import exec as core_exec
from langgraph.checkpoint.memory import MemorySaver


def _out_target(cmd):
    """명령에서 결과 파일 경로를 뽑는다 (`--out X`, `--save-as X` 또는 `export X`)."""
    if "--out" in cmd:
        return cmd[cmd.index("--out") + 1]
    if "--save-as" in cmd:
        return cmd[cmd.index("--save-as") + 1]
    if "export" in cmd:
        return cmd[cmd.index("export") + 1]
    return None


@pytest.fixture
def env(monkeypatch, tmp_path):
    """모든 경로를 tmp로 돌리고, core.exec.run을 가짜 결과 파일 생성으로 대체한다."""
    out_root = tmp_path / "out"
    cdn_dir = tmp_path / "cdn"
    calls = []

    def fake_run(argv, *, timeout=None, check=True, **kwargs):
        calls.append({"argv": list(argv), "timeout": timeout})
        target = _out_target(argv)
        if target:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00" * 4096)
        return core_exec.RunResult(
            argv=tuple(str(arg) for arg in argv),
            returncode=0,
            stdout="",
            stderr="",
            duration_ms=1,
            timeout=timeout,
        )

    monkeypatch.setattr(mod, "OUTPUT_TEMP_ROOT", out_root)
    monkeypatch.setattr(mod, "CDN_VIDEO_DIR", cdn_dir)
    monkeypatch.setattr(mod, "CDN_PUBLIC_BASE", "https://cdn.example/video")
    monkeypatch.setattr(mod.core_exec, "run", fake_run)
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
    assert Path(state.values["motion_asset_path"]).stat().st_size > 0
    assert Path(state.values["preview_frame_path"]).stat().st_size > 0
    assert state.values["status"] == "awaiting_approval"

    # HITL 전에는 인코딩 명령이 나가지 않는다
    assert not any("export" in call["argv"] for call in env["calls"])

    # 모든 명령은 명시적 타임아웃으로 실행된다
    assert env["calls"]
    assert all(call["timeout"] for call in env["calls"])

    # 게이트 전에도 manifest가 남는다
    manifest_path = Path(state.values["manifest_path"])
    assert manifest_path.exists()


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

    manifest = core_artifacts.read_manifest(state["manifest_path"])
    assert manifest["kind"] == "video_ops"
    assert manifest["status"] == "ok"
    assert manifest["meta"]["published_url"] == state["published_url"]
    names = {entry["name"] for entry in manifest["artifacts"]}
    assert {"motion_clip", "preview_frame", "final_video"} <= names
    assert all(entry["sha256"] for entry in manifest["artifacts"])


def test_hitl_without_approval_stays_pending():
    result = mod.hitl_checkpoint_node({"approval_status": "pending"})

    assert result["status"] == "awaiting_approval"
    assert result["approval_status"] == "pending"


def test_verify_accepts_pending_and_published_runs(tmp_path):
    motion = tmp_path / "motion.mp4"
    preview = tmp_path / "preview.png"
    motion.write_bytes(b"\x00" * 4096)
    preview.write_bytes(b"\x00" * 4096)

    pending = {
        "motion_asset_path": str(motion),
        "preview_frame_path": str(preview),
        "approval_status": "pending",
        "status": "awaiting_approval",
    }
    result = mod.verify(pending)
    assert result.ok, result.to_dict()
    assert "hitl_gate" in result.checks
    assert "export" not in result.checks

    final_video = tmp_path / "showreel.mp4"
    final_video.write_bytes(b"\x00" * mod.MIN_VIDEO_BYTES)
    completed = {
        **pending,
        "approval_status": "approved",
        "status": "completed",
        "exported_video_path": str(final_video),
        "published_url": f"{mod.CDN_PUBLIC_BASE}/showreel.mp4",
    }
    assert mod.verify(completed).ok

    # 최종 영상이 기준 크기에 못 미치면 실패한다
    small = tmp_path / "small.mp4"
    small.write_bytes(b"\x00" * 16)
    failed = mod.verify({**completed, "exported_video_path": str(small)})
    assert not failed.ok
    assert {issue.code for issue in failed.errors} == {"empty_artifact"}


def test_effectcraft_failure_raises_structured_error(monkeypatch, tmp_path):
    failing_cli = tmp_path / "effectcraft-cli"
    failing_cli.write_text("#!/bin/sh\necho 'effectcraft exploded' >&2\nexit 3\n")
    failing_cli.chmod(0o755)

    monkeypatch.setattr(mod, "OUTPUT_TEMP_ROOT", tmp_path / "out")
    monkeypatch.setattr(mod, "EFFECTCRAFT_BIN", str(failing_cli))

    with pytest.raises(core_exec.CommandError) as excinfo:
        mod.effectcraft_motion_node({"run_id": "boom", "brief": "Explode: Unit"})

    assert excinfo.value.returncode == 3
    assert "effectcraft exploded" in str(excinfo.value)
    assert excinfo.value.argv[0] == str(failing_cli)
