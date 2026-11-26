"""T2 (GRAPH-36) 취향 증류 그래프 — 원장 → 규칙·프로필·노트 + HITL 반영 테스트."""

from __future__ import annotations

import json
from pathlib import Path

from graphs import taste_distill_graph as tdg
from graphs.taste import distill


def _record(
    idx: int,
    *,
    selected_id: str = "opt-a",
    style_a: str = "forest-paper",
    style_b: str = "star-editorial",
    tags: list[str] | None = None,
    ws_a: float = 0.6,
    ws_b: float = 0.2,
):
    return {
        "thread_id": f"t-{idx}",
        "selected": [selected_id],
        "reason_tags": tags if tags is not None else ["차분"],
        "reason": "여백이 좋음" if idx % 2 == 0 else "",
        "options": [
            {
                "id": "opt-a",
                "style": style_a,
                "png": f"/tmp/a-{idx}.png",
                "features": {"whitespace_ratio": ws_a, "distinct_color_count": 3},
            },
            {
                "id": "opt-b",
                "style": style_b,
                "png": f"/tmp/b-{idx}.png",
                "features": {"whitespace_ratio": ws_b, "distinct_color_count": 6},
            },
        ],
    }


def test_distill_under_30_uses_rules_only_and_early_confidence():
    records = [_record(i) for i in range(5)]
    out = distill.build_distillation(records, month="2026-11")

    assert out["decisions_count"] == 5
    assert out["confidence"] == "early"
    assert out["profile_proposal"]["feature_bias"] == {}
    assert out["profile_proposal"]["tag_weights"]["차분"] == 1.0
    assert out["profile_proposal"]["style_winrate"]["forest-paper"] == 1.0
    assert out["profile_proposal"]["style_winrate"]["star-editorial"] == 0.0
    assert "30건 미만" in out["note_markdown"]
    assert "/tmp/a-4.png" in out["note_markdown"]
    assert "/tmp/b-4.png" in out["note_markdown"]


def test_distill_30_plus_computes_feature_bias():
    records = [_record(i, ws_a=0.6, ws_b=0.2) for i in range(30)]
    out = distill.build_distillation(records, month="2026-11")

    assert out["decisions_count"] == 30
    assert out["confidence"] == "ready"
    bias = out["profile_proposal"]["feature_bias"]
    assert bias["whitespace_ratio"] == 0.4
    assert bias["distinct_color_count"] == -3.0


def test_hitl_blocks_apply_until_approved(tmp_path: Path):
    dpath = tmp_path / "decisions.jsonl"
    dpath.write_text(
        "\n".join(json.dumps(_record(i), ensure_ascii=False) for i in range(3)) + "\n",
        encoding="utf-8",
    )
    ppath = tmp_path / "profile.json"
    wiki_dir = tmp_path / "wiki"

    state = {
        "decisions_path": str(dpath),
        "profile_path": str(ppath),
        "wiki_dir": str(wiki_dir),
        "month": "2026-11",
    }
    state.update(tdg.load_ledger_node(state))
    state.update(tdg.distill_node(state))

    # 미승인 상태에서는 profile.json과 월간 노트가 생성되지 않아야 한다.
    hitl_pending = tdg.hitl_node(state)
    assert hitl_pending["approval_status"] == "pending"
    assert tdg._route_after_hitl({**state, **hitl_pending}) == "end"
    assert not ppath.exists()
    assert not (wiki_dir / "2026-11.md").exists()

    # 승인 후에만 profile.json과 월간 위키 노트가 기록된다.
    state["approval_status"] = "approved"
    state.update(tdg.hitl_node(state))
    assert tdg._route_after_hitl(state) == "apply"
    applied = tdg.apply_node(state)

    assert Path(applied["applied_profile_path"]).is_file()
    assert Path(applied["applied_note_path"]).is_file()
    saved_profile = json.loads(ppath.read_text(encoding="utf-8"))
    assert saved_profile["decisions"] == 3
    assert saved_profile["confidence"] == "early"
    assert "취향 증류 노트 (2026-11)" in (wiki_dir / "2026-11.md").read_text(encoding="utf-8")


def test_graph_compiles():
    assert tdg.graph is not None
