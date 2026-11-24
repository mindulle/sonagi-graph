import os
import sys
import uuid
import datetime
from pathlib import Path
from typing import Annotated, TypedDict, List, Dict, Any, Optional

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.prompts import ChatPromptTemplate

from graphs.llm import chat_model, wiki_dir

# ---------------------------------------------------------------------------
# 1. State & Pydantic Schemas for Visual Consistency
# ---------------------------------------------------------------------------

class VectorCritiqueResult(BaseModel):
    score: int = Field(description="0 to 100 score on vector graphic and iconography consistency")
    stroke_weight_consistent: bool = Field(description="Whether all vector assets maintain consistent stroke width (e.g. 2px vs 1.5px)")
    style_consistent: bool = Field(description="Whether fill vs outline style is uniformly maintained without arbitrary mixing")
    grid_aligned: bool = Field(description="Whether icons fit standard 24px/8pt bounding box grid")
    detected_flaws: List[str] = Field(description="List of detected vector inconsistencies (e.g. icon A is 1.5px, icon B is 2.5px)")
    corrected_svgs: List[Dict[str, str]] = Field(description="List of corrected and normalized SVG codes with standardized stroke and fill")
    summary: str = Field(description="Concise 1-2 sentence assessment of vector consistency")

class MotionCritiqueResult(BaseModel):
    score: int = Field(description="0 to 100 score on motion dynamics, easing curves, and timing consistency")
    has_linear_motion: bool = Field(description="Whether stiff linear (등속) motion was detected")
    golden_snap_compliant: bool = Field(description="Whether transitions follow Sonagi 85% Golden Snap curve: cubic-bezier(0.16, 1, 0.3, 1)")
    has_stagger_offset: bool = Field(description="Whether group elements use 0.05s sequential stagger offsets")
    detected_flaws: List[str] = Field(description="List of detected motion timing/easing flaws")
    corrected_motion_code: str = Field(description="Production-ready CSS / Framer Motion token code applying 85% Golden Snap and stagger")
    summary: str = Field(description="Concise 1-2 sentence assessment of motion consistency")

class ImageryCritiqueResult(BaseModel):
    score: int = Field(description="0 to 100 score on photography tone, lighting, and color temperature consistency")
    color_temp_consistent: bool = Field(description="Whether color temperature (warm vs cool Kelvin) is harmonized across images")
    lighting_consistent: bool = Field(description="Whether lighting direction and contrast ratios are unified")
    composition_balanced: bool = Field(description="Whether shot sizes (Close-up vs Wide) and visual weight are harmoniously balanced")
    detected_flaws: List[str] = Field(description="List of detected imagery/photo tone and lighting mismatches")
    styling_recommendations: List[str] = Field(description="Actionable photography styling/LUT/color grading calibration steps")
    summary: str = Field(description="Concise 1-2 sentence assessment of photographic consistency")

class VisualDesignState(TypedDict):
    project_name: str
    brand_tokens: Optional[Dict[str, Any]]  # standard stroke, palette, primary ease
    vectors: Optional[List[Dict[str, Any]]]  # [{name, svg_content}]
    motions: Optional[List[Dict[str, Any]]]  # [{name, code_snippet}]
    imagery: Optional[List[Dict[str, Any]]]  # [{name, description, style_notes}]
    vector_critique: Optional[Dict[str, Any]]
    motion_critique: Optional[Dict[str, Any]]
    imagery_critique: Optional[Dict[str, Any]]
    total_score: int
    verdict: str  # "PASS", "NEEDS_REVISION", "REJECT"
    final_report: str
    saved_path: Optional[str]
    status: str

# ---------------------------------------------------------------------------
# 2. LLM Helper
# ---------------------------------------------------------------------------
def get_llm(model_name: Optional[str] = None, temperature: float = 0.1):
    """비주얼 크리틱 프록시 클라이언트. 모델은 VISUAL_LLM_MODEL로 바꾼다."""
    return chat_model("VISUAL_LLM_MODEL", default_model=model_name, temperature=temperature)


REVIEW_DIR = Path(wiki_dir()) / "03_Resources" / "Design_Reviews"

# ---------------------------------------------------------------------------
# 3. Node Implementations
# ---------------------------------------------------------------------------

def vector_design_critic(state: VisualDesignState) -> Dict[str, Any]:
    """검증 1: 벡터 그래픽 & 아이콘의 선 두께, 코너 곡률, Fill/Stroke 스타일 일관성 린팅"""
    print("[Critic 1/3] 📐 벡터 그래픽 & 아이콘 기하학 일관성(Vector Consistency) 검증 중...")
    project_name = state.get("project_name", "Untitled Project")
    vectors = state.get("vectors") or []
    brand_tokens = state.get("brand_tokens") or {"stroke_width": "2.0px", "corner_radius": "4px"}

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 최고 수준의 벡터 그래픽 시스템 엔지니어이자 아이콘 디자이너입니다.\n"
         "제시된 여러 벡터 그래픽(SVG 코드, 아이콘 목록)의 기하학적 통일성을 엄격히 린팅하세요.\n\n"
         "브랜드 표준 토큰: {brand_tokens}\n\n"
         "CRITERIA:\n"
         "1. [획 두께(Stroke Weight)]: 모든 아이콘의 stroke-width가 표준(예: 2px)과 일치하는지 확인하고, 1.5px나 2.5px 등 불일치를 적발하세요.\n"
         "2. [스타일 혼용(Fill vs Stroke)]: 특정 아이콘만 채움(Filled)이고 다른 것은 선(Outline)으로 그려져 시각적 균형이 깨지는지 검사하세요.\n"
         "3. [코너 라운딩(Corner Radius)]: 각진 모서리와 둥근 모서리의 혼용 여부를 감지하세요.\n"
         "4. [자동 보정 SVG 생성]: 발견된 불일치를 해결하여, 모든 아이콘을 표준 stroke-width와 통일된 스타일로 수정한 완전한 SVG 코드를 제공하세요.\n"
         "5. 점수는 0~100점으로 산출하세요."),
        ("user", "프로젝트: {project_name}\n\n벡터 에셋 데이터:\n{vector_data}")
    ])

    llm = get_llm()
    evaluator = prompt | llm.with_structured_output(VectorCritiqueResult)

    try:
        res = evaluator.invoke({
            "project_name": project_name,
            "brand_tokens": str(brand_tokens),
            "vector_data": str(vectors)
        })
        critique = res.model_dump()
        print(f"  -> 벡터 점수: {critique['score']}/100 | 선 두께 일치: {critique['stroke_weight_consistent']} | 감지 결함: {len(critique['detected_flaws'])}건")
    except Exception as e:
        print(f"  -> ⚠️ 벡터 검증 중 오류: {e}")
        critique = {
            "score": 50,
            "stroke_weight_consistent": False,
            "style_consistent": False,
            "grid_aligned": True,
            "detected_flaws": [f"검증 오류: {e}"],
            "corrected_svgs": [],
            "summary": "벡터 검증 호출 실패로 기본 점수 적용"
        }

    return {"vector_critique": critique}


def motion_dynamics_critic(state: VisualDesignState) -> Dict[str, Any]:
    """검증 2: 모션 엔지니어링 헌법(존코바 85% 골든스냅, 0.05s Stagger, 등속 운동 배제) 검증"""
    print("[Critic 2/3] ⚡ 모션 다이내믹스 & 이징 곡선(Motion Consistency) 검증 중...")
    project_name = state.get("project_name", "Untitled Project")
    motions = state.get("motions") or []

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 수석 모션 그래픽스 엔지니어이자 인터랙션 디자이너입니다.\n"
         "제시된 모션 코드(CSS, Framer Motion, Lottie 속성)의 시간성과 탄성 일관성을 엄격히 검증하세요.\n\n"
         "SONAGI MOTION 절대 헌법:\n"
         "1. [등속 운동 배제]: linear, ease, ease-in-out 같은 기계적이고 둔탁한 등속/완만한 곡선이 섞여 있는지 엄단하세요.\n"
         "2. [85% 골든스냅 준수]: 모든 주요 트랜지션은 극단적 가속 후 우아한 감속을 이루는 cubic-bezier(0.16, 1.0, 0.3, 1.0) 또는 [0.16, 1, 0.3, 1]에 정렬되어야 합니다.\n"
         "3. [0.05s Stagger 오프셋]: 여러 요소가 동시에 덜컥 튀어나오지 않고 50ms 시차로 흐르는지 확인하세요.\n"
         "4. [자동 보정 모션 코드]: 감지된 결함을 즉시 대체할 수 있는 완성된 CSS/Framer Motion 토큰 코드를 합성하세요.\n"
         "5. 점수는 0~100점으로 산출하세요."),
        ("user", "프로젝트: {project_name}\n\n모션 명세 데이터:\n{motion_data}")
    ])

    llm = get_llm()
    evaluator = prompt | llm.with_structured_output(MotionCritiqueResult)

    try:
        res = evaluator.invoke({
            "project_name": project_name,
            "motion_data": str(motions)
        })
        critique = res.model_dump()
        print(f"  -> 모션 점수: {critique['score']}/100 | 등속 운동 감지: {critique['has_linear_motion']} | 골든스냅 부합: {critique['golden_snap_compliant']}")
    except Exception as e:
        print(f"  -> ⚠️ 모션 검증 중 오류: {e}")
        critique = {
            "score": 50,
            "has_linear_motion": True,
            "golden_snap_compliant": False,
            "has_stagger_offset": False,
            "detected_flaws": [f"검증 오류: {e}"],
            "corrected_motion_code": "/* 검증 오류로 기본 코드 미생성 */",
            "summary": "모션 검증 호출 실패로 기본 점수 적용"
        }

    return {"motion_critique": critique}


def imagery_tone_critic(state: VisualDesignState) -> Dict[str, Any]:
    """검증 3: 사진, 실사 이미지의 색온도(Kelvin), 조명 방향, 콘트라스트 및 스타일 일관성 검증"""
    print("[Critic 3/3] 📷 사진 & 실사 이미지 톤앤매너(Imagery Consistency) 검증 중...")
    project_name = state.get("project_name", "Untitled Project")
    imagery = state.get("imagery") or []

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 아트 디렉터이자 컬러리스트(Colorist)입니다.\n"
         "여러 컷의 사진, 일러스트, 배너 이미지의 조형적·시각적 톤앤매너 일관성을 엄격히 평가하세요.\n\n"
         "CRITERIA:\n"
         "1. [색온도(Color Temperature)]: 같은 브랜드 접점에서 차가운 쿨톤(6500K)과 노란 웜톤(3200K)이 제각각 튀어 산만함을 유발하는지 감지하세요.\n"
         "2. [조명 방향 & 콘트라스트]: 강한 측면 드라마틱 조명과 플랫한 전면 조명이 무분별하게 섞여 공간감이 붕괴되는지 검사하세요.\n"
         "3. [구도 및 텍스처/그레인]: 피사체 밀도, 샷 사이즈(Close-up vs Wide shot), 필름 그레인/선예도의 균형을 검토하세요.\n"
         "4. [보정 가이드라인]: 이미지들을 동일한 시각적 룩북으로 통합하기 위한 색보정(LUT), 노출 조정, 화각 매칭 권고안을 제시하세요.\n"
         "5. 점수는 0~100점으로 산출하세요."),
        ("user", "프로젝트: {project_name}\n\n이미지 에셋 명세:\n{imagery_data}")
    ])

    llm = get_llm()
    evaluator = prompt | llm.with_structured_output(ImageryCritiqueResult)

    try:
        res = evaluator.invoke({
            "project_name": project_name,
            "imagery_data": str(imagery)
        })
        critique = res.model_dump()
        print(f"  -> 이미지 점수: {critique['score']}/100 | 색온도 일치: {critique['color_temp_consistent']} | 조명 일치: {critique['lighting_consistent']}")
    except Exception as e:
        print(f"  -> ⚠️ 이미지 검증 중 오류: {e}")
        critique = {
            "score": 50,
            "color_temp_consistent": False,
            "lighting_consistent": False,
            "composition_balanced": False,
            "detected_flaws": [f"검증 오류: {e}"],
            "styling_recommendations": [],
            "summary": "이미지 검증 호출 실패로 기본 점수 적용"
        }

    return {"imagery_critique": critique}


def brand_system_synthesizer(state: VisualDesignState) -> Dict[str, Any]:
    """종합: 3대 시각 크리틱 피드백 집계, 종합 스코어카드 및 통합 비주얼 리포트 합성"""
    print("[Brand Synthesizer] 🎨 3대 시각 크리틱 종합 및 자동 보정 에셋 번들 합성 중...")

    vc = state.get("vector_critique") or {}
    mc = state.get("motion_critique") or {}
    ic = state.get("imagery_critique") or {}

    s1 = vc.get("score", 0)
    s2 = mc.get("score", 0)
    s3 = ic.get("score", 0)

    # 가중치: 벡터 35%, 모션 35%, 이미지/사진 30%
    total_score = int(round(s1 * 0.35 + s2 * 0.35 + s3 * 0.30))

    if total_score >= 80:
        verdict = "PASS"
    elif total_score >= 50:
        verdict = "NEEDS_REVISION"
    else:
        verdict = "REJECT"

    project_name = state.get("project_name", "Untitled Design")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    report_lines = [
        f"# 🎨 Visual Design Consistency & Brand Governance Report",
        f"> **프로젝트**: {project_name}",
        f"> **검증 일시**: {now_str}",
        f"> **종합 판정**: **`{verdict}`** (총점: **{total_score}** / 100점)",
        "",
        "---",
        "",
        "## 📊 3대 시각 일관성 스코어카드",
        f"| 디자인 영역 | 점수 | 핵심 진단 | 상태 |",
        f"| :--- | :---: | :--- | :---: |",
        f"| **1. 벡터 그래픽 & 아이콘** | `{s1}/100` | {vc.get('summary', '-')} | {'✅ 우수' if s1>=80 else '⚠️ 주의' if s1>=50 else '❌ 미달'} |",
        f"| **2. 모션 다이내믹스 & 이징** | `{s2}/100` | {mc.get('summary', '-')} | {'✅ 우수' if s2>=80 else '⚠️ 주의' if s2>=50 else '❌ 미달'} |",
        f"| **3. 사진 & 이미지 톤앤매너** | `{s3}/100` | {ic.get('summary', '-')} | {'✅ 우수' if s3>=80 else '⚠️ 주의' if s3>=50 else '❌ 미달'} |",
        "",
        "---",
        "",
        "## 🔍 세부 진단 결과",
        "",
        "### 1. 📐 벡터 그래픽 & 아이콘 (Vector Geometry)",
        f"- **선 두께(Stroke) 일관성:** {'✅ 통일됨' if vc.get('stroke_weight_consistent') else '❌ 불일치 적발'}",
        f"- **스타일(Fill/Outline) 일치:** {'✅ 통일됨' if vc.get('style_consistent') else '❌ 혼용 적발'}",
    ]

    v_flaws = vc.get("detected_flaws", [])
    if v_flaws:
        report_lines.append("\n**발견된 벡터 불협화음:**")
        for vf in v_flaws:
            report_lines.append(f"- ⚠️ {vf}")

    corrected_svgs = vc.get("corrected_svgs", [])
    if corrected_svgs:
        report_lines.append("\n**🛠️ 통일 규격으로 자동 보정된 SVG 에셋:**")
        for item in corrected_svgs:
            svg_name = item.get("name", "normalized_icon")
            svg_code = item.get("svg", item.get("content", ""))
            report_lines.append(f"**`{svg_name}`**:\n```xml\n{svg_code}\n```")

    report_lines.extend([
        "",
        "### 2. ⚡ 모션 다이내믹스 (Motion Dynamics & Easing)",
        f"- **등속(Linear) 운동 혼입:** {'🚨 감지됨 (제거 필요)' if mc.get('has_linear_motion') else '✅ 없음'}",
        f"- **85% 골든스냅 준수 여부:** {'✅ 완벽 정렬' if mc.get('golden_snap_compliant') else '❌ 곡선 위반'}",
        f"- **0.05s Stagger 적용 여부:** {'✅ 적용됨' if mc.get('has_stagger_offset') else '⚠️ 동시 발화(개선 권장)'}",
    ])

    m_flaws = mc.get("detected_flaws", [])
    if m_flaws:
        report_lines.append("\n**발견된 모션 결함:**")
        for mf in m_flaws:
            report_lines.append(f"- ⚠️ {mf}")

    corrected_motion = mc.get("corrected_motion_code")
    if corrected_motion:
        report_lines.append("\n**🛠️ 교체 권고 모션 토큰 코드 (85% Golden Snap & Stagger):**")
        report_lines.append(f"```typescript\n{corrected_motion}\n```")

    report_lines.extend([
        "",
        "### 3. 📷 사진 & 이미지 톤앤매너 (Imagery & Tone)",
        f"- **색온도(Kelvin) 조화:** {'✅ 일치' if ic.get('color_temp_consistent') else '❌ 웜/쿨톤 불일치'}",
        f"- **조명 및 명암 일관성:** {'✅ 일치' if ic.get('lighting_consistent') else '❌ 조명 방향 충돌'}",
        f"- **구도 및 화각 균형:** {'✅ 조화로움' if ic.get('composition_balanced') else '⚠️ 산만함'}",
    ])

    i_flaws = ic.get("detected_flaws", [])
    if i_flaws:
        report_lines.append("\n**발견된 이미지 톤 충돌:**")
        for ifl in i_flaws:
            report_lines.append(f"- ⚠️ {ifl}")

    recs = ic.get("styling_recommendations", [])
    if recs:
        report_lines.append("\n**💡 이미지 톤앤매너 보정 가이드라인:**")
        for r in recs:
            report_lines.append(f"- 📌 {r}")

    report_lines.extend([
        "",
        "---",
        "",
        "## 🏛️ 아트 디렉터(Art Director)의 액션 플랜",
    ])

    if verdict == "PASS":
        report_lines.append("1. **출하 승인:** 벡터 획 두께, 모션 텐션, 이미지 색온도가 완벽한 브랜드 일관성을 이루고 있어 프로덕션에 즉시 배포 가능합니다.")
    elif verdict == "NEEDS_REVISION":
        report_lines.append("1. **에셋 교체:** 위에서 생성된 '자동 보정 SVG'와 '골든스냅 모션 토큰'을 프로젝트에 적용하세요.")
        report_lines.append("2. **색보정 진행:** 튀는 사진 에셋에 권장된 색온도/조명 가이드를 적용하여 재검증을 진행하세요.")
    else:
        report_lines.append("1. **시각 시스템 전면 재정렬 필요:** 선 두께 및 모션 이징이 혼재되어 조형적 질서가 붕괴되었습니다. 디자인 토큰(W3C)을 먼저 바인딩하고 에셋을 일괄 재생성하세요.")

    final_report = "\n".join(report_lines)
    print(f" -> 판정 완료: [{verdict}] (총점 {total_score}점)")
    return {
        "total_score": total_score,
        "verdict": verdict,
        "final_report": final_report,
        "status": "visual_synthesized"
    }


def vault_loader(state: VisualDesignState) -> Dict[str, Any]:
    """저장 노드: 검증 리포트를 Obsidian Vault에 영구 보관"""
    output_dir = REVIEW_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    proj_slug = "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in state.get("project_name", "Design")])[:30]
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = output_dir / f"Review_{proj_slug}_{timestamp}.md"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(state["final_report"])

    print(f"[Vault Loader] 💾 디자인 일관성 리포트 저장 완료: {filepath}")
    return {"saved_path": str(filepath), "status": "completed"}

# ---------------------------------------------------------------------------
# 4. Graph Construction
# ---------------------------------------------------------------------------

builder = StateGraph(VisualDesignState)

builder.add_node("vector_critic", vector_design_critic)
builder.add_node("motion_critic", motion_dynamics_critic)
builder.add_node("imagery_critic", imagery_tone_critic)
builder.add_node("brand_synthesizer", brand_system_synthesizer)
builder.add_node("vault_loader", vault_loader)

# Start ➔ 3대 비주얼 크리틱 병렬 Fan-out
builder.add_edge(START, "vector_critic")
builder.add_edge(START, "motion_critic")
builder.add_edge(START, "imagery_critic")

# 3대 크리틱 ➔ Brand Synthesizer 병합 Fan-in
builder.add_edge("vector_critic", "brand_synthesizer")
builder.add_edge("motion_critic", "brand_synthesizer")
builder.add_edge("imagery_critic", "brand_synthesizer")

# Synthesizer ➔ Vault Loader (HITL 인터럽트 포인트)
builder.add_edge("brand_synthesizer", "vault_loader")
builder.add_edge("vault_loader", END)

# Checkpointer 바인딩 및 interrupt_before 적용
memory = MemorySaver()
graph = builder.compile(checkpointer=memory, interrupt_before=["vault_loader"])
