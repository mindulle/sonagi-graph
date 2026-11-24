"""Figma Variables 별칭 감사 — 별칭 해석·드리프트 판정·수집 실패 처리.

실서비스 파일의 시맨틱 변수는 전부 별칭이다(예: `state/error → state/danger →
crimson/50`). 값만 비교하면 29건이 전부 거짓 드리프트가 나므로, 별칭을 모드 이름
따라 끝까지 해석하는 규칙을 여기서 고정한다.

픽스처 `test_data/figma/snapshot_mindulle_2026-11-24.json`은 2026-11-24 실서비스
파일에서 읽은 Colors(29)+Color Primitives(43)이고, 값·사슬은 실측 그대로다.
"""

import copy
import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import figma_drift as fd  # noqa: E402
import token_sync as ts  # noqa: E402

FIXTURE = Path(__file__).with_name("test_data") / "figma" / "snapshot_mindulle_2026-11-24.json"


def load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def real_request():
    analysis, _ = ts.analyze(None, None)
    return ts.figma_write_request(ts.figma_plan(analysis))


def var_by_name(snapshot, name):
    return next(v for v in snapshot["variables"] if v["name"] == name)


def prim_id(snapshot, name):
    return var_by_name(snapshot, name)["id"]


# ---------------------------------------------------------------- 해석


def test_fixture_matches_mds_with_aliases_resolved():
    report = fd.check_snapshot(load(), real_request())

    assert report["ok"] is True
    assert report["matched"] == report["expected"] == 29
    assert report["drift"] == []
    assert report["aliasIssues"] == []
    # 시맨틱 29개가 전부 별칭이므로 해석 횟수는 최소 29
    assert report["aliasResolved"] >= 29


def test_alias_chain_is_reported():
    snapshot = load()
    # state/error → state/danger → crimson/*  (2단계)
    resolved = fd.resolve_value(snapshot, var_by_name(snapshot, "state/error")["id"], "31:1")

    assert resolved["chain"] == ["state/error", "state/danger", "crimson/50"]
    assert resolved["value"] == "#fde8e8"


def test_repointed_alias_shows_drift_with_chain():
    """crimson/50 → crimson/400으로 되돌리면 드리프트 2건(danger·error)이 잡힌다."""
    snapshot = load()
    colors_dark = "31:1"
    var_by_name(snapshot, "state/danger")["values"][colors_dark] = {"alias": prim_id(snapshot, "crimson/400")}

    report = fd.check_snapshot(snapshot, real_request())

    assert report["ok"] is False
    drifted = {item["name"] for item in report["drift"]}
    assert drifted == {"state/danger", "state/error"}
    item = next(d for d in report["drift"] if d["name"] == "state/danger")
    assert item["actual"] == "#f85149"
    assert item["chain"] == ["state/danger", "crimson/400"]


def test_alias_cycle_is_an_issue_not_a_value():
    snapshot = {
        "schema": fd.SNAPSHOT_SCHEMA,
        "collections": [{"id": "c", "name": "Colors", "modes": [{"id": "m", "name": "Light"}]}],
        "variables": [
            {"id": "a", "name": "a", "collectionId": "c", "values": {"m": {"alias": "b"}}},
            {"id": "b", "name": "b", "collectionId": "c", "values": {"m": {"alias": "a"}}},
        ],
    }
    report = fd.check_snapshot(
        snapshot, {"collection": "Colors", "modes": ["Light"],
                   "variables": [{"name": "a", "modes": {"Light": "#000000"}}]}
    )

    assert report["ok"] is False
    assert report["aliasIssues"][0]["issue"] == "alias-cycle"


def test_missing_alias_target_is_an_issue():
    snapshot = load()
    var_by_name(snapshot, "state/success")["values"]["31:1"] = {"alias": "VariableID:nope"}

    report = fd.check_snapshot(snapshot, real_request())

    assert report["ok"] is False
    issue = next(i for i in report["aliasIssues"] if i["name"] == "state/success")
    assert issue["issue"] == "alias-missing"


def test_target_without_matching_mode_falls_back_to_first_mode():
    """별칭 대상 컬렉션에 같은 이름의 모드가 없으면 첫 모드로 해석한다."""
    snapshot = load()
    resolved = fd.resolve_value(snapshot, var_by_name(snapshot, "state/danger")["id"], "31:1")

    assert resolved["value"] == "#fde8e8"  # Color Primitives는 단일 모드


def test_mode_missing_when_source_mode_absent():
    snapshot = load()
    var = var_by_name(snapshot, "background/base")
    resolved = fd.resolve_value(snapshot, var["id"], "99:99")

    assert resolved["issue"] == "mode-missing"


def test_collection_missing_is_reported():
    report = fd.check_snapshot(load(), {"collection": "Semantic Colors", "modes": ["Light"], "variables": []})

    assert report["ok"] is False
    assert report["reason"] == "collection_missing"


# ---------------------------------------------------------------- REST 정규화


def test_snapshot_from_rest_keeps_alias_ids_and_modes():
    payload = {
        "meta": {
            "variableCollections": {
                "VC:1": {"name": "Colors", "modes": [{"modeId": "m1", "name": "Light"}, {"modeId": "m2", "name": "Dark"}]}
            },
            "variables": {
                "V:1": {"name": "text/primary", "variableCollectionId": "VC:1", "resolvedType": "COLOR",
                        "valuesByMode": {"m1": {"type": "VARIABLE_ALIAS", "id": "V:2"}, "m2": {"r": 0.5, "g": 0.5, "b": 0.5, "a": 1}}},
                "V:2": {"name": "neutral/900", "variableCollectionId": "VC:1", "resolvedType": "COLOR",
                        "valuesByMode": {"m1": {"r": 0.0941, "g": 0.0941, "b": 0.1059, "a": 1}}},
            },
        }
    }
    snapshot = fd.snapshot_from_rest(payload, file_key="k")

    assert snapshot["schema"] == fd.SNAPSHOT_SCHEMA
    assert snapshot["collections"][0]["modes"][1] == {"id": "m2", "name": "Dark"}
    text = next(v for v in snapshot["variables"] if v["name"] == "text/primary")
    assert text["values"]["m1"] == {"alias": "V:2"}
    assert text["values"]["m2"] == "#808080"
    assert fd.resolve_value(snapshot, "V:1", "m1")["value"] == "#18181b"


# ---------------------------------------------------------------- 수집 실패 (fail-closed)


def test_missing_token_is_not_a_pass(monkeypatch, tmp_path):
    monkeypatch.delenv(fd.TOKEN_ENV, raising=False)
    monkeypatch.setattr(fd, "TOKEN_FILE", str(tmp_path / "nope"))

    with pytest.raises(fd.FigmaTokenMissing):
        fd.fetch_local_variables("KEY")


def test_expired_token_raises_auth_error(monkeypatch):
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    with pytest.raises(fd.FigmaAuthError):
        fd.fetch_local_variables("KEY", token="expired", opener=opener)


def test_network_failure_raises_fetch_error():
    def opener(request, timeout=None):
        raise OSError("connection refused")

    with pytest.raises(fd.FigmaFetchError):
        fd.fetch_local_variables("KEY", token="t", opener=opener)


# ---------------------------------------------------------------- CLI 종료 코드


def test_cli_exit_zero_on_fixture(tmp_path, capsys):
    code = fd.main(["--snapshot", str(FIXTURE)])
    assert code == 0
    assert "matched 29/29" in capsys.readouterr().out


def test_cli_exit_one_on_drift(tmp_path, capsys):
    snapshot = load()
    var_by_name(snapshot, "border/strong")["values"]["31:0"] = {"alias": prim_id(snapshot, "gold/600")}
    path = tmp_path / "drift.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    assert fd.main(["--snapshot", str(path)]) == 1
    assert "drift border/strong" in capsys.readouterr().out


def test_cli_exit_two_when_cannot_verify(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(fd.TOKEN_ENV, raising=False)
    monkeypatch.setattr(fd, "TOKEN_FILE", str(tmp_path / "nope"))

    assert fd.main(["--file-key", "KEY"]) == 2
    assert "확인 불가" in capsys.readouterr().err


# ---------------------------------------------------------------- 기존 경로 연결


def test_verify_figma_readback_delegates_alias_snapshots():
    """GRAPH-7의 판정 함수가 별칭 스냅샷을 받아도 거짓 드리프트를 내지 않는다."""
    report = ts.verify_figma_readback(real_request(), load())

    assert report["ok"] is True
    assert report["matched"] == 29
    assert report["aliasResolved"] >= 29


def test_snapshot_script_keeps_alias_ids_and_all_collections():
    script = ts.figma_snapshot_script()

    assert "sonagi.figma.snapshot/1" in script
    assert "VARIABLE_ALIAS" in script
    assert "getLocalVariableCollectionsAsync" in script
    assert "createVariable" not in script
