import os
import sys
import uuid
from typing import Annotated, TypedDict, List, Dict, Any, Optional

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate

from graphs.llm import chat_model

# ---------------------------------------------------------------------------
# 1. State & Pydantic Schema Definitions
# ---------------------------------------------------------------------------

class ClicheAnalysis(BaseModel):
    cliches_found: List[str] = Field(description="List of identified abstract clichés, corporate marketing jargon, or empty buzzwords")
    abstract_words: List[str] = Field(description="Words that lack physical/tactile substance (e.g. '열정', '최고의', '진심', '혁신')")
    critique: str = Field(description="Concise 1-2 sentence critique on why the current draft lacks visceral impact")

class SensoryMetaphor(BaseModel):
    tactile_words: List[str] = Field(description="Physical and visceral sensory words (e.g. '1px 침상 서리', '흑요석', '2px 단단한 뿌리', '45도 소나기', '황금 홀씨', '유리 굴절')")
    atmospheric_elements: List[str] = Field(description="Lighting, temperature, or soundscape anchors (e.g. '6500K 창백한 월광', '120BPM 벼락', '3200K 모닝 골드', '차임 공명')")

class CopyOptionItem(BaseModel):
    flavor: str = Field(description="'A안 (담백·절제형)', 'B안 (시적·서정형)', 'C안 (철학·주권형)'")
    title: str = Field(description="Main headline title (under 20 characters)")
    verse: str = Field(description="Poetic subtitle verse (1-2 sentences with visceral imagery)")
    prose: str = Field(description="Detailed narrative prose (2-3 sentences explaining the philosophy)")
    cadence_score: int = Field(description="0 to 100 score on rhythmic breathing and readability")
    rationale: str = Field(description="Why this copy strikes the emotional chord for this specific scene")

class CopywritingOutput(BaseModel):
    options: List[CopyOptionItem] = Field(description="Exactly 3 distinct, high-impact copy options for the director to choose from")

class CopywritingState(TypedDict):
    scene_id: str
    raw_draft: str
    context: str
    cliche_analysis: Optional[Dict[str, Any]]
    sensory_metaphors: Optional[Dict[str, Any]]
    copy_options: Optional[List[Dict[str, Any]]]
    selected_option_index: Optional[int]
    winning_copy: Optional[Dict[str, Any]]
    approval_status: str  # "pending", "approved", "revision_needed"
    status: str

# ---------------------------------------------------------------------------
# 2. LLM Helper
# ---------------------------------------------------------------------------
def get_llm(model_name: Optional[str] = None, temperature: float = 0.3):
    """카피 크래프트 프록시 클라이언트. 모델은 COPYWRITING_LLM_MODEL로 바꾼다."""
    return chat_model("COPYWRITING_LLM_MODEL", default_model=model_name, temperature=temperature)

# ---------------------------------------------------------------------------
# 3. Graph Nodes
# ---------------------------------------------------------------------------

def cliche_filter_node(state: CopywritingState) -> Dict[str, Any]:
    raw_draft = state.get("raw_draft", "")
    scene_id = state.get("scene_id", "Act")
    print(f"🧹 [CopywritingCraft: Node 1] '{scene_id}' 초안 클리셰 및 추상어 탐지 중...")

    llm = get_llm(temperature=0.1)
    structured_llm = llm.with_structured_output(ClicheAnalysis)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """당신은 1인 소프트웨어/디자인 공방 '민들레(Mindulle)'의 수석 문장 검수관입니다.
우리는 상업 광고판의 공허한 카피("최고의 경험", "고객 중심", "열정", "혁신적인 솔루션")를 극도로 혐오합니다.
문장에 만져지지 않는 추상명사나 진부한 클리셰가 있는지 엄격하게 판정하십시오."""),
        ("human", "대상 씬: {scene_id}\n현재 초안:\n{raw_draft}")
    ])

    chain = prompt | structured_llm
    result = chain.invoke({"scene_id": scene_id, "raw_draft": raw_draft})
    print(f"  ✓ 클리셰 검수 완료: 발견된 추상어 {len(result.abstract_words)}개")

    return {
        "cliche_analysis": result.model_dump(),
        "status": "cliche_filtered"
    }

def sensory_injection_node(state: CopywritingState) -> Dict[str, Any]:
    scene_id = state.get("scene_id", "")
    context = state.get("context", "")
    print(f"🌿 [CopywritingCraft: Node 2] '{scene_id}' 오감 감각어 및 소나기 생태계 비유 주입 중...")

    llm = get_llm(temperature=0.2)
    structured_llm = llm.with_structured_output(SensoryMetaphor)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """당신은 소나기(Sonagi) 생태계의 물리적 물성(Physical Metaphors)을 언어로 벼려내는 장인입니다.
추상적인 관념 대신, 손에 쥐어지는 촉각(돌, 얼음, 2px 굳은살, 벼락, 유리 문진)과 빛, 소리 감각어를 도출하십시오.
- 브랜드 슬로건: '기계는 일하고, 사람은 쉽니다.'
- 핵심 상징: 동토의 로제트 뿌리, 45도 소나기 클렌징, 만개하는 황금 홀씨, 8각 투명 크리스탈 문진(IOR 1.52)."""),
        ("human", "씬: {scene_id}\n맥락 정보:\n{context}")
    ])

    chain = prompt | structured_llm
    result = chain.invoke({"scene_id": scene_id, "context": context})
    print(f"  ✓ 오감 감각어 도출: 촉각 {len(result.tactile_words)}개, 대기광학 {len(result.atmospheric_elements)}개")

    return {
        "sensory_metaphors": result.model_dump(),
        "status": "sensory_injected"
    }

def cadence_polisher_node(state: CopywritingState) -> Dict[str, Any]:
    scene_id = state.get("scene_id", "")
    raw_draft = state.get("raw_draft", "")
    cliche_data = state.get("cliche_analysis", {})
    sensory_data = state.get("sensory_metaphors", {})
    print(f"✍️ [CopywritingCraft: Node 3] 시적 운율 및 3색 카피 옵션(담백/서정/주권) 합성 중...")

    llm = get_llm(temperature=0.4)
    structured_llm = llm.with_structured_output(CopywritingOutput)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """당신은 한국 최고 수준의 시네마틱 카피라이터이자 시인입니다.
제거된 클리셰와 주입된 오감 감각어를 바탕으로, 관객의 가슴을 때리는 **정확히 3개의 독보적인 카피 옵션**을 작성하십시오.
반드시 한국어 음수율(글자 수 리듬, 끊어 읽기)이 유려해야 하며, 제목은 20자 미만의 압도적인 한 줄이어야 합니다.

- A안 (담백·절제형): 군더더기 없는 장인의 단단하고 절제된 직설 (미니멀리즘)
- B안 (시적·서정형): 문학적 여운과 비유가 깊은 서정적인 시구 (장인의 온기)
- C안 (철학·주권형): 외부 의존을 끊고 1인 공방의 자립과 인간의 안식을 선언하는 아포리즘"""),
        ("human", """씬: {scene_id}
원래 초안: {raw_draft}
클리셰 분석: {cliche_analysis}
추출된 오감 감각어: {sensory_metaphors}""")
    ])

    chain = prompt | structured_llm
    result = chain.invoke({
        "scene_id": scene_id,
        "raw_draft": raw_draft,
        "cliche_analysis": str(cliche_data),
        "sensory_metaphors": str(sensory_data)
    })
    print(f"  ✓ 3대 카피 옵션 합성 완료!")

    return {
        "copy_options": [opt.model_dump() for opt in result.options],
        "approval_status": "pending",
        "status": "awaiting_director_approval"
    }

def hitl_checkpoint_node(state: CopywritingState) -> Dict[str, Any]:
    print("🛑 [CopywritingCraft: HITL] 감독님 카피 최종 결재 대기...")
    selected_idx = state.get("selected_option_index")
    options = state.get("copy_options", [])

    if selected_idx is not None and 0 <= selected_idx < len(options):
        winning = options[selected_idx]
        print(f"  ✅ [HITL] 감독님 채택 완료: {winning.get('flavor')} - {winning.get('title')}")
        return {
            "winning_copy": winning,
            "approval_status": "approved",
            "status": "approved"
        }

    return {
        "approval_status": "pending",
        "status": "awaiting_director_approval"
    }

def route_after_approval(state: CopywritingState):
    """감독이 옵션을 고르지 않았으면 발행하지 않고 그래프를 끝낸다."""
    return "publish_copy" if state.get("approval_status") == "approved" else END


def publish_copy_node(state: CopywritingState) -> Dict[str, Any]:
    winning = state.get("winning_copy") or {}
    print(f"🎉 [CopywritingCraft: Publish] 공식 카피 확정 완료: {winning.get('title')}")
    return {
        "status": "published"
    }

# ---------------------------------------------------------------------------
# 4. Graph Construction & Compilation
# ---------------------------------------------------------------------------

def build_copywriting_craft_graph(checkpointer=None):
    builder = StateGraph(CopywritingState)

    builder.add_node("cliche_filter", cliche_filter_node)
    builder.add_node("sensory_injection", sensory_injection_node)
    builder.add_node("cadence_polisher", cadence_polisher_node)
    builder.add_node("hitl_checkpoint", hitl_checkpoint_node)
    builder.add_node("publish_copy", publish_copy_node)

    builder.add_edge(START, "cliche_filter")
    builder.add_edge("cliche_filter", "sensory_injection")
    builder.add_edge("sensory_injection", "cadence_polisher")
    builder.add_edge("cadence_polisher", "hitl_checkpoint")
    builder.add_conditional_edges(
        "hitl_checkpoint",
        route_after_approval,
        {"publish_copy": "publish_copy", END: END},
    )
    builder.add_edge("publish_copy", END)

    # 옵션 3개가 준비된 뒤, 감독이 하나를 고르기 전까지 멈춘다 (HITL).
    # 결재 노드(hitl_checkpoint)가 선택 인덱스를 읽어야 하므로 그 직전에서 멈춰야 한다.
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["hitl_checkpoint"]
    )

graph = build_copywriting_craft_graph()
