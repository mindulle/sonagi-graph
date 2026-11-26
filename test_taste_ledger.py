"""취향 결정 원장(Graph-35) 테스트 — HTTP는 대역, 파일은 tmp_path만 쓴다."""

from __future__ import annotations

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from graphs.taste import ledger, profile  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "graph_cli", ROOT / "scripts" / "graph_cli.py"
)
graph_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(graph_cli)


RECORD_KEYS = {
    "ts",
    "run_id",
    "thread_id",
    "profile",
    "platform",
    "brief_sha256",
    "brief_head",
    "options",
    "selected",
    "rejected",
    "reason_tags",
    "reason_text",
    "critic_top",
    "agreement_top1",
    "artifacts",
}


def write_option_svg(directory: Path, name: str, fill: str = "#161616") -> Path:
    path = directory / f"{name}.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<rect x="0" y="0" width="200" height="100" fill="#fbf9f5"/>'
        f'<rect x="20" y="20" width="60" height="40" fill="{fill}"/>'
        '<text x="20" y="90" font-size="14" fill="#161616">Hello</text>'
        "</svg>",
        encoding="utf-8",
    )
    return path


def sample_state(tmp_path: Path, *, with_critic: bool = True):
    a = write_option_svg(tmp_path, "a")
    b = write_option_svg(tmp_path, "b", fill="#b4551f")
    c = write_option_svg(tmp_path, "c", fill="#7a7a7a")
    values = {
        "brief": "민들레 티 Earl Grey 라벨 — 차분하고 고급스러운,\n인쇄 라벨",
        "run_id": "entry-test",
        "spec": {"profile": "print", "platform": "99designs"},
        "options": [
            {
                "id": "a",
                "direction": "차분한 종이",
                "style": "forest-paper",
                "theme": "light",
                "svg": str(a),
            },
            {
                "id": "b",
                "direction": "대담한 활자",
                "style": "star-editorial",
                "theme": "dark",
                "svg": str(b),
            },
            {
                "id": "c",
                "direction": "절제된 선",
                "style": "line-minimal",
                "theme": "light",
                "svg": str(c),
            },
        ],
        "selection": ["a"],
    }
    if with_critic:
        values["critiques"] = [
            {"id": "a", "verdict": "pass", "plan_score": 7, "findings": []},
            {
                "id": "b",
                "verdict": "warn",
                "plan_score": 9,
                "findings": [{"rule": "contrast", "severity": "warn"}],
            },
            {"id": "c", "verdict": "pass", "plan_score": 0, "findings": []},
        ]
    return {"values": values, "next": []}


# ---------------------------------------------------------------------------
# 기록 작성
# ---------------------------------------------------------------------------


def test_build_record_schema_and_decision_fields(tmp_path):
    record = ledger.build_record(
        thread_id="t-1",
        state=sample_state(tmp_path),
        values={"selection": ["a"]},
        graph="entry_pack",
        reason_tags=["차분함", "여백", "차분함"],
        reason_text="빈 공간이 편안해서",
    )

    assert set(record) == RECORD_KEYS
    assert record["run_id"] == "entry-test"
    assert record["profile"] == "print"
    assert record["platform"] == "99designs"
    assert len(record["brief_sha256"]) == 64
    assert "\n" not in record["brief_head"]
    assert record["brief_head"].startswith("민들레 티")
    assert record["selected"] == ["a"]
    assert record["rejected"] == ["b", "c"]
    assert record["reason_tags"] == ["차분함", "여백"]  # 순서 유지·중복 제거
    assert record["reason_text"] == "빈 공간이 편안해서"
    # 비평 1순위는 b(9점), 내 선택은 a → 불일치.
    assert record["critic_top"] == ["b", "a"]
    assert record["agreement_top1"] is False
    assert set(record["artifacts"]) == {"a", "b", "c"}
    assert all(len(value) == 64 for value in record["artifacts"].values())

    first = record["options"][0]
    assert set(first) == {"id", "direction", "style", "theme", "features", "critic"}
    assert first["features"]["source"] == "svg"
    assert first["critic"]["verdict"] == "pass"
    assert first["critic"]["plan_score"] == 7


def test_build_record_without_critic_has_no_agreement(tmp_path):
    record = ledger.build_record(
        thread_id="t-2",
        state=sample_state(tmp_path, with_critic=False),
        values={"selection": ["a"]},
    )
    assert all(option["critic"] == {} for option in record["options"])
    assert record["critic_top"] == []
    assert record["agreement_top1"] is None


def test_build_record_without_selection_marks_nothing_rejected(tmp_path):
    state = sample_state(tmp_path)
    state["values"].pop("selection")
    record = ledger.build_record(thread_id="t-3", state=state, values={})
    assert record["selected"] == []
    assert record["rejected"] == []


def test_entry_items_supply_sha256_and_png_features(tmp_path):
    png = tmp_path / "render.png"
    from PIL import Image

    Image.new("RGB", (100, 100), (251, 249, 245)).save(png)
    state = sample_state(tmp_path, with_critic=False)
    state["values"]["entry"] = {
        "items": [{"id": "a", "png": str(png), "sha256": "f" * 64}]
    }
    record = ledger.build_record(
        thread_id="t-4", state=state, values={"selection": ["a"]}
    )
    assert record["options"][0]["features"]["source"] == "png"
    assert record["artifacts"]["a"] == "f" * 64


# ---------------------------------------------------------------------------
# append / read
# ---------------------------------------------------------------------------


def test_append_never_rewrites_old_lines(tmp_path):
    path = tmp_path / "decisions.jsonl"
    first = {"ts": "1", "selected": ["a"]}
    second = {"ts": "2", "selected": ["b"]}
    ledger.append_decision(first, path)
    before = path.read_bytes()
    ledger.append_decision(second, path)
    after = path.read_bytes()

    assert after.startswith(before)
    lines = [json.loads(line) for line in after.decode("utf-8").splitlines()]
    assert lines == [first, second]


def test_append_is_line_atomic_under_concurrency(tmp_path):
    path = tmp_path / "decisions.jsonl"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: ledger.append_decision(
                    {"index": index, "pad": "x" * 200}, path
                ),
                range(40),
            )
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 40
    parsed = [json.loads(line) for line in lines]
    assert sorted(row["index"] for row in parsed) == list(range(40))


def test_read_decisions_skips_broken_lines_and_limits_from_the_end(tmp_path):
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"n": 1}\nnot json\n\n{"n": 2}\n{"n": 3}\n', encoding="utf-8")
    assert [row["n"] for row in ledger.read_decisions(path=path)] == [1, 2, 3]
    assert [row["n"] for row in ledger.read_decisions(limit=2, path=path)] == [2, 3]
    assert ledger.read_decisions(path=tmp_path / "missing.jsonl") == []


# ---------------------------------------------------------------------------
# CLI approve
# ---------------------------------------------------------------------------


def fake_http(monkeypatch, routes):
    calls = []

    def _request(method, path, payload=None, timeout=120):
        calls.append((method, path, payload))
        handler = routes.get((method, path))
        if handler is None:
            raise AssertionError(f"예상하지 않은 호출: {method} {path}")
        return handler(payload) if callable(handler) else handler

    monkeypatch.setattr(graph_cli, "_request", _request)
    return calls


def test_approve_records_even_with_missing_critic(monkeypatch, tmp_path, capsys):
    state = sample_state(tmp_path, with_critic=False)
    monkeypatch.setenv(profile.TASTE_DIR_ENV, str(tmp_path / "taste"))
    fake_http(
        monkeypatch,
        {
            ("POST", "/threads/t-1/state"): {},
            ("POST", "/threads/t-1/runs"): {},
            ("GET", "/threads/t-1/state"): state,
        },
    )

    assert (
        graph_cli.main(
            [
                "approve",
                "t-1",
                "--graph",
                "entry_pack",
                "--values",
                '{"selection":["b"]}',
                "--tag",
                "차분함",
                "--tag",
                "여백",
                "--reason",
                "빈 공간이 편안해서",
            ]
        )
        == 0
    )

    records = ledger.read_decisions(path=tmp_path / "taste" / "decisions.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["selected"] == ["b"]
    assert record["reason_tags"] == ["차분함", "여백"]
    assert record["reason_text"] == "빈 공간이 편안해서"
    assert record["critic_top"] == []
    assert record["agreement_top1"] is None
    assert "취향 기록" in capsys.readouterr().err


def test_approve_still_succeeds_when_recording_fails(monkeypatch, tmp_path, capsys):
    state = sample_state(tmp_path, with_critic=False)
    blocked = tmp_path / "blocked"
    blocked.write_text("파일이라 폴더가 될 수 없다", encoding="utf-8")
    monkeypatch.setenv(profile.TASTE_DIR_ENV, str(blocked / "taste"))
    fake_http(
        monkeypatch,
        {
            ("POST", "/threads/t-1/state"): {},
            ("POST", "/threads/t-1/runs"): {},
            ("GET", "/threads/t-1/state"): state,
        },
    )

    assert (
        graph_cli.main(
            [
                "approve",
                "t-1",
                "--graph",
                "entry_pack",
                "--values",
                '{"selection":["a"]}',
            ]
        )
        == 0
    )
    assert "취향 기록을 남기지 못했습니다" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI decisions / taste
# ---------------------------------------------------------------------------


def seed_ledger(tmp_path, count=2):
    path = tmp_path / "decisions.jsonl"
    for index in range(count):
        record = {
            "ts": f"2026-11-2{index}T00:00:00Z",
            "profile": "web",
            "platform": "99designs",
            "options": [
                {"id": "a", "style": "forest-paper"},
                {"id": "b", "style": "star-editorial"},
            ],
            "selected": ["a"] if index == 0 else ["b"],
            "rejected": ["b"] if index == 0 else ["a"],
            "reason_tags": ["차분함"] if index == 0 else ["대담함"],
            "reason_text": "편안해서" if index == 0 else "",
            "agreement_top1": True if index == 0 else None,
        }
        ledger.append_decision(record, path)
    return path


def test_decisions_cli_json_returns_last_n(monkeypatch, tmp_path, capsys):
    seed_ledger(tmp_path)
    monkeypatch.setenv(profile.TASTE_DIR_ENV, str(tmp_path))
    assert graph_cli.main(["--json", "decisions", "--last", "1"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["selected"] == ["b"]


def test_decisions_cli_text_is_plain_korean(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(profile.TASTE_DIR_ENV, str(tmp_path))
    assert graph_cli.main(["decisions"]) == 0
    assert "취향 기록 없음" in capsys.readouterr().out

    seed_ledger(tmp_path)
    graph_cli.main(["decisions", "--last", "1"])
    out = capsys.readouterr().out
    assert "이유:" in out
    assert "고름:" in out


def test_taste_cli_summarizes_and_creates_empty_profile(monkeypatch, tmp_path, capsys):
    seed_ledger(tmp_path, count=3)
    monkeypatch.setenv(profile.TASTE_DIR_ENV, str(tmp_path))
    assert graph_cli.main(["taste"]) == 0
    out = capsys.readouterr().out

    assert "취향 기록 3건" in out
    assert "자주 고른 태그" in out
    assert "스타일 승률" in out
    assert "비평가 1순위와 내 선택 일치" in out
    assert "초기 단계" in out

    stored = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert set(stored) == {
        "version",
        "updated_at",
        "decisions",
        "tag_weights",
        "style_winrate",
        "feature_bias",
        "confidence",
        "notes",
    }
    assert stored["decisions"] == 0
    assert stored["confidence"] == "early"


def test_taste_cli_json_shape(monkeypatch, tmp_path, capsys):
    seed_ledger(tmp_path, count=2)
    monkeypatch.setenv(profile.TASTE_DIR_ENV, str(tmp_path))
    assert graph_cli.main(["--json", "taste"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decisions"] == 2
    assert payload["confidence"] == "early"
    assert payload["tag_ranking"] == [
        {"tag": "대담함", "count": 1},
        {"tag": "차분함", "count": 1},
    ]
    assert payload["styles"]["forest-paper"] == {
        "wins": 1,
        "appearances": 2,
        "rate": 0.5,
    }
    assert payload["agreement"] == {"agree": 1, "compared": 1, "rate": 1.0}
    assert payload["profile"]["path"].endswith("profile.json")


def test_summary_does_not_invent_agreement(tmp_path):
    path = seed_ledger(tmp_path, count=1)
    summary = ledger.summarize(ledger.read_decisions(path=path))
    assert summary["agreement"] == {"agree": 1, "compared": 1, "rate": 1.0}

    empty = ledger.summarize([])
    assert empty["agreement"] == {"agree": 0, "compared": 0, "rate": None}
    assert empty["confidence"] == "early"
    assert "취향 기록이 아직 없습니다" in ledger.format_summary(empty)


def test_profile_load_keeps_user_edits(tmp_path):
    path = tmp_path / "profile.json"
    first = profile.load_profile(path)
    assert first["decisions"] == 0

    path.write_text(
        json.dumps({**first, "decisions": 12, "notes": "손으로 고침"}),
        encoding="utf-8",
    )
    loaded = profile.load_profile(path)
    assert loaded["decisions"] == 12
    assert loaded["notes"] == "손으로 고침"


def test_profile_load_tolerates_broken_json(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{broken", encoding="utf-8")
    loaded = profile.load_profile(path)
    assert loaded["decisions"] == 0
    assert "JSONDecodeError" in loaded["notes"]
