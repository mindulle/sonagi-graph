"""Asset Craft Graph — 자연어 브리프에서 렌더 후보를 만들고, 재고, 사람이 고른다.

앞선 논의의 결론을 그대로 옮긴 구조다. AI는 픽셀을 만지지 않고 **파라미터
공간**에서만 움직인다. 판정은 결정론 린터가 하고, 선택은 사람이 한다.

```
brief ──▶ compose ──▶ render(변주 N) ──▶ critic ──▶ select ──▶ [사람 선택] ──▶ approve
  IR        후보 스타일       SVG+manifest      lint 점수      상위 3안        결정 원장
```

의존은 `sonagi-assets`의 스크립트다(경로는 `SONAGI_ASSETS_DIR`, 기본
`/home/ubuntu/sonagi-assets`). 그래프는 그 저장소의 렌더러·린터·원장을 호출만 한다.

사용::

    from graphs.asset_craft_graph import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": "craft-1"}}
    graph.invoke({"brief": "따뜻하고 고급스러운 인쇄물", "content": "..."}, config)
    # select에서 멈춘다. state.values["options"]를 보고 selection을 넣어 재개.
    graph.update_state(config, {"selection": "minimal-desktop-light"})
    graph.invoke(None, config)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import craft  # noqa: E402

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.graph.message import add_messages  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

ASSETS_DIR = Path(os.environ.get("SONAGI_ASSETS_DIR", "/home/ubuntu/sonagi-assets"))
CATALOG = Path(
    os.environ.get("SONAGI_CATALOG", str(ASSETS_DIR / "test_data/golden/catalog.json"))
)
TOKENS = ASSETS_DIR / "tokens/semantic.json"
OUT_ROOT = Path(os.environ.get("SONAGI_CRAFT_OUT", "/tmp/asset_craft"))


class CraftState(TypedDict):
    messages: Any
    run_id: str
    brief: Any
    ir: Dict[str, Any]
    references: List[Dict[str, Any]]
    scout_report: Dict[str, Any]
    content: str
    recipe: Optional[str]
    variants: List[Dict[str, Any]]
    renders: List[Dict[str, Any]]
    critiques: List[Dict[str, Any]]
    options: List[Dict[str, Any]]
    selection: Optional[str]
    ledger: Optional[str]
    status: str


def _run(args, cwd=None):
    done = subprocess.run(
        [sys.executable, *map(str, args)],
        capture_output=True,
        text=True,
        cwd=str(cwd or ASSETS_DIR),
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"실패: {' '.join(map(str, args))}\n{done.stdout[-800:]}\n{done.stderr[-800:]}"
        )
    return done.stdout


def brief_node(state: CraftState) -> Dict[str, Any]:
    raw = state.get("brief") or state.get("content") or "차분한 랜딩"
    ir = craft.parse_brief(raw)
    return {
        "ir": ir,
        "run_id": state.get("run_id") or uuid.uuid4().hex[:8],
        "status": "brief",
        "messages": [
            AIMessage(
                content=f"브리프: 톤 {', '.join(ir['tones'])} · 매체 {ir['media']}"
            )
        ],
    }


def scout_node(state: CraftState) -> Dict[str, Any]:
    ir = state.get("ir") or craft.parse_brief(state.get("brief", ""))
    catalog = Path(os.environ.get("SONAGI_CATALOG", str(CATALOG)))
    items = json.loads(catalog.read_text()).get("items", [])
    report = craft.scout_references(
        ir, items, limit=int(os.environ.get("SONAGI_CRAFT_REFS", craft.REF_LIMIT))
    )
    shortfall = report.get("shortfall") or []
    notes = "; ".join(row["reason"] for row in shortfall) if shortfall else "쿼터 충족"
    return {
        "references": report["references"],
        "scout_report": report,
        "status": "scouted",
        "messages": [
            AIMessage(
                content=(
                    f"레퍼런스 {len(report['references'])}건 "
                    f"(주제어 {'/'.join(report['terms']) or '없음'}) — {notes}"
                )
            )
        ],
    }


def compose_node(state: CraftState) -> Dict[str, Any]:
    ir = state.get("ir") or craft.parse_brief(state.get("brief", ""))
    styles = craft.load_registry(ASSETS_DIR)
    breakpoints = ("mobile",) if ir.get("media") == "print" else ("desktop",)
    variants = craft.plan_variants(
        ir, styles, limit=3, breakpoints=breakpoints, themes=("light",)
    )
    return {"variants": variants, "status": "composed"}


def render_node(state: CraftState) -> Dict[str, Any]:
    content = state.get("content")
    if not content:
        raise ValueError("content(마크다운 경로 또는 본문)가 필요합니다")
    content_path = Path(content)
    run_dir = OUT_ROOT / state["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)

    if not content_path.is_file():
        content_path = run_dir / "brief.md"
        content_path.write_text(str(content))

    recipe = run_dir / "recipe.json"
    _run(
        [
            ASSETS_DIR / "scripts/import_content.py",
            content_path,
            "--catalog",
            CATALOG,
            "--out",
            recipe,
        ]
    )

    renders = []
    for variant in state["variants"]:
        out = run_dir / variant["id"]
        _run(
            [
                ASSETS_DIR / "scripts/render_recipe.py",
                "--recipe",
                recipe,
                "--style",
                ASSETS_DIR / "styles" / f"{variant['style']}.json",
                "--catalog",
                CATALOG,
                "--tokens",
                TOKENS,
                "--out",
                out,
                "--breakpoint",
                variant["breakpoint"],
            ]
        )
        svg = next(out.glob("*.svg"), None)
        manifest = next(out.glob("*.manifest.json"), None)
        if not svg or not manifest:
            raise RuntimeError(f"렌더 산출물이 없습니다: {out}")
        renders.append({**variant, "svg": str(svg), "manifest": str(manifest)})

    return {"recipe": str(recipe), "renders": renders, "status": "rendered"}


def critic_node(state: CraftState) -> Dict[str, Any]:
    critiques = []
    for render in state.get("renders", []):
        raw = _run(
            [
                ASSETS_DIR / "scripts/lint_render.py",
                "--svg",
                render["svg"],
                "--manifest",
                render["manifest"],
                "--tokens",
                TOKENS,
                "--json",
            ]
        )
        payload = json.loads(raw)
        payload["id"] = render["id"]
        payload["svg"] = render["svg"]
        payload["plan_score"] = render.get("score", 0)
        critiques.append(payload)
    return {"critiques": critiques, "status": "critiqued"}


def select_node(state: CraftState) -> Dict[str, Any]:
    options = craft.pick(state.get("critiques", []), limit=3)
    return {"options": options, "status": "awaiting_selection"}


def approve_node(state: CraftState) -> Dict[str, Any]:
    selection = state.get("selection")
    options = state.get("options") or []
    if not options:
        raise RuntimeError("선택할 후보가 없습니다(전부 FAIL)")
    chosen = next((o for o in options if o["id"] == selection), None)
    if chosen is None:
        chosen = options[0]

    render = next(r for r in state["renders"] if r["id"] == chosen["id"])
    ledger_dir = Path(render["svg"]).parent
    ledger = ledger_dir / "decisions.json"
    args = [
        ASSETS_DIR / "scripts/ledger.py",
        "--svg",
        render["svg"],
        "--manifest",
        render["manifest"],
        "--decision",
        f"style={render['style_id']}: 판정 {chosen['verdict']} · 점수 {chosen['score']}",
    ]
    references = state.get("references") or []
    if references:
        names = ", ".join(str(ref.get("name")) for ref in references[:6])
        roles = ",".join(
            f"{role}:{count}"
            for role, count in (state.get("scout_report") or {})
            .get("filled", {})
            .items()
        )
        suffix = f" [{roles}]" if roles else ""
        args += ["--decision", f"scout={len(references)}건{suffix}: {names}"]
    else:
        args += ["--decision", "scout=0건: 주제어 일치 없음"]
    args += [
        "--by",
        os.environ.get("SONAGI_CRAFT_BY", "craft-graph"),
        "--out",
        str(ledger),
    ]
    _run(args)
    return {
        "selection": chosen["id"],
        "ledger": str(ledger),
        "status": "approved",
        "messages": [
            AIMessage(content=f"선택: {chosen['id']} (점수 {chosen['score']})")
        ],
    }


def build_graph():
    graph = StateGraph(CraftState)
    graph.add_node("brief", brief_node)
    graph.add_node("scout", scout_node)
    graph.add_node("compose", compose_node)
    graph.add_node("render", render_node)
    graph.add_node("critic", critic_node)
    graph.add_node("select", select_node)
    graph.add_node("approve", approve_node)

    graph.add_edge(START, "brief")
    graph.add_edge("brief", "scout")
    graph.add_edge("scout", "compose")
    graph.add_edge("compose", "render")
    graph.add_edge("render", "critic")
    graph.add_edge("critic", "select")
    graph.add_edge("select", "approve")
    graph.add_edge("approve", END)

    return graph.compile(checkpointer=MemorySaver(), interrupt_before=["approve"])


graph = build_graph()
