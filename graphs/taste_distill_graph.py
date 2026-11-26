"""taste_distill 그래프 — 결정 원장 → 규칙·프로필 갱신안·한국어 노트 (T2 / GRAPH-36).

흐름::

    load_ledger → distill → [HITL 승인] → apply (profile.json + 월간 위키 노트)

사람이 승인하기 전까지는 `profile.json`과 위키 노트를 절대 쓰지 않으며,
브랜드 SSOT(`sonagi-assets`)는 건드리지 않는다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from graphs.taste import distill as taste_distill
from graphs.taste import ledger as taste_ledger
from graphs.taste import asset_prefs as taste_asset_prefs
from graphs.taste import gallery as taste_gallery
from graphs.taste import profile as taste_profile

WIKI_TASTE_DIR_ENV = "SONAGI_WIKI_TASTE_DIR"


class TasteDistillState(TypedDict, total=False):
    decisions_path: str
    profile_path: str
    wiki_dir: str
    month: str
    decisions: List[Dict[str, Any]]
    current_profile: Dict[str, Any]
    decisions_count: int
    confidence: str
    profile_proposal: Dict[str, Any]
    note_markdown: str
    examples: Dict[str, List[Dict[str, str]]]
    approval_status: str
    approved_overrides: Dict[str, Any]
    applied_profile_path: str
    applied_note_path: str
    applied_asset_prefs_path: str
    applied_gallery_html: str
    applied_gallery_png: str
    status: str


def _resolve_wiki_dir(state: Mapping[str, Any], profile_target: Path) -> Path:
    explicit = state.get("wiki_dir") or os.environ.get(WIKI_TASTE_DIR_ENV)
    if explicit:
        return Path(str(explicit)).expanduser()
    return profile_target.parent / "notes"


def load_ledger_node(state: TasteDistillState) -> Dict[str, Any]:
    """원장과 현재 프로필을 읽는다(읽기 전용)."""
    if isinstance(state.get("decisions"), list):
        records = [dict(r) for r in state["decisions"] if isinstance(r, Mapping)]
    else:
        raw_dpath = state.get("decisions_path")
        dpath = Path(raw_dpath) if raw_dpath else taste_ledger.decisions_path()
        records = taste_ledger.read_decisions(path=dpath)

    raw_ppath = state.get("profile_path")
    ppath = Path(raw_ppath) if raw_ppath else taste_profile.profile_path()
    current = taste_profile.load_profile(ppath, create=False)

    return {
        "decisions": records,
        "current_profile": current,
        "status": "loaded",
    }


def distill_node(state: TasteDistillState) -> Dict[str, Any]:
    """원장을 증류해 프로필 갱신안과 한국어 노트를 만든다."""
    records = state.get("decisions") or []
    current = state.get("current_profile") or {}
    out = taste_distill.build_distillation(
        records,
        current_profile=current,
        month=state.get("month") or None,
    )
    return {
        "month": out["month"],
        "decisions_count": out["decisions_count"],
        "confidence": out["confidence"],
        "profile_proposal": out["profile_proposal"],
        "note_markdown": out["note_markdown"],
        "examples": out["examples"],
        "status": "distilled",
    }


def hitl_node(state: TasteDistillState) -> Dict[str, Any]:
    """사람 승인 여부를 확인한다."""
    if state.get("approval_status") == "approved":
        return {"status": "approved"}
    return {"approval_status": "pending", "status": "awaiting_approval"}


def _route_after_hitl(state: TasteDistillState) -> str:
    if state.get("approval_status") == "approved":
        return "apply"
    return "end"


def apply_node(state: TasteDistillState) -> Dict[str, Any]:
    """승인된 프로필 갱신안과 월간 위키 노트를 기록한다."""
    if state.get("approval_status") != "approved":
        return {"status": "skipped_unapproved"}

    raw_ppath = state.get("profile_path")
    ppath = Path(raw_ppath) if raw_ppath else taste_profile.profile_path()
    proposal = dict(state.get("profile_proposal") or {})
    overrides = state.get("approved_overrides")
    if isinstance(overrides, Mapping):
        proposal.update(overrides)

    taste_profile._write_json_atomic(ppath, proposal)

    month = str(state.get("month") or taste_distill._now_month())
    wiki_dir = _resolve_wiki_dir(state, ppath)
    wiki_dir.mkdir(parents=True, exist_ok=True)
    note_path = wiki_dir / f"{month}.md"
    note_text = str(state.get("note_markdown") or "")
    tmp_note = note_path.with_name(note_path.name + ".tmp")
    tmp_note.write_text(note_text, encoding="utf-8")
    os.replace(tmp_note, note_path)

    records = state.get("decisions") or []
    ap_path = ppath.parent / taste_asset_prefs.ASSET_PREFS_NAME
    prefs = taste_asset_prefs.build_asset_prefs(records)
    taste_asset_prefs.save_asset_prefs(prefs, ap_path)

    gallery_html = ""
    gallery_png = ""
    try:
        gal = taste_gallery.build_monthly_gallery(
            records, out_dir=wiki_dir, month=month, prefs=prefs
        )
        gallery_html = gal["html_path"]
        gallery_png = gal["png_path"]
    except ImportError:
        pass

    return {
        "applied_profile_path": str(ppath),
        "applied_note_path": str(note_path),
        "applied_asset_prefs_path": str(ap_path),
        "applied_gallery_html": gallery_html,
        "applied_gallery_png": gallery_png,
        "status": "applied",
    }


def build_taste_distill_graph(checkpointer=None, interrupt_before=None):
    builder = StateGraph(TasteDistillState)
    builder.add_node("load_ledger", load_ledger_node)
    builder.add_node("distill", distill_node)
    builder.add_node("hitl", hitl_node)
    builder.add_node("apply", apply_node)

    builder.add_edge(START, "load_ledger")
    builder.add_edge("load_ledger", "distill")
    builder.add_edge("distill", "hitl")
    builder.add_conditional_edges(
        "hitl",
        _route_after_hitl,
        {"apply": "apply", "end": END},
    )
    builder.add_edge("apply", END)

    gates = ["hitl"] if interrupt_before is None else list(interrupt_before)
    return builder.compile(checkpointer=checkpointer, interrupt_before=gates)


graph = build_taste_distill_graph()
