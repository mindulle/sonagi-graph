import os
import sys
import uuid
import time
import json
import shutil
import sqlite3
import requests
import subprocess
from pathlib import Path
from typing import Annotated, TypedDict, List, Dict, Any, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AIMessage, HumanMessage

# VectorCraft CLI 경로 탐색
VECTORCRAFT_BIN = shutil.which("vectorcraft-cli") or "/usr/local/bin/vectorcraft-cli"

# Paperclip 인프라 설정
PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3100/api")
PAPERCLIP_API_KEY = os.environ.get("PAPERCLIP_API_KEY", "pcp_508e1df1f3ef0ea8512618560aeb1158d374da12292fb5d9")
PAPERCLIP_COMPANY_ID = os.environ.get("PAPERCLIP_COMPANY_ID", "3fa0dfa2-9f91-4002-8012-ac598bbb4761")
PAPERCLIP_AGENT_ID = os.environ.get("PAPERCLIP_AGENT_ID", "481817cd-92e1-40a9-aa04-120b9fa103f7")

# 에셋 스토리지 및 카탈로그 설정
ASSET_HUB_DB_PATH = os.environ.get("ASSET_HUB_DB_PATH", "/home/ubuntu/sonagi-assets/asset_hub.db")
PORTAL_BRAND_DIR = Path("/home/ubuntu/sonagi-design-system/apps/portal/public/brand")
STATIC_BRAND_DIR = Path("/home/ubuntu/sonagi-tools/static/brand")
OUTPUT_TEMP_ROOT = Path("/tmp/design_ops")

class DesignOpsState(TypedDict):
    messages: Annotated[list, add_messages]
    run_id: str
    brief: str
    design_tokens: Dict[str, Any]
    drafts: List[Dict[str, Any]]
    selected_draft_index: int
    approval_status: str  # "pending", "approved", "rejected"
    approval_id: Optional[str]
    feedback: Optional[str]
    published_assets: Dict[str, Any]
    status: str

# -------------------------------------------------------------
# 1. Brief Node: 디자인 토큰 주입 및 시안 명세 정의
# -------------------------------------------------------------
def brief_node(state: DesignOpsState) -> Dict[str, Any]:
    print("🎨 [Node: brief_node] 디자인 토큰 및 시안 아키텍처 바인딩 중...")
    
    run_id = state.get("run_id") or str(uuid.uuid4())[:8]
    brief_text = state.get("brief") or "Mindulle Studio Glass Paperweight Dandelion Seal"
    
    design_tokens = {
        "version": "v3",
        "grid": 8,
        "viewBox": "0 0 400 400",
        "strokes": {
            "primary": 2.0,
            "secondary": 1.0,
            "accent": 1.5,
            "dash_pattern": "4 4"
        },
        "palette": {
            "bg_obsidian": "#060911",
            "bg_dome": "#090d16",
            "border_slate": "#30363d",
            "border_dark": "#1e2430",
            "ice_blue": "#4A90E2",
            "cyan_soft": "#00ffcc",
            "gold_dandelion": "#ffd166",
            "gold_core": "#f59e0b",
            "gold_highlight": "#fff5cc",
            "white": "#ffffff",
            "mono_ink": "#111827",
            "mono_paper": "#fafafa"
        },
        "typography": {
            "serif_family": "Cormorant Garamond",
            "mono_family": "JetBrains Mono"
        },
        "archetypes": [
            {
                "id": "draft_a_faceted",
                "name": "Draft A: Faceted Crystalline Paperweight",
                "description": "8각 크리스탈 파셋 돔 + 16 방사형 홀씨 관모 + 심층 옵시디언 듀얼 아우라",
                "type": "faceted_dome"
            },
            {
                "id": "draft_b_lens",
                "name": "Draft B: Minimalist Optical Lens Seal",
                "description": "원형 볼록 렌즈 구면 굴절 + 12 미니멀 유기 홀씨 + 모던 타이포그래피",
                "type": "optical_lens"
            },
            {
                "id": "draft_c_stamp",
                "name": "Draft C: Heritage Monogram Stamp",
                "description": "2px 단일 선화 모노크롬 스탬프 / 엠보싱 / 왁스 실링용 하이콘트라스트 에디션",
                "type": "heritage_stamp"
            }
        ]
    }
    
    msg = (
        f"📋 [Brief 접수] '{brief_text}'\n"
        f"- Sonagi V3 디자인 토큰 바인딩 완료 (2px 정밀 선화, OKLCH 기반 옵시디언/골드/시안 팔레트, 8px 그리드)\n"
        f"- 3대 시안 아키텍처 설정 완료: Draft A (8각 파셋 돔), Draft B (광학 렌즈형), Draft C (모노크롬 스탬프)"
    )
    
    return {
        "run_id": run_id,
        "design_tokens": design_tokens,
        "status": "brief_bound",
        "messages": [AIMessage(content=msg)]
    }

# -------------------------------------------------------------
# SVG 생성 헬퍼 함수군
# -------------------------------------------------------------
def _generate_svg_faceted_paperweight(tokens: Dict[str, Any]) -> str:
    p = tokens["palette"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400" fill="none">
  <defs>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@700&amp;family=JetBrains+Mono:wght@600&amp;display=swap');
      .seal-serif {{ font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 700; font-size: 21px; letter-spacing: 0.28em; fill: {p["white"]}; }}
      .seal-mono {{ font-family: 'JetBrains Mono', Consolas, monospace; font-weight: 600; font-size: 10px; letter-spacing: 0.22em; fill: {p["gold_dandelion"]}; }}
    </style>
    <path id="upperArc" d="M 54,200 A 146,146 0 1,1 346,200" fill="none" />
    <path id="lowerArc" d="M 64,210 A 142,142 0 0,0 336,210" fill="none" />
    <radialGradient id="glassAura" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="{p["gold_dandelion"]}" stop-opacity="0.18" />
      <stop offset="45%" stop-color="{p["ice_blue"]}" stop-opacity="0.12" />
      <stop offset="75%" stop-color="{p["cyan_soft"]}" stop-opacity="0.04" />
      <stop offset="100%" stop-color="{p["bg_obsidian"]}" stop-opacity="0" />
    </radialGradient>
    <linearGradient id="facetHighlight" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{p["ice_blue"]}" stop-opacity="0.6" />
      <stop offset="50%" stop-color="{p["white"]}" stop-opacity="0.2" />
      <stop offset="100%" stop-color="{p["gold_dandelion"]}" stop-opacity="0.4" />
    </linearGradient>
    <linearGradient id="goldCore" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="{p["gold_highlight"]}" />
      <stop offset="50%" stop-color="{p["gold_dandelion"]}" />
      <stop offset="100%" stop-color="{p["gold_core"]}" />
    </linearGradient>
  </defs>
  <circle cx="200" cy="200" r="192" fill="{p["bg_obsidian"]}" />
  <circle cx="200" cy="200" r="192" fill="url(#glassAura)" />
  <circle cx="200" cy="200" r="188" stroke="{p["border_slate"]}" stroke-width="2" />
  <circle cx="200" cy="200" r="176" stroke="{p["ice_blue"]}" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.8" />
  <circle cx="200" cy="200" r="126" stroke="{p["border_slate"]}" stroke-width="2" />
  <circle cx="200" cy="200" r="120" stroke="{p["border_dark"]}" stroke-width="1" />
  <text class="seal-serif">
    <textPath href="#upperArc" startOffset="50%" text-anchor="middle">MINDULLE STUDIO</textPath>
  </text>
  <text class="seal-mono">
    <textPath href="#lowerArc" startOffset="50%" text-anchor="middle">• SOUL &amp; HARNESS • EST. 2026 •</textPath>
  </text>
  <polygon points="50,193 52,198 57,200 52,202 50,207 48,202 43,200 48,198" fill="{p["gold_dandelion"]}" />
  <polygon points="350,193 352,198 357,200 352,202 350,207 348,202 343,200 348,198" fill="{p["gold_dandelion"]}" />
  <polygon points="200,88 279,121 312,200 279,279 200,312 121,279 88,200 121,121"
           stroke="{p["ice_blue"]}" stroke-width="1.5" fill="none" opacity="0.4" />
  <polygon points="200,104 268,132 296,200 268,268 200,296 132,268 104,200 132,132"
           stroke="url(#facetHighlight)" stroke-width="1" fill="{p["bg_dome"]}" fill-opacity="0.6" />
  <line x1="121" y1="121" x2="200" y2="200" stroke="{p["ice_blue"]}" stroke-width="1" stroke-dasharray="2 3" opacity="0.35" />
  <line x1="279" y1="121" x2="200" y2="200" stroke="{p["ice_blue"]}" stroke-width="1" stroke-dasharray="2 3" opacity="0.35" />
  <line x1="279" y1="279" x2="200" y2="200" stroke="{p["ice_blue"]}" stroke-width="1" stroke-dasharray="2 3" opacity="0.35" />
  <line x1="121" y1="279" x2="200" y2="200" stroke="{p["ice_blue"]}" stroke-width="1" stroke-dasharray="2 3" opacity="0.35" />
  <path d="M 148,148 A 74,74 0 0,1 252,148" stroke="{p["white"]}" stroke-width="2" stroke-linecap="round" opacity="0.5" />
  <circle cx="160" cy="154" r="2" fill="{p["white"]}" opacity="0.8" />
  <g stroke="{p["gold_dandelion"]}" stroke-width="1.2" opacity="0.85">
    <line x1="200" y1="200" x2="200" y2="132" />
    <circle cx="200" cy="132" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="138" x2="194" y2="132" stroke-width="0.8" />
    <line x1="200" y1="138" x2="206" y2="132" stroke-width="0.8" />
    <line x1="200" y1="200" x2="226" y2="137" />
    <circle cx="226" cy="137" r="1.8" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="248" y2="152" />
    <circle cx="248" cy="152" r="2" fill="{p["gold_highlight"]}" />
    <line x1="242" y1="158" x2="245" y2="149" stroke-width="0.8" />
    <line x1="242" y1="158" x2="252" y2="157" stroke-width="0.8" />
    <line x1="200" y1="200" x2="263" y2="174" />
    <circle cx="263" cy="174" r="1.8" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="268" y2="200" />
    <circle cx="268" cy="200" r="2" fill="{p["gold_highlight"]}" />
    <line x1="262" y1="200" x2="268" y2="194" stroke-width="0.8" />
    <line x1="262" y1="200" x2="268" y2="206" stroke-width="0.8" />
    <line x1="200" y1="200" x2="263" y2="226" />
    <circle cx="263" cy="226" r="1.8" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="248" y2="248" />
    <circle cx="248" cy="248" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="226" y2="263" />
    <circle cx="226" cy="263" r="1.8" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="200" y2="268" />
    <circle cx="200" cy="268" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="174" y2="263" />
    <circle cx="174" cy="263" r="1.8" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="152" y2="248" />
    <circle cx="152" cy="248" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="137" y2="226" />
    <circle cx="137" cy="226" r="1.8" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="132" y2="200" />
    <circle cx="132" cy="200" r="2" fill="{p["gold_highlight"]}" />
    <line x1="138" y1="200" x2="132" y2="194" stroke-width="0.8" />
    <line x1="138" y1="200" x2="132" y2="206" stroke-width="0.8" />
    <line x1="200" y1="200" x2="137" y2="174" />
    <circle cx="137" cy="174" r="1.8" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="152" y2="152" />
    <circle cx="152" cy="152" r="2" fill="{p["gold_highlight"]}" />
    <line x1="158" y1="158" x2="148" y2="157" stroke-width="0.8" />
    <line x1="158" y1="158" x2="155" y2="149" stroke-width="0.8" />
    <line x1="200" y1="200" x2="174" y2="137" />
    <circle cx="174" cy="137" r="1.8" fill="{p["gold_highlight"]}" />
  </g>
  <line x1="200" y1="200" x2="200" y2="228" stroke="url(#goldCore)" stroke-width="2.5" stroke-linecap="round" />
  <ellipse cx="200" cy="200" rx="6" ry="10" fill="url(#goldCore)" />
  <ellipse cx="200" cy="200" rx="3" ry="6" fill="{p["white"]}" opacity="0.9" />
  <circle cx="182" cy="170" r="1.5" fill="{p["gold_dandelion"]}" opacity="0.7" />
  <circle cx="218" cy="174" r="1.2" fill="{p["ice_blue"]}" opacity="0.8" />
  <circle cx="178" cy="225" r="1.5" fill="{p["ice_blue"]}" opacity="0.7" />
  <circle cx="222" cy="220" r="1.2" fill="{p["gold_dandelion"]}" opacity="0.8" />
</svg>"""

def _generate_svg_minimalist_lens(tokens: Dict[str, Any]) -> str:
    p = tokens["palette"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400" fill="none">
  <defs>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@600;700&amp;family=JetBrains+Mono:wght@500&amp;display=swap');
      .lens-serif {{ font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 700; font-size: 20px; letter-spacing: 0.32em; fill: {p["white"]}; }}
      .lens-mono {{ font-family: 'JetBrains Mono', Consolas, monospace; font-weight: 500; font-size: 9.5px; letter-spacing: 0.24em; fill: {p["gold_dandelion"]}; }}
    </style>
    <path id="lensUpper" d="M 60,200 A 140,140 0 1,1 340,200" fill="none" />
    <path id="lensLower" d="M 70,210 A 136,136 0 0,0 330,210" fill="none" />
    <radialGradient id="pureLensGlow" cx="42%" cy="40%" r="58%">
      <stop offset="0%" stop-color="{p["white"]}" stop-opacity="0.22" />
      <stop offset="35%" stop-color="{p["gold_dandelion"]}" stop-opacity="0.14" />
      <stop offset="70%" stop-color="{p["ice_blue"]}" stop-opacity="0.08" />
      <stop offset="100%" stop-color="{p["bg_obsidian"]}" stop-opacity="0" />
    </radialGradient>
    <linearGradient id="lensRingGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{p["ice_blue"]}" stop-opacity="0.9" />
      <stop offset="50%" stop-color="{p["gold_dandelion"]}" stop-opacity="0.7" />
      <stop offset="100%" stop-color="{p["ice_blue"]}" stop-opacity="0.4" />
    </linearGradient>
  </defs>
  <circle cx="200" cy="200" r="192" fill="{p["bg_obsidian"]}" />
  <circle cx="200" cy="200" r="192" fill="url(#pureLensGlow)" />
  <circle cx="200" cy="200" r="188" stroke="{p["border_slate"]}" stroke-width="2" />
  <circle cx="200" cy="200" r="182" stroke="url(#lensRingGrad)" stroke-width="1" opacity="0.6" />
  <circle cx="200" cy="200" r="128" stroke="{p["border_slate"]}" stroke-width="1.5" />
  <text class="lens-serif">
    <textPath href="#lensUpper" startOffset="50%" text-anchor="middle">MINDULLE STUDIO</textPath>
  </text>
  <text class="lens-mono">
    <textPath href="#lensLower" startOffset="50%" text-anchor="middle">• THE ARTISAN HARNESS • 2026 •</textPath>
  </text>
  <circle cx="56" cy="200" r="2.5" fill="{p["gold_dandelion"]}" />
  <circle cx="344" cy="200" r="2.5" fill="{p["gold_dandelion"]}" />
  <circle cx="200" cy="200" r="92" stroke="{p["ice_blue"]}" stroke-width="1.2" stroke-dasharray="3 3" opacity="0.5" />
  <circle cx="200" cy="200" r="64" stroke="{p["gold_dandelion"]}" stroke-width="0.8" opacity="0.4" />
  <g stroke="{p["gold_dandelion"]}" stroke-width="1.2" opacity="0.9">
    <line x1="200" y1="200" x2="200" y2="140" />
    <circle cx="200" cy="140" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="242" y2="158" />
    <circle cx="242" cy="158" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="260" y2="200" />
    <circle cx="260" cy="200" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="242" y2="242" />
    <circle cx="242" cy="242" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="200" y2="260" />
    <circle cx="200" cy="260" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="158" y2="242" />
    <circle cx="158" cy="242" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="140" y2="200" />
    <circle cx="140" cy="200" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="158" y2="158" />
    <circle cx="158" cy="158" r="2" fill="{p["gold_highlight"]}" />
    <line x1="200" y1="200" x2="225" y2="145" stroke-width="0.8" opacity="0.7" />
    <line x1="200" y1="200" x2="255" y2="175" stroke-width="0.8" opacity="0.7" />
    <line x1="200" y1="200" x2="255" y2="225" stroke-width="0.8" opacity="0.7" />
    <line x1="200" y1="200" x2="175" y2="255" stroke-width="0.8" opacity="0.7" />
  </g>
  <line x1="200" y1="200" x2="200" y2="224" stroke="{p["gold_core"]}" stroke-width="2" stroke-linecap="round" />
  <circle cx="200" cy="200" r="7" fill="{p["gold_dandelion"]}" />
  <circle cx="198" cy="198" r="2.5" fill="{p["white"]}" />
</svg>"""

def _generate_svg_monogram_stamp(tokens: Dict[str, Any]) -> str:
    p = tokens["palette"]
    ink = p["mono_paper"]
    bg = p["mono_ink"]
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400" fill="none">
  <defs>
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@700&amp;family=JetBrains+Mono:wght@700&amp;display=swap');
      .stamp-serif {{ font-family: 'Cormorant Garamond', Georgia, serif; font-weight: 700; font-size: 22px; letter-spacing: 0.30em; fill: {ink}; }}
      .stamp-mono {{ font-family: 'JetBrains Mono', Consolas, monospace; font-weight: 700; font-size: 11px; letter-spacing: 0.26em; fill: {ink}; }}
    </style>
    <path id="stampUpper" d="M 52,200 A 148,148 0 1,1 348,200" fill="none" />
    <path id="stampLower" d="M 64,212 A 142,142 0 0,0 336,212" fill="none" />
  </defs>
  <circle cx="200" cy="200" r="192" fill="{bg}" />
  <circle cx="200" cy="200" r="188" stroke="{ink}" stroke-width="2.5" />
  <circle cx="200" cy="200" r="180" stroke="{ink}" stroke-width="1" />
  <circle cx="200" cy="200" r="126" stroke="{ink}" stroke-width="2" />
  <text class="stamp-serif">
    <textPath href="#stampUpper" startOffset="50%" text-anchor="middle">MINDULLE STUDIO</textPath>
  </text>
  <text class="stamp-mono">
    <textPath href="#stampLower" startOffset="50%" text-anchor="middle">• ARCHITECT &amp; ARTISAN • 2026 •</textPath>
  </text>
  <polygon points="50,193 52,198 57,200 52,202 50,207 48,202 43,200 48,198" fill="{ink}" />
  <polygon points="350,193 352,198 357,200 352,202 350,207 348,202 343,200 348,198" fill="{ink}" />
  <polygon points="200,90 278,122 310,200 278,278 200,310 122,278 90,200 122,122"
           stroke="{ink}" stroke-width="2" fill="none" />
  <polygon points="200,108 265,135 292,200 265,265 200,292 135,265 108,200 135,135"
           stroke="{ink}" stroke-width="1" fill="none" stroke-dasharray="3 3" />
  <g stroke="{ink}" stroke-width="1.8">
    <line x1="200" y1="200" x2="200" y2="136" />
    <circle cx="200" cy="136" r="2.5" fill="{ink}" />
    <line x1="200" y1="200" x2="245" y2="155" />
    <circle cx="245" cy="155" r="2.5" fill="{ink}" />
    <line x1="200" y1="200" x2="264" y2="200" />
    <circle cx="264" cy="200" r="2.5" fill="{ink}" />
    <line x1="200" y1="200" x2="245" y2="245" />
    <circle cx="245" cy="245" r="2.5" fill="{ink}" />
    <line x1="200" y1="200" x2="200" y2="264" />
    <circle cx="200" cy="264" r="2.5" fill="{ink}" />
    <line x1="200" y1="200" x2="155" y2="245" />
    <circle cx="155" cy="245" r="2.5" fill="{ink}" />
    <line x1="200" y1="200" x2="136" y2="200" />
    <circle cx="136" cy="200" r="2.5" fill="{ink}" />
    <line x1="200" y1="200" x2="155" y2="155" />
    <circle cx="155" cy="155" r="2.5" fill="{ink}" />
  </g>
  <line x1="200" y1="200" x2="200" y2="228" stroke="{ink}" stroke-width="3" stroke-linecap="round" />
  <circle cx="200" cy="200" r="6" fill="{ink}" />
</svg>"""

# -------------------------------------------------------------
# 2. Generator Node: ArtCraft (VectorCraft) 헤드리스 엔진 실행
# -------------------------------------------------------------
def generator_node(state: DesignOpsState) -> Dict[str, Any]:
    print("⚙️ [Node: generator_node] ArtCraft (VectorCraft) 멀티 시안 생성 및 AST 검증 중...")
    
    run_id = state.get("run_id", "default")
    out_dir = OUTPUT_TEMP_ROOT / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    
    tokens = state.get("design_tokens", {})
    archetypes = tokens.get("archetypes", [])
    
    drafts = []
    
    generators = {
        "draft_a_faceted": _generate_svg_faceted_paperweight,
        "draft_b_lens": _generate_svg_minimalist_lens,
        "draft_c_stamp": _generate_svg_monogram_stamp
    }
    
    for idx, arch in enumerate(archetypes):
        arch_id = arch["id"]
        svg_filename = f"{arch_id}.svg"
        vc_filename = f"{arch_id}.vectorcraft"
        pdf_filename = f"{arch_id}.pdf"
        png_filename = f"{arch_id}.png"
        
        svg_path = out_dir / svg_filename
        vc_path = out_dir / vc_filename
        pdf_path = out_dir / pdf_filename
        png_path = out_dir / png_filename
        
        # 1. SVG 파일 작성
        gen_fn = generators.get(arch_id, _generate_svg_faceted_paperweight)
        svg_content = gen_fn(tokens)
        svg_path.write_text(svg_content, encoding="utf-8")
        
        # 2. VectorCraft CLI를 통한 무손실 변환 (.vectorcraft, .pdf, .png)
        # 2-A: SVG -> .vectorcraft (JSON AST)
        cmd_vc = [VECTORCRAFT_BIN, "convert", str(svg_path), str(vc_path)]
        subprocess.run(cmd_vc, check=True, capture_output=True)
        
        # 2-B: SVG -> .pdf (Vector Print Master)
        cmd_pdf = [VECTORCRAFT_BIN, "convert", str(svg_path), str(pdf_path)]
        subprocess.run(cmd_pdf, check=True, capture_output=True)
        
        # 2-C: SVG -> .png (High-Resolution Raster Preview)
        cmd_png = [VECTORCRAFT_BIN, "convert", str(svg_path), str(png_path)]
        subprocess.run(cmd_png, check=True, capture_output=True)
        
        # 3. VectorCraft Info 추출
        cmd_info = [VECTORCRAFT_BIN, "info", str(svg_path)]
        info_res = subprocess.run(cmd_info, capture_output=True, text=True)
        info_json = json.loads(info_res.stdout) if info_res.returncode == 0 else {}
        
        # 4. VectorCraft Bench 성능 측정
        cmd_bench = [VECTORCRAFT_BIN, "bench", str(svg_path)]
        bench_res = subprocess.run(cmd_bench, capture_output=True, text=True)
        bench_out = bench_res.stdout.strip()
        
        draft_item = {
            "index": idx,
            "id": arch_id,
            "name": arch["name"],
            "description": arch["description"],
            "svg_path": str(svg_path),
            "vectorcraft_path": str(vc_path),
            "pdf_path": str(pdf_path),
            "png_path": str(png_path),
            "file_sizes": {
                "svg_bytes": svg_path.stat().st_size,
                "vectorcraft_bytes": vc_path.stat().st_size,
                "pdf_bytes": pdf_path.stat().st_size,
                "png_bytes": png_path.stat().st_size
            },
            "nodes_count": info_json.get("info", {}).get("objects", 0),
            "kinds": info_json.get("kinds", {}),
            "bench": bench_out
        }
        drafts.append(draft_item)
        print(f"  ✓ {arch['name']} 생성 완료 (Nodes: {draft_item['nodes_count']}, Size: {draft_item['file_sizes']['svg_bytes']}B)")
        
    summary_text = (
        f"✨ **ArtCraft (VectorCraft) 멀티 시안 생성 완료 ({len(drafts)}종)**\n\n"
        + "\n".join([
            f"- **[{d['name']}]**\n"
            f"  • 조형 설명: {d['description']}\n"
            f"  • 포맷 규격: SVG({d['file_sizes']['svg_bytes']}B), PDF({d['file_sizes']['pdf_bytes']}B), AST({d['file_sizes']['vectorcraft_bytes']}B), PNG({d['file_sizes']['png_bytes']}B)\n"
            f"  • 성능: {d['bench'].splitlines()[-1] if d['bench'] else 'N/A'}"
            for d in drafts
        ])
    )
    
    return {
        "drafts": drafts,
        "status": "drafts_generated",
        "messages": [AIMessage(content=summary_text)]
    }

# -------------------------------------------------------------
# 3. HITL Checkpoint Node: Paperclip Approval 연동 및 승인 게이트
# -------------------------------------------------------------
def request_paperclip_approval(title: str, summary: str, drafts_summary: str) -> Optional[str]:
    """Paperclip API에 결재(Approval) 요청을 생성하고 ID를 반환"""
    if not PAPERCLIP_API_KEY or not PAPERCLIP_COMPANY_ID:
        print("⚠️ [Paperclip API] 키가 없어 인메모리 HITL로 동작합니다.")
        return str(uuid.uuid4())
        
    url = f"{PAPERCLIP_API_URL}/companies/{PAPERCLIP_COMPANY_ID}/approvals"
    headers = {
        "Authorization": f"Bearer {PAPERCLIP_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "type": "request_board_approval",
        "requestedByAgentId": PAPERCLIP_AGENT_ID,
        "issueIds": [],
        "payload": {
            "title": title,
            "summary": summary,
            "recommendedAction": "디렉터 검토 후 최적의 시안(Draft A/B/C)을 선정하고 영구 배포를 승인합니다.",
            "risks": ["승인 즉시 sonagi-design-system 포털 및 공식 CDN, sonagi-assets 카탈로그에 덮어써집니다."],
            "details": drafts_summary
        }
    }
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=5)
        res.raise_for_status()
        approval_id = res.json().get("id")
        print(f"📎 [Paperclip API] 결재 요청 생성 성공! (Approval ID: {approval_id})")
        return approval_id
    except Exception as e:
        print(f"⚠️ [Paperclip API] 결재 요청 실패: {e}")
        return str(uuid.uuid4())

def hitl_checkpoint_node(state: DesignOpsState) -> Dict[str, Any]:
    print("🛑 [Node: hitl_checkpoint_node] HITL 승인 게이트 도달. Paperclip Approval 상태 검사...")
    
    approval_status = state.get("approval_status", "pending")
    drafts = state.get("drafts", [])
    
    if approval_status == "approved":
        print("✅ [HITL Checkpoint] 승인 완료됨. 배포 단계로 진행합니다.")
        return {"status": "approved"}
        
    drafts_text = "\n".join([f"{d['index']}. {d['name']} - {d['description']}" for d in drafts])
    approval_id = state.get("approval_id") or request_paperclip_approval(
        title="[Design Approval] 민들레 스튜디오 CI 시각 자산 (ArtCraft 벡터 시안)",
        summary="VectorCraft 엔진으로 3가지 시안(8각 파셋형, 미니멀 렌즈형, 모노크롬 스탬프)이 생성되었습니다.",
        drafts_summary=drafts_text
    )
    
    pause_msg = (
        f"⏳ **디렉터 결재 대기 중 (HITL Approval Required)**\n"
        f"- Approval ID: `{approval_id}`\n"
        f"- 검토 대상 시안: Draft A (8각 파셋형), Draft B (미니멀 렌즈형), Draft C (모노크롬 스탬프형)\n"
        f"- 승인 또는 피드백 입력 시 최종 퍼블리시 노드가 가동됩니다."
    )
    
    return {
        "approval_id": approval_id,
        "approval_status": "pending",
        "status": "awaiting_approval",
        "messages": [AIMessage(content=pause_msg)]
    }

# -------------------------------------------------------------
# 4. Publish Node: 포털 배포, sonagi-assets 등록, MinIO 영구화
# -------------------------------------------------------------
def register_to_asset_hub(item_id: str, name: str, ext: str, tags: List[str], cdn_url: str, original_path: str):
    """sonagi-assets의 SQLite DB(asset_hub.db)에 신규 자산 카탈로그 등록"""
    db_path = Path(ASSET_HUB_DB_PATH)
    if not db_path.parent.exists():
        return
        
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS items
                     (id TEXT PRIMARY KEY, name TEXT, ext TEXT, tags TEXT,
                      annotation TEXT, url TEXT, created_at INTEGER,
                      thumbnail_path TEXT, original_path TEXT)""")
        
        now = int(time.time())
        tags_str = json.dumps(tags)
        c.execute(
            """INSERT OR REPLACE INTO items (id, name, ext, tags, annotation, url, created_at, thumbnail_path, original_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, name, ext, tags_str, "Generated via sonagi-graph ArtCraft HITL Pipeline", cdn_url, now, original_path, original_path)
        )
        conn.commit()
        conn.close()
        print(f"📦 [sonagi-assets DB] 자산 등록 완료: {item_id} ({name})")
    except Exception as e:
        print(f"⚠️ [sonagi-assets DB] 등록 중 경고 (DB 접근): {e}")

def publish_node(state: DesignOpsState) -> Dict[str, Any]:
    print("🚀 [Node: publish_node] 최종 승인본 포털 배포 및 sonagi-assets 카탈로그 등록 중...")
    
    drafts = state.get("drafts", [])
    selected_idx = state.get("selected_draft_index", 0)
    
    if not drafts:
        raise ValueError("배포할 시안(drafts)이 존재하지 않습니다.")
        
    if selected_idx >= len(drafts):
        selected_idx = 0
        
    approved_draft = drafts[selected_idx]
    
    PORTAL_BRAND_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_BRAND_DIR.mkdir(parents=True, exist_ok=True)
    
    # 1. 파일 복사 및 배포
    target_svg = PORTAL_BRAND_DIR / "mindulle-studio-seal.svg"
    target_vc = PORTAL_BRAND_DIR / "mindulle-studio-seal.vectorcraft"
    target_pdf = PORTAL_BRAND_DIR / "mindulle-studio-seal.pdf"
    target_png = PORTAL_BRAND_DIR / "mindulle-studio-seal.png"
    
    shutil.copy2(approved_draft["svg_path"], target_svg)
    shutil.copy2(approved_draft["vectorcraft_path"], target_vc)
    shutil.copy2(approved_draft["pdf_path"], target_pdf)
    shutil.copy2(approved_draft["png_path"], target_png)
    
    # 정적 백업 디렉토리에도 동기화
    shutil.copy2(approved_draft["svg_path"], STATIC_BRAND_DIR / "mindulle-studio-seal.svg")
    shutil.copy2(approved_draft["vectorcraft_path"], STATIC_BRAND_DIR / "mindulle-studio-seal.vectorcraft")
    shutil.copy2(approved_draft["pdf_path"], STATIC_BRAND_DIR / "mindulle-studio-seal.pdf")
    shutil.copy2(approved_draft["png_path"], STATIC_BRAND_DIR / "mindulle-studio-seal.png")
    
    # 2. sonagi-assets 카탈로그 등록
    asset_id = str(uuid.uuid4()).upper()
    cdn_url = "https://cdn.sonagi.space/brand/mindulle-studio-seal.svg"
    register_to_asset_hub(
        item_id=asset_id,
        name="민들레 스튜디오 공식 인장 (유리 문진 속 민들레 홀씨)",
        ext="svg",
        tags=["brand", "ci", "seal", "vectorcraft", "mindulle", approved_draft["id"]],
        cdn_url=cdn_url,
        original_path=str(target_svg)
    )
    
    published_assets = {
        "selected_draft": approved_draft["name"],
        "asset_id": asset_id,
        "cdn_url": cdn_url,
        "portal_path": str(target_svg),
        "vectorcraft_path": str(target_vc),
        "pdf_path": str(target_pdf),
        "png_path": str(target_png),
        "file_sizes": {
            "svg": target_svg.stat().st_size,
            "vectorcraft": target_vc.stat().st_size,
            "pdf": target_pdf.stat().st_size,
            "png": target_png.stat().st_size
        }
    }
    
    final_msg = (
        f"🎉 **[배포 완료] 민들레 스튜디오 CI 시각 자산 영구 자산화 성공!**\n\n"
        f"- **선정 시안**: `{approved_draft['name']}` ({approved_draft['description']})\n"
        f"- **공식 CDN 배포**: {cdn_url}\n"
        f"- **포털 런타임**: `{target_svg}`\n"
        f"- **VectorCraft 원본 AST**: `{target_vc}` ({published_assets['file_sizes']['vectorcraft']} bytes)\n"
        f"- **인쇄용 무손실 PDF**: `{target_pdf}` ({published_assets['file_sizes']['pdf']} bytes)\n"
        f"- **sonagi-assets 카탈로그 ID**: `{asset_id}`"
    )
    
    return {
        "published_assets": published_assets,
        "status": "published",
        "messages": [AIMessage(content=final_msg)]
    }

# -------------------------------------------------------------
# 5. Graph Compilation
# -------------------------------------------------------------
def build_graph():
    builder = StateGraph(DesignOpsState)
    
    builder.add_node("brief", brief_node)
    builder.add_node("generator", generator_node)
    builder.add_node("hitl_checkpoint", hitl_checkpoint_node)
    builder.add_node("publish", publish_node)
    
    builder.add_edge(START, "brief")
    builder.add_edge("brief", "generator")
    builder.add_edge("generator", "hitl_checkpoint")
    builder.add_edge("hitl_checkpoint", "publish")
    builder.add_edge("publish", END)
    
    memory = MemorySaver()
    
    # publish 노드 진입 직전에 인터럽트(중단)하여 인간 디렉터의 승인을 받습니다.
    compiled_graph = builder.compile(
        checkpointer=memory,
        interrupt_before=["publish"]
    )
    return compiled_graph

graph = build_graph()
