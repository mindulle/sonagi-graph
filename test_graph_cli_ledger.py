"""graph_cli 원장 조회(W5) 단위 테스트 — HTTP·파일은 전부 대역, 네트워크 없음.

`runs`/`show`의 추출·포맷 헬퍼와 mock HTTP 명령 경로를 검증한다.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from graphs.core import artifacts  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "graph_cli", ROOT / "scripts" / "graph_cli.py"
)
graph_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(graph_cli)


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


def make_manifest(tmp_path, kind="entry_pack"):
    """실제 core artifacts로 run 디렉터리 + manifest를 만든다."""
    run_dir = artifacts.create_run_dir(tmp_path, prefix="run")
    pack_dir = run_dir / "pack"
    pack_dir.mkdir()
    meta = pack_dir / "meta.json"
    meta.write_text('{"ok": true}', encoding="utf-8")
    manifest = artifacts.build_manifest(
        run_id=run_dir.name,
        kind=kind,
        inputs={"brief": artifacts.sha256_bytes(b"min-dule")},
        artifacts=[artifacts.artifact_entry(meta, root=run_dir)],
    )
    manifest_path = artifacts.write_manifest(run_dir, manifest)
    return run_dir, manifest_path


def fake_http(monkeypatch, routes):
    """`_request` 대역. routes: (method, path) → 값 또는 호출 기록 함수."""
    calls = []

    def _request(method, path, payload=None, timeout=120):
        calls.append((method, path, payload))
        handler = routes.get((method, path))
        if handler is None:
            raise AssertionError(f"예상하지 않은 호출: {method} {path}")
        return handler(payload) if callable(handler) else handler

    monkeypatch.setattr(graph_cli, "_request", _request)
    return calls


# ---------------------------------------------------------------------------
# short_time
# ---------------------------------------------------------------------------


def test_short_time_normalizes_iso_microseconds_and_z():
    assert (
        graph_cli.short_time("2026-11-25T10:00:00.123456+00:00")
        == "2026-11-25T10:00:00Z"
    )
    assert graph_cli.short_time("2026-11-25T10:00:00Z") == "2026-11-25T10:00:00Z"
    # 오프셋은 UTC로 옮긴다
    assert graph_cli.short_time("2026-11-25T19:00:00+09:00") == "2026-11-25T10:00:00Z"


def test_short_time_accepts_epoch_and_keeps_garbage():
    stamp = 1770000000
    assert graph_cli.short_time(stamp) == time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)
    )
    assert graph_cli.short_time(1770000000000) == graph_cli.short_time(stamp)
    assert graph_cli.short_time("어제쯤") == "어제쯤"
    assert graph_cli.short_time(None) is None


# ---------------------------------------------------------------------------
# ledger_row / format_ledger
# ---------------------------------------------------------------------------


def test_ledger_row_prefers_thread_metadata_graph_id():
    row = graph_cli.ledger_row(
        {
            "thread_id": "t-1",
            "created_at": "2026-11-25T10:00:00.000000+00:00",
            "status": "interrupted",
            "metadata": {"graph_id": "logo_design", "assistant_id": "abc"},
        }
    )
    assert row == {
        "thread_id": "t-1",
        "graph_id": "logo_design",
        "status": "interrupted",
        "created_at": "2026-11-25T10:00:00Z",
    }


def test_ledger_row_falls_back_to_latest_run_when_metadata_empty():
    row = graph_cli.ledger_row(
        {"thread_id": "t-2", "metadata": {}},
        {
            "assistant_id": "asst-9",
            "status": "success",
            "created_at": "2026-11-24T09:00:00Z",
        },
    )
    assert row["graph_id"] == "asst-9"
    assert row["status"] == "success"
    assert row["created_at"] == "2026-11-24T09:00:00Z"


def test_ledger_rows_are_newest_first_and_limited():
    threads = [
        {"thread_id": "old", "created_at": "2026-11-24T10:00:00Z", "status": "idle"},
        {"thread_id": "new", "created_at": "2026-11-25T10:00:00Z", "status": "busy"},
        {"thread_id": "mid", "created_at": "2026-11-24T23:00:00Z", "status": "error"},
    ]
    rows = graph_cli.ledger_rows(threads, limit=2)
    assert [row["thread_id"] for row in rows] == ["new", "mid"]


def test_format_ledger_table_and_empty_state():
    rows = [
        {
            "thread_id": "t-new",
            "graph_id": "error_triage",
            "status": "success",
            "created_at": "2026-11-25T10:00:00Z",
        },
        {"thread_id": "t-old", "graph_id": None, "status": None, "created_at": None},
    ]
    text = graph_cli.format_ledger(rows)
    lines = text.splitlines()
    assert lines[0].split() == ["thread_id", "graph", "status", "created"]
    assert "error_triage" in lines[1]
    assert lines[2].split() == ["t-old", "-", "-", "-"]
    assert graph_cli.format_ledger([]) == "실행 기록 없음"


# ---------------------------------------------------------------------------
# manifest 경로 추출
# ---------------------------------------------------------------------------


def test_manifest_paths_from_nested_values_dedupes_and_keeps_order():
    values = {
        "manifest": "/tmp/a/manifest.json",
        "nested": {"run": {"manifest_path": "/tmp/b/manifest.json"}},
        "batches": [
            {"manifest_files": ["/tmp/c/manifest.json", "/tmp/a/manifest.json"]},
            {"manifest": {"path": "/tmp/d/manifest.json"}},
        ],
        "render": {"manifest": "/tmp/render/x.manifest.json"},
        "note": "산출물은 /tmp/e/manifest.json 에 있다",  # 공백 있는 산문은 경로가 아니다
        "other": "/tmp/f/notes.md",
    }
    assert graph_cli.manifest_paths_from_values(values) == [
        "/tmp/a/manifest.json",
        "/tmp/b/manifest.json",
        "/tmp/c/manifest.json",
        "/tmp/d/manifest.json",
        "/tmp/render/x.manifest.json",
    ]


def test_manifest_paths_ignores_values_without_manifest():
    assert graph_cli.manifest_paths_from_values(None) == []
    assert graph_cli.manifest_paths_from_values({"brief": "로그", "n": 3}) == []


# ---------------------------------------------------------------------------
# manifest_report / format_manifests
# ---------------------------------------------------------------------------


def test_manifest_report_reads_digest_and_file_sha256(tmp_path):
    run_dir, manifest_path = make_manifest(tmp_path)
    rows = graph_cli.manifest_report([str(manifest_path)])
    stored = artifacts.read_manifest(manifest_path)

    assert rows[0]["exists"] is True
    assert rows[0]["digest"] == artifacts.manifest_digest(stored)
    assert rows[0]["sha256"] == artifacts.sha256_file(manifest_path)
    assert rows[0]["run_id"] == run_dir.name
    assert rows[0]["kind"] == "entry_pack"
    assert rows[0]["status"] == "ok"
    assert rows[0]["artifacts"] == 1


def test_manifest_report_accepts_run_directory(tmp_path):
    run_dir, manifest_path = make_manifest(tmp_path)
    rows = graph_cli.manifest_report([str(run_dir)])
    assert rows[0]["manifest_path"] == str(manifest_path)
    assert rows[0]["digest"] == artifacts.manifest_digest(
        artifacts.read_manifest(manifest_path)
    )


def test_manifest_report_missing_file_and_dir_without_manifest(tmp_path):
    missing = tmp_path / "nope" / "manifest.json"
    empty_dir = tmp_path / "run-empty"
    empty_dir.mkdir()

    rows = graph_cli.manifest_report([str(missing), str(empty_dir)])
    assert rows[0] == {"path": str(missing), "exists": False}
    assert rows[1]["exists"] is True
    assert "manifest가 없다" in rows[1]["error"]


def test_manifest_report_flags_broken_json(tmp_path):
    broken = tmp_path / "manifest.json"
    broken.write_text("{not json", encoding="utf-8")
    rows = graph_cli.manifest_report([str(broken)])
    assert rows[0]["exists"] is True
    assert "JSONDecodeError" in rows[0]["error"]


def test_format_manifests_degrades_to_manifest_none():
    assert graph_cli.format_manifests([]) == "manifest 없음"


def test_format_manifests_prints_hashes_and_missing(tmp_path):
    _, manifest_path = make_manifest(tmp_path)
    rows = graph_cli.manifest_report(
        [str(manifest_path), str(tmp_path / "gone" / "manifest.json")]
    )
    text = graph_cli.format_manifests(rows)
    assert str(manifest_path) in text
    assert "sha256:" in text
    assert "digest:" in text
    assert "kind: entry_pack" in text
    assert "파일 없음" in text


# ---------------------------------------------------------------------------
# 명령 경로 (mock HTTP)
# ---------------------------------------------------------------------------


THREADS = [
    {
        "thread_id": "t-old",
        "created_at": "2026-11-24T10:00:00.000000+00:00",
        "status": "idle",
        "metadata": {"graph_id": "error_triage"},
    },
    {
        "thread_id": "t-new",
        "created_at": "2026-11-25T10:00:00.000000+00:00",
        "status": "interrupted",
        "metadata": {"assistant_id": "asst-123"},
    },
]


def test_cmd_runs_lists_ledger_json_with_limit(monkeypatch, capsys):
    calls = fake_http(
        monkeypatch,
        {("POST", "/threads/search"): lambda payload: THREADS},
    )
    assert graph_cli.main(["runs", "--limit", "5", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)

    assert calls[0][2]["limit"] == 5
    assert [row["thread_id"] for row in rows] == ["t-new", "t-old"]
    assert rows[0]["graph_id"] == "asst-123"
    assert rows[0]["created_at"] == "2026-11-25T10:00:00Z"


def test_cmd_runs_uses_global_json_flag_and_text_mode(monkeypatch, capsys):
    fake_http(monkeypatch, {("POST", "/threads/search"): THREADS})

    graph_cli.main(["--json", "runs", "--limit", "1"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["thread_id"] == "t-new"

    graph_cli.main(["runs"])
    text = capsys.readouterr().out
    assert text.splitlines()[0].split() == ["thread_id", "graph", "status", "created"]


def test_cmd_runs_keeps_thread_history_behavior(monkeypatch, capsys):
    runs = [{"run_id": "r1", "status": "success"}, {"run_id": "r2", "status": "error"}]
    calls = fake_http(monkeypatch, {("GET", "/threads/t-9/runs"): runs})

    graph_cli.main(["runs", "t-9"])
    assert json.loads(capsys.readouterr().out) == runs
    assert calls == [("GET", "/threads/t-9/runs", None)]


def test_cmd_show_prints_state_and_manifest_hashes(monkeypatch, capsys, tmp_path):
    _, manifest_path = make_manifest(tmp_path)
    state = {
        "values": {"status": "done", "manifest": str(manifest_path)},
        "next": [],
    }
    fake_http(
        monkeypatch,
        {
            ("GET", "/threads/t-1/state"): state,
            ("GET", "/threads/t-1/runs"): [{"status": "success"}],
        },
    )

    graph_cli.main(["show", "t-1"])
    out = capsys.readouterr().out
    digest = artifacts.manifest_digest(artifacts.read_manifest(manifest_path))

    assert "thread_id: t-1" in out
    assert "run_status: success" in out
    assert "status: done" in out
    assert "manifest.json" in out
    assert digest in out
    assert artifacts.sha256_file(manifest_path) in out


def test_cmd_show_without_manifest_prints_manifest_none(monkeypatch, capsys):
    fake_http(
        monkeypatch,
        {
            ("GET", "/threads/t-2/state"): {
                "values": {"status": "init"},
                "next": ["hitl"],
            },
            ("GET", "/threads/t-2/runs"): [{"status": "interrupted"}],
        },
    )
    graph_cli.main(["show", "t-2"])
    out = capsys.readouterr().out
    assert "manifest 없음" in out
    assert 'next: ["hitl"]' in out


def test_cmd_show_json_shape(monkeypatch, capsys, tmp_path):
    _, manifest_path = make_manifest(tmp_path, kind="video_ops")
    fake_http(
        monkeypatch,
        {
            ("GET", "/threads/t-3/state"): {
                "values": {"manifest_paths": [str(manifest_path)]},
                "next": [],
            },
            ("GET", "/threads/t-3/runs"): [{"status": "success"}],
        },
    )
    graph_cli.main(["show", "t-3", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["summary"]["thread_id"] == "t-3"
    assert payload["manifests"][0]["kind"] == "video_ops"
    assert payload["manifests"][0]["exists"] is True


def test_search_threads_retries_without_sort_fields(monkeypatch):
    seen = []

    def _request(method, path, payload=None, timeout=120):
        seen.append(dict(payload or {}))
        if len(seen) == 1:
            raise SystemExit("HTTP 422: sort_by")
        return THREADS

    monkeypatch.setattr(graph_cli, "_request", _request)
    assert graph_cli._search_threads(3) == THREADS
    assert seen[0]["sort_by"] == "thread_id"
    assert seen[1] == {"limit": 3}


@pytest.mark.parametrize("limit", [0, -2])
def test_cmd_runs_clamps_limit(monkeypatch, capsys, limit):
    calls = fake_http(monkeypatch, {("POST", "/threads/search"): []})
    graph_cli.main(["runs", "--limit", str(limit)])
    assert calls[0][2]["limit"] == 1
    assert capsys.readouterr().out.strip() == "실행 기록 없음"
