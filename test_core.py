"""core 런타임(W1) 단위 테스트 — env·exec·artifacts·contract·hitl.

네트워크·렌더러 없이 돈다. subprocess는 현재 인터프리터만 쓴다.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, TypedDict

import pytest

from graphs.core import artifacts, contract, env, hitl
from graphs.core import exec as core_exec

ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------


def test_env_defaults_match_existing_graphs():
    paths = env.load_paths({})
    assert paths.assets_dir == Path("/home/ubuntu/sonagi-assets")
    assert paths.catalog == Path(
        "/home/ubuntu/sonagi-assets/test_data/golden/catalog.json"
    )
    assert paths.tokens == Path("/home/ubuntu/sonagi-assets/tokens/semantic.json")
    assert paths.wiki_dir == Path("/home/ubuntu/llm-wiki")
    assert paths.searxng_url == "http://localhost:8080"
    assert paths.vectorcraft == "vectorcraft-cli"


def test_env_overrides():
    paths = env.load_paths(
        {
            "SONAGI_ASSETS_DIR": "/srv/assets",
            "SONAGI_CATALOG": "/srv/catalog.json",
            "SONAGI_TOKENS": "/srv/tokens.json",
            "SONAGI_OUT_ROOT": "/srv/out",
            "SONAGI_WIKI_DIR": "/srv/wiki",
            "SEARXNG_URL": "http://searx:8080",
            "VECTORCRAFT_BIN": "/opt/vc",
        }
    )
    assert paths.assets_dir == Path("/srv/assets")
    assert paths.catalog == Path("/srv/catalog.json")
    assert paths.tokens == Path("/srv/tokens.json")
    assert paths.out_root == Path("/srv/out")
    assert paths.wiki_dir == Path("/srv/wiki")
    assert paths.searxng_url == "http://searx:8080"
    assert paths.vectorcraft == "/opt/vc"


def test_catalog_and_tokens_follow_assets_dir():
    paths = env.load_paths({"SONAGI_ASSETS_DIR": "/srv/assets"})
    assert paths.catalog == Path("/srv/assets/test_data/golden/catalog.json")
    assert paths.tokens == Path("/srv/assets/tokens/semantic.json")


def test_out_root_legacy_aliases_and_precedence():
    assert env.out_root("entry_pack", {}) == Path("/tmp/entry_pack")
    assert env.out_root("asset_craft", {}) == Path("/tmp/asset_craft")
    assert env.out_root("logo_design", {}) == Path("/tmp/package_delivery")
    assert env.out_root("new_graph", {}) == Path(env.DEFAULT_OUT_ROOT)
    assert env.out_root("entry_pack", {"SONAGI_ENTRY_OUT": "/srv/entry"}) == Path(
        "/srv/entry"
    )
    # 명시적 공통 루트가 그래프 별칭을 이긴다
    assert env.out_root(
        "entry_pack", {"SONAGI_OUT_ROOT": "/srv/all", "SONAGI_ENTRY_OUT": "/x"}
    ) == Path("/srv/all")


def test_import_has_no_side_effects(tmp_path):
    """import만으로 디렉터리·파일을 만들지 않는다."""
    marker = tmp_path / "out"
    env_vars = {
        **os.environ,
        "SONAGI_OUT_ROOT": str(marker),
        "PYTHONPATH": str(ROOT),
    }
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import graphs.core.env, graphs.core.exec, graphs.core.artifacts;"
            "import graphs.core.contract, graphs.core.hitl",
        ],
        cwd=str(ROOT),
        env=env_vars,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not marker.exists()


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


def test_exec_success_returns_stdout_and_stderr():
    result = core_exec.run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ]
    )
    assert result.ok
    assert result.returncode == 0
    assert "out" in result.stdout
    assert "err" in result.stderr
    assert result.attempts == 1


def test_exec_failure_raises_structured_error():
    with pytest.raises(core_exec.CommandError) as excinfo:
        core_exec.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    error = excinfo.value
    assert error.returncode == 3
    assert error.timed_out is False
    assert str(error).startswith("실패:")
    assert "종료 코드 3" in str(error)
    assert error.to_dict()["returncode"] == 3


def test_exec_failure_message_includes_both_streams():
    with pytest.raises(core_exec.CommandError) as excinfo:
        core_exec.run(
            [
                sys.executable,
                "-c",
                "import sys; print('stdout-tail'); print('stderr-tail', file=sys.stderr); sys.exit(1)",
            ]
        )
    message = str(excinfo.value)
    assert "stdout-tail" in message
    assert "stderr-tail" in message


def test_exec_check_false_returns_failure():
    result = core_exec.run(
        [sys.executable, "-c", "import sys; sys.exit(2)"], check=False
    )
    assert result.ok is False
    assert result.returncode == 2


def test_exec_timeout_is_reported_not_swallowed():
    with pytest.raises(core_exec.CommandError) as excinfo:
        core_exec.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    error = excinfo.value
    assert error.timed_out is True
    assert "시간 초과" in str(error)
    assert "5" in str(error) or "0.2" in str(error)


def test_exec_timeout_without_check_returns_result():
    result = core_exec.run(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2, check=False
    )
    assert result.timed_out is True
    assert result.returncode != 0
    assert result.timeout == 0.2


def test_exec_retries_count_attempts():
    result = core_exec.run(
        [sys.executable, "-c", "import sys; sys.exit(9)"], retries=2, check=False
    )
    assert result.attempts == 3
    assert result.returncode == 9


def test_exec_retry_codes_filter():
    result = core_exec.run(
        [sys.executable, "-c", "import sys; sys.exit(9)"],
        retries=2,
        retry_codes={7},
        check=False,
    )
    assert result.attempts == 1


def test_exec_retry_succeeds_on_second_attempt(tmp_path):
    counter = tmp_path / "count"
    script = (
        "from pathlib import Path\n"
        f"p = Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "raise SystemExit(0 if n >= 1 else 1)\n"
    )
    result = core_exec.run([sys.executable, "-c", script], retries=2)
    assert result.attempts == 2
    assert counter.read_text() == "2"


def test_exec_log_writes_jsonl(tmp_path):
    log_path = tmp_path / "logs" / "commands.jsonl"
    core_exec.run(
        [sys.executable, "-c", "import sys; sys.exit(4)"],
        retries=1,
        log=log_path,
        check=False,
    )
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert '"returncode": 4' in lines[0]
    assert '"attempts": 1' in lines[0]
    assert '"attempts": 2' in lines[1]


def test_exec_rejects_empty_argv():
    with pytest.raises(ValueError):
        core_exec.run([])


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def test_sha256_is_deterministic(tmp_path):
    payload = b"sonagi"
    assert artifacts.sha256_bytes(payload) == artifacts.sha256_bytes(payload)
    assert artifacts.sha256_bytes(payload) != artifacts.sha256_bytes(payload + b"!")
    assert artifacts.sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )

    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_bytes(payload)
    second.write_bytes(payload)
    assert artifacts.sha256_file(first) == artifacts.sha256_file(second)


def test_sha256_json_ignores_key_order():
    assert artifacts.sha256_json({"a": 1, "b": 2}) == artifacts.sha256_json(
        {"b": 2, "a": 1}
    )
    assert artifacts.sha256_json({"a": 1}) != artifacts.sha256_json({"a": 2})


def test_manifest_roundtrip(tmp_path):
    run_dir = artifacts.create_run_dir(tmp_path, "run-001")
    out = run_dir / "pack" / "page.png"
    out.parent.mkdir()
    out.write_bytes(b"png")
    manifest = artifacts.build_manifest(
        run_id="run-001",
        kind="entry_pack",
        inputs={"brief": artifacts.sha256_bytes(b"brief")},
        artifacts=[artifacts.artifact_entry(out, root=run_dir)],
        gates=[{"gate": "hitl", "status": "approved"}],
        meta={"platform": "99designs"},
        created_at="2026-11-25T00:00:00Z",
    )
    path = artifacts.write_manifest(run_dir, manifest)
    loaded = artifacts.read_manifest(run_dir)
    assert path.name == artifacts.MANIFEST_NAME
    assert loaded == manifest
    assert loaded["artifacts"][0]["path"] == "pack/page.png"
    assert loaded["artifacts"][0]["size"] == 3


def test_manifest_read_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        artifacts.read_manifest(tmp_path / "nope")


def test_manifest_digest_ignores_volatile_keys(tmp_path):
    run_dir = artifacts.create_run_dir(tmp_path, "run-001")
    payload = run_dir / "x.txt"
    payload.write_text("x", encoding="utf-8")
    entry = artifacts.artifact_entry(payload, root=run_dir)
    first = artifacts.build_manifest(
        run_id="run-001", kind="k", artifacts=[entry], created_at="2026-11-25T00:00:00Z"
    )
    second = artifacts.build_manifest(
        run_id="run-999", kind="k", artifacts=[entry], created_at="2026-11-26T00:00:00Z"
    )
    assert artifacts.manifest_digest(first) == artifacts.manifest_digest(second)

    changed = dict(second)
    changed["artifacts"] = [dict(entry, sha256="0" * 64)]
    assert artifacts.manifest_digest(first) != artifacts.manifest_digest(changed)


def test_create_run_dir_does_not_overwrite(tmp_path):
    artifacts.create_run_dir(tmp_path, "run-001")
    with pytest.raises(FileExistsError):
        artifacts.create_run_dir(tmp_path, "run-001")
    assert artifacts.create_run_dir(tmp_path, "run-001", exist_ok=True).is_dir()


def test_create_run_writes_initial_manifest(tmp_path):
    run = artifacts.create_run(tmp_path, kind="entry_pack", run_id="run-001")
    assert run.dir == tmp_path / "run-001"
    loaded = run.read()
    assert loaded["status"] == "running"
    assert loaded["kind"] == "entry_pack"
    run.write({"schema": 1, "run_id": "run-001", "kind": "entry_pack", "status": "ok"})
    assert run.read()["status"] == "ok"


def test_prune_keeps_newest_runs(tmp_path):
    for index in range(5):
        artifacts.create_run_dir(tmp_path, f"run-{index:03d}")
    removed = artifacts.prune(tmp_path, keep=2)
    assert [path.name for path in removed] == ["run-002", "run-001", "run-000"]
    assert [path.name for path in artifacts.list_runs(tmp_path)] == [
        "run-004",
        "run-003",
    ]


def test_prune_dry_run_touches_nothing(tmp_path):
    for index in range(3):
        artifacts.create_run_dir(tmp_path, f"run-{index:03d}")
    removed = artifacts.prune(tmp_path, keep=1, dry_run=True)
    assert len(removed) == 2
    assert len(list(tmp_path.iterdir())) == 3


def test_prune_ignores_foreign_directories(tmp_path):
    (tmp_path / "keep-me").mkdir()
    for index in range(3):
        artifacts.create_run_dir(tmp_path, f"run-{index:03d}")
    artifacts.prune(tmp_path, keep=1)
    assert (tmp_path / "keep-me").is_dir()


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------


class EntryInput(TypedDict):
    brief: str
    top_n: int


class EntryOutput(TypedDict, total=False):
    items: List[Dict[str, Any]]
    status: str


def test_check_schema_accepts_valid_payload():
    issues = contract.check_schema(EntryInput, {"brief": "브리프", "top_n": 3})
    assert issues == []


def test_check_schema_reports_missing_key():
    issues = contract.check_schema(EntryInput, {"brief": "브리프"})
    assert [issue.code for issue in issues] == ["missing_key"]
    assert issues[0].field == "top_n"


def test_check_schema_reports_wrong_type():
    issues = contract.check_schema(EntryInput, {"brief": 7, "top_n": "3"})
    assert {issue.code for issue in issues} == {"wrong_type"}
    assert len(issues) == 2


def test_check_schema_total_false_allows_empty():
    assert contract.check_schema(EntryOutput, {}) == []


def test_verify_contract_passes():
    simple = contract.SimpleContract(
        name="entry",
        inputs=EntryInput,
        outputs=EntryOutput,
        verify=lambda state: True,
    )
    state = {"brief": "b", "top_n": 1, "items": [], "status": "ok"}
    result = contract.verify_contract(simple, state, stage="both")
    assert result.ok
    assert result.status == "pass"
    assert result.checks == ("inputs", "verify", "outputs")


def test_verify_contract_false_fails():
    simple = contract.SimpleContract(
        name="entry",
        inputs=EntryInput,
        outputs=EntryOutput,
        verify=lambda state: False,
    )
    state = {"brief": "b", "top_n": 1, "status": "ok"}
    result = contract.verify_contract(simple, state, stage="none")
    assert result.ok is False
    assert result.status == "fail"


def test_verify_contract_accepts_issue_list_and_dict():
    issues = [contract.error("overlap", "텍스트가 겹친다", "title")]
    simple = contract.SimpleContract(
        name="entry",
        inputs=EntryInput,
        outputs=EntryOutput,
        verify=lambda state: issues,
    )
    result = contract.verify_contract(simple, {"brief": "b", "top_n": 1}, stage="none")
    assert result.ok is False
    assert result.errors[0].code == "overlap"

    dict_result = contract.normalize_verdict({"ok": False, "issues": [{"code": "x"}]})
    assert dict_result.ok is False
    assert dict_result.issues[0].code == "x"


def test_verify_contract_none_is_pass():
    simple = contract.SimpleContract(
        name="entry", inputs=EntryInput, outputs=EntryOutput, verify=lambda state: None
    )
    assert contract.verify_contract(simple, {"brief": "b", "top_n": 1}, stage="none").ok


def test_verify_contract_warning_only_is_warn():
    simple = contract.SimpleContract(
        name="entry",
        inputs=EntryInput,
        outputs=EntryOutput,
        verify=lambda state: [contract.warning("thin", "설명이 짧다")],
    )
    result = contract.verify_contract(simple, {"brief": "b", "top_n": 1}, stage="none")
    assert result.ok is True
    assert result.status == "warn"


def test_contract_protocol_isinstance_and_raise_for_failure():
    simple = contract.SimpleContract(
        name="entry", inputs=EntryInput, outputs=EntryOutput, verify=lambda state: True
    )
    assert isinstance(simple, contract.Contract)

    failed = contract.verify_contract(
        contract.SimpleContract(
            name="entry",
            inputs=EntryInput,
            outputs=EntryOutput,
            verify=lambda state: False,
        ),
        {"brief": "b", "top_n": 1},
        stage="none",
    )
    with pytest.raises(contract.ContractError):
        contract.raise_for_failure(failed)


def test_load_schema_is_serializable():
    schema = contract.load_schema(EntryInput)
    assert schema["brief"] == {"type": "str", "required": True}
    assert schema["top_n"]["required"] is True


def test_verify_contract_rejects_unknown_stage():
    simple = contract.SimpleContract(
        name="entry", inputs=EntryInput, outputs=EntryOutput, verify=lambda state: True
    )
    with pytest.raises(ValueError):
        contract.verify_contract(simple, {}, stage="everything")


# ---------------------------------------------------------------------------
# hitl
# ---------------------------------------------------------------------------


OPTIONS = [{"id": "a"}, {"id": "b"}, {"id": "c"}]


def test_hitl_pending_defaults():
    state = hitl.pending(gate="entry_pack.gate", options=OPTIONS, digest="d1")
    assert state["approval_status"] == hitl.PENDING
    assert state["selection"] == []
    assert hitl.is_approved(state) is False
    result = hitl.check(state)
    assert result.status == hitl.PENDING


def test_hitl_pending_to_approved():
    state = {"brief": "b"}
    state.update(hitl.pending(gate="gate", options=OPTIONS, digest="d1"))
    state.update(hitl.approve(["b", "a"], gate="gate", digest="d1"))
    result = hitl.check(state, digest="d1", allowed=["a", "b", "c"])
    assert result.approved()
    assert result.selection == ("b", "a")
    assert hitl.route_after_gate(state, digest="d1") == "publish"
    assert hitl.route_after_gate({"approval_status": hitl.PENDING}) == hitl.END


def test_hitl_dedupes_selection():
    state = hitl.approve(["a", "a", "b"])
    assert state["selection"] == ["a", "b"]


def test_hitl_reject_keeps_reason():
    state = hitl.reject("방향이 브리프와 다르다")
    result = hitl.check(state)
    assert result.status == hitl.REJECTED
    assert "브리프" in result.reason


def test_hitl_unknown_selection_is_invalid():
    state = hitl.approve(["zzz"])
    result = hitl.check(state, allowed=["a", "b"])
    assert result.status == hitl.INVALID
    assert "zzz" in result.reason
    with pytest.raises(hitl.SelectionError):
        hitl.select(state, allowed=["a", "b"])


def test_hitl_empty_selection_requires_one():
    state = hitl.approve([])
    with pytest.raises(hitl.SelectionError):
        hitl.select(state, allowed=["a"], require=True)
    assert hitl.select(state, allowed=["a"], require=False) == []


def test_hitl_stale_digest_blocks_approval():
    state = hitl.approve(["a"], digest="old-digest")
    result = hitl.check(state, digest="new-digest", allowed=["a"])
    assert result.status == hitl.STALE
    assert result.approved() is False
    assert "지문" in result.reason
    assert hitl.route_after_gate(state, digest="new-digest", allowed=["a"]) == hitl.END


def test_hitl_limit_truncates_selection():
    state = hitl.approve(["a", "b", "c"])
    result = hitl.check(state, allowed=["a", "b", "c"], limit=2)
    assert result.selection == ("a", "b")


def test_hitl_gate_manifest_entry_roundtrip():
    result = hitl.check(hitl.approve(["a"], digest="d1"), digest="d1")
    entry = hitl.gate_manifest_entry(result, gate="entry_pack.gate")
    assert entry["gate"] == "entry_pack.gate"
    assert entry["status"] == hitl.APPROVED
    assert entry["selection"] == ["a"]
