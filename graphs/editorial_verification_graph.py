import os
import sys
import uuid
import datetime
from pathlib import Path
from typing import Annotated, TypedDict, List, Dict, Any, Optional

from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langchain_core.prompts import ChatPromptTemplate

from graphs.llm import chat_model, wiki_dir

# ---------------------------------------------------------------------------
# 1. State & Pydantic Schema Definitions
# ---------------------------------------------------------------------------

class AcademicCitationEvaluation(BaseModel):
    score: int = Field(description="0 to 100 score on academic rigor, citation validity, and grounding in primary literature")
    has_citations: bool = Field(description="Whether the text explicitly cites papers, books, or primary sources")
    unsupported_claims: List[str] = Field(description="Bold claims or factual assertions lacking citations or evidence")
    hallucination_risks: List[str] = Field(description="Suspicious or unverified citations/claims that might be AI hallucinations")
    summary: str = Field(description="Concise 1-2 sentence assessment of academic grounding")

class LogicalCoherenceEvaluation(BaseModel):
    score: int = Field(description="0 to 100 score on logical reasoning, structural flow, and lack of fallacies")
    fallacies: List[str] = Field(description="Identified logical fallacies (e.g. non-sequitur, hasty generalization, false dilemma)")
    structural_leaps: List[str] = Field(description="Awkward transitions or missing premise-to-conclusion bridges")
    contradictions: List[str] = Field(description="Internal contradictory statements or conflicting propositions")
    summary: str = Field(description="Concise 1-2 sentence assessment of logical structure")

class AestheticStanceEvaluation(BaseModel):
    score: int = Field(description="0 to 100 score on consistency of subjective stance, aesthetic philosophy, and artistic worldview")
    identified_stance: str = Field(description="The primary aesthetic/philosophical perspective identified in the text")
    stance_drifts: List[str] = Field(description="Points where the author contradicts their own aesthetic stance or drifts off tone")
    voice_consistency: str = Field(description="Evaluation of authorial voice, persona, and stylistic harmony")
    summary: str = Field(description="Concise 1-2 sentence assessment of subjective and aesthetic consistency")

class DiagramEvaluation(BaseModel):
    score: int = Field(description="0 to 100 score on diagram clarity, semantic parity with text, visual balance, and dual-coding standard")
    has_diagrams: bool = Field(description="Whether the document contains embedded diagrams (e.g. Mermaid, Excalidraw, ASCII)")
    diagram_needed: bool = Field(description="Whether a diagram is strongly recommended due to complex architecture, pipeline, or state machine in the text")
    semantic_discrepancies: List[str] = Field(description="Points where the diagram contradicts the text, or misses key components/sequence steps described in the article")
    visual_flaws: List[str] = Field(description="Visual/layout flaws: overly verbose node text (>5 words), cluttered connections, improper orientation, missing hierarchy")
    corrected_diagram_code: Optional[str] = Field(description="A clean, corrected, and semantically styled Mermaid diagram block (using LR/TD, concise nodes, clear labels)")
    summary: str = Field(description="Concise 1-2 sentence assessment of diagram quality and visual representation")

class EditorialState(TypedDict):
    title: str
    content: str
    target_stance: Optional[str]  # e.g. "미니멀리즘 기능주의 비평", "기술 인문학적 에세이"
    academic_critique: Optional[Dict[str, Any]]
    logical_critique: Optional[Dict[str, Any]]
    aesthetic_critique: Optional[Dict[str, Any]]
    diagram_critique: Optional[Dict[str, Any]]
    total_score: int
    verdict: str  # "PASS" (80+), "NEEDS_REVISION" (50-79), "REJECT" (<50)
    final_report: str
    saved_path: Optional[str]
    status: str

# ---------------------------------------------------------------------------
# 2. LLM Helper
# ---------------------------------------------------------------------------
def get_llm(model_name: Optional[str] = None, temperature: float = 0.1):
    """에디토리얼 프록시 클라이언트. 모델은 EDITORIAL_LLM_MODEL로 바꾼다."""
    return chat_model("EDITORIAL_LLM_MODEL", default_model=model_name, temperature=temperature)


REVIEW_DIR = Path(wiki_dir()) / "03_Resources" / "Editorial_Reviews"

# ---------------------------------------------------------------------------
# 3. Node Implementations
# ---------------------------------------------------------------------------

def academic_citation_critic(state: EditorialState) -> Dict[str, Any]:
    """검증 1: 학술적 근거, 논문/도서 인용의 정합성, 출처 결여 주장 탐지"""
    print("[Critic 1/4] 📚 학술적 인용 및 팩트 근거(Grounding) 검증 중...")
    content = state.get("content", "")
    title = state.get("title", "Untitled")

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 엄격한 학술 에디터이자 논문 검증관입니다.\n"
         "글에서 제시된 주장들이 신뢰할 수 있는 학술 문헌, 공인 논문, 1차 사료(Primary Source)에 기반하고 있는지 검증하세요.\n"
         "CRITERIA:\n"
         "1. 정량적 수치나 과학적/이론적 주장을 하면서 출처(논문, 도서, 통계)를 명시하지 않은 단정적 진술을 색출하세요.\n"
         "2. 환각(Hallucination)으로 의심되는 가짜 학자명, 조작된 논문 제목, 왜곡된 인용을 경고하세요.\n"
         "3. 점수는 0~100점 척도로 엄정하게 평가하세요."),
        ("user", "제목: {title}\n\n본문:\n{content}")
    ])

    llm = get_llm()
    evaluator = prompt | llm.with_structured_output(AcademicCitationEvaluation)

    try:
        res = evaluator.invoke({"title": title, "content": content})
        critique = res.model_dump()
        print(f"  -> 학술 점수: {critique['score']}/100 | 인용 유무: {critique['has_citations']}")
    except Exception as e:
        print(f"  -> ⚠️ 학술 검증 중 오류: {e}")
        critique = {
            "score": 50,
            "has_citations": False,
            "unsupported_claims": [f"검증 오류 발생: {e}"],
            "hallucination_risks": [],
            "summary": "학술 검증 호출 실패로 기본 점수 적용"
        }

    return {"academic_critique": critique}


def logical_coherence_critic(state: EditorialState) -> Dict[str, Any]:
    """검증 2: 툴민(Toulmin) 논증 모델 기반 논리 구조, 비약 및 모순 탐지"""
    print("[Critic 2/4] ⚖️ 논리 구조 및 논증 건전성(Logic & Structure) 검증 중...")
    content = state.get("content", "")
    title = state.get("title", "Untitled")

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 분석 철학자이자 논리학 에디터입니다.\n"
         "글의 전제(Premise)와 주장(Claim), 논거(Warrant) 사이의 인과적 결합과 흐름을 엄격히 분석하세요.\n"
         "CRITERIA:\n"
         "1. 논리적 오류(성급한 일반화, 인과관계 오류, 순환논증, 허수아비 공격, 흑백논리 등)를 구체적으로 지목하세요.\n"
         "2. 문단과 문단 사이의 논리적 비약(Non-sequitur)이나 어색한 연결 다리를 식별하세요.\n"
         "3. 글 내부에서 서로 충돌하는 자기모순 명제가 있는지 찾아내세요.\n"
         "4. 점수는 0~100점으로 산출하세요."),
        ("user", "제목: {title}\n\n본문:\n{content}")
    ])

    llm = get_llm()
    evaluator = prompt | llm.with_structured_output(LogicalCoherenceEvaluation)

    try:
        res = evaluator.invoke({"title": title, "content": content})
        critique = res.model_dump()
        print(f"  -> 논리 점수: {critique['score']}/100 | 감지된 오류: {len(critique['fallacies'])}개")
    except Exception as e:
        print(f"  -> ⚠️ 논리 검증 중 오류: {e}")
        critique = {
            "score": 50,
            "fallacies": [f"검증 오류: {e}"],
            "structural_leaps": [],
            "contradictions": [],
            "summary": "논리 검증 호출 실패로 기본 점수 적용"
        }

    return {"logical_critique": critique}


def aesthetic_stance_critic(state: EditorialState) -> Dict[str, Any]:
    """검증 3: 예술작품/비평에서의 주관적 일관성, 심미안 및 세계관 표류(Stance Drift) 검증"""
    print("[Critic 3/4] 🎭 주관적 일관성 및 미학적 관점(Aesthetic Stance) 검증 중...")
    content = state.get("content", "")
    title = state.get("title", "Untitled")
    target_stance = state.get("target_stance") or "저자가 표방한 미학적/비평적 고유 관점"

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 권위 있는 예술 비평가이자 인문학 에디터입니다.\n"
         "예술, 디자인, 에세이 등 주관이 중요한 글에서 '저자가 일관된 철학적/미학적 태도'를 끝까지 견지하고 있는지 평가하세요.\n"
         "목표 관점/스탠스: {target_stance}\n\n"
         "CRITERIA:\n"
         "1. 초반에 제시한 미학적 잣대(예: 바우하우스 기능주의, 디컨스트럭션, 미니멀리즘 등)가 중후반부에서 맥락 없이 뒤집히는 '관점 표류(Stance Drift)'를 감지하세요.\n"
         "2. 감상이나 가치 평가의 잣대가 자의적이거나 모순되지 않고 일관된 고유 목소리(Authorial Voice)를 유지하는지 확인하세요.\n"
         "3. 점수는 0~100점으로 산출하세요."),
        ("user", "제목: {title}\n\n본문:\n{content}")
    ])

    llm = get_llm()
    evaluator = prompt | llm.with_structured_output(AestheticStanceEvaluation)

    try:
        res = evaluator.invoke({"title": title, "content": content, "target_stance": target_stance})
        critique = res.model_dump()
        print(f"  -> 미학/주관 점수: {critique['score']}/100 | 식별된 관점: {critique['identified_stance'][:30]}...")
    except Exception as e:
        print(f"  -> ⚠️ 미학 검증 중 오류: {e}")
        critique = {
            "score": 50,
            "identified_stance": "식별 실패",
            "stance_drifts": [f"검증 오류: {e}"],
            "voice_consistency": "알 수 없음",
            "summary": "미학 검증 호출 실패로 기본 점수 적용"
        }

    return {"aesthetic_critique": critique}


def diagram_visual_critic(state: EditorialState) -> Dict[str, Any]:
    """검증 4: 다이어그램 무결성, 본문 ↔ 시각 정합성 및 이중 부호화(Dual Coding) 보정"""
    print("[Critic 4/4] 📐 다이어그램 무결성 및 시각 보정(Diagram & Visual Parity) 검증 중...")
    content = state.get("content", "")
    title = state.get("title", "Untitled")

    prompt = ChatPromptTemplate.from_messages([
        ("system",
         "당신은 정보 시각화 전문가이자 이중 부호화(Dual Coding Theory) 아키텍트입니다.\n"
         "글 안에 포함된 다이어그램(Mermaid, Excalidraw, ASCII 흐름도 등)의 시각적 무결성과 본문 정합성을 엄격히 검증하고 보정하세요.\n\n"
         "CRITERIA:\n"
         "1. [본문 ↔ 다이어그램 정합성]: 다이어그램이 본문에서 설명한 아키텍처/파이프라인 순서나 인과관계를 왜곡하거나, 중요한 단계를 건너뛰지 않았는지 검사하세요.\n"
         "2. [시각적 인지 부하]: 노드 박스 안에 3줄 이상의 긴 줄글이 들어가 가독성을 해치는지 확인하세요 (노드당 단어 5개 이내 권장).\n"
         "3. [레이아웃 방향]: 파이프라인/흐름도는 LR(좌->우), 계층 구조는 TD(상->하) 원칙을 준수하는지 검토하세요.\n"
         "4. [자동 보정 코드 생성]: 다이어그램에 오류나 왜곡이 있거나, 또는 본문에 시스템/워크플로우 설명이 많은데 다이어그램이 아예 없다면, 본문의 핵심을 완벽히 시각화한 정밀 보정본 Mermaid 코드를 반드시 생성하세요.\n"
         "5. 점수는 0~100점으로 산출하세요."),
        ("user", "제목: {title}\n\n본문:\n{content}")
    ])

    llm = get_llm()
    evaluator = prompt | llm.with_structured_output(DiagramEvaluation)

    try:
        res = evaluator.invoke({"title": title, "content": content})
        critique = res.model_dump()
        print(f"  -> 다이어그램 점수: {critique['score']}/100 | 다이어그램 포함: {critique['has_diagrams']} | 보정 코드 생성: {bool(critique.get('corrected_diagram_code'))}")
    except Exception as e:
        print(f"  -> ⚠️ 다이어그램 검증 중 오류: {e}")
        critique = {
            "score": 50,
            "has_diagrams": False,
            "diagram_needed": True,
            "semantic_discrepancies": [f"검증 오류: {e}"],
            "visual_flaws": [],
            "corrected_diagram_code": None,
            "summary": "다이어그램 검증 호출 실패로 기본 점수 적용"
        }

    return {"diagram_critique": critique}


def chief_editor_synthesizer(state: EditorialState) -> Dict[str, Any]:
    """종합: 4대 크리틱 피드백 집계, 종합 스코어카드 및 정밀 교정 리포트 생성"""
    print("[Chief Editor] 🖋️ 4대 크리틱 결과 종합 및 최종 에디토리얼 리포트 합성 중...")

    acad = state.get("academic_critique") or {}
    logic = state.get("logical_critique") or {}
    aes = state.get("aesthetic_critique") or {}
    diag = state.get("diagram_critique") or {}

    s1 = acad.get("score", 0)
    s2 = logic.get("score", 0)
    s3 = aes.get("score", 0)
    s4 = diag.get("score", 0)

    # 4대 영역 가중치: 논리 35%, 미학 25%, 다이어그램 20%, 학술 20%
    total_score = int(round(s2 * 0.35 + s3 * 0.25 + s4 * 0.20 + s1 * 0.20))

    if total_score >= 80:
        verdict = "PASS"
    elif total_score >= 50:
        verdict = "NEEDS_REVISION"
    else:
        verdict = "REJECT"

    title = state.get("title", "Untitled")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    report_lines = [
        f"# 🛡️ Editorial Multi-Dimensional Verification Report",
        f"> **대상 글**: {title}",
        f"> **검증 일시**: {now_str}",
        f"> **종합 판정**: **`{verdict}`** (총점: **{total_score}** / 100점)",
        "",
        "---",
        "",
        "## 📊 4대 다면 평가 스코어카드",
        f"| 평가 영역 | 점수 | 핵심 진단 | 상태 |",
        f"| :--- | :---: | :--- | :---: |",
        f"| **1. 논리 구조 & 논증 건전성** | `{s2}/100` | {logic.get('summary', '-')} | {'✅ 우수' if s2>=80 else '⚠️ 주의' if s2>=50 else '❌ 미달'} |",
        f"| **2. 주관 일관성 & 미학적 스탠스** | `{s3}/100` | {aes.get('summary', '-')} | {'✅ 우수' if s3>=80 else '⚠️ 주의' if s3>=50 else '❌ 미달'} |",
        f"| **3. 다이어그램 & 시각 정합성** | `{s4}/100` | {diag.get('summary', '-')} | {'✅ 우수' if s4>=80 else '⚠️ 주의' if s4>=50 else '❌ 미달'} |",
        f"| **4. 학술 인용 & 1차 근거** | `{s1}/100` | {acad.get('summary', '-')} | {'✅ 우수' if s1>=80 else '⚠️ 주의' if s1>=50 else '❌ 미달'} |",
        "",
        "---",
        "",
        "## 🔍 세부 진단 결과",
        "",
        "### 1. ⚖️ 논리 구조 및 오류 (Logical Fallacies & Leaps)",
    ]

    fallacies = logic.get("fallacies", [])
    if fallacies:
        report_lines.append("**발견된 논리적 오류:**")
        for f in fallacies:
            report_lines.append(f"- ⚠️ {f}")
    else:
        report_lines.append("- ✅ 심각한 논리적 비약이나 오류가 발견되지 않았습니다.")

    contradictions = logic.get("contradictions", [])
    if contradictions:
        report_lines.append("\n**내부 모순 명제:**")
        for c in contradictions:
            report_lines.append(f"- ❗ {c}")

    report_lines.extend([
        "",
        "### 2. 🎭 미학적 관점 및 주관 일관성 (Stance & Worldview)",
        f"- **식별된 저자의 관점:** {aes.get('identified_stance', '미지정')}",
        f"- **보이스 및 문체 일관성:** {aes.get('voice_consistency', '-')}"
    ])

    drifts = aes.get("stance_drifts", [])
    if drifts:
        report_lines.append("\n**관점 표류(Stance Drift) 지점:**")
        for d in drifts:
            report_lines.append(f"- 🌀 {d}")
    else:
        report_lines.append("- ✅ 글 전체에서 미학적/비평적 태도가 일관되게 유지됩니다.")

    report_lines.extend([
        "",
        "### 3. 📐 다이어그램 및 시각 표현 (Diagram & Visual Dual-Coding)",
        f"- **다이어그램 포함 여부:** {'유 (Yes)' if diag.get('has_diagrams') else '무 (No)'}",
        f"- **다이어그램 필요성:** {'필요 (Recommended)' if diag.get('diagram_needed') else '불필요'}"
    ])

    semantic_discrepancies = diag.get("semantic_discrepancies", [])
    if semantic_discrepancies:
        report_lines.append("\n**본문 ↔ 다이어그램 불일치(모순) 지점:**")
        for sd in semantic_discrepancies:
            report_lines.append(f"- ⚠️ {sd}")

    visual_flaws = diag.get("visual_flaws", [])
    if visual_flaws:
        report_lines.append("\n**시각적 레이아웃/인지 부하 결함:**")
        for vf in visual_flaws:
            report_lines.append(f"- 🔍 {vf}")

    corrected_code = diag.get("corrected_diagram_code")
    if corrected_code:
        clean_code = corrected_code.strip()
        if not clean_code.startswith("```"):
            clean_code = f"```mermaid\n{clean_code}\n```"
        report_lines.append("\n**🎨 자동 보정/추천 다이어그램 (Corrected Mermaid Diagram):**")
        report_lines.append(clean_code)

    report_lines.extend([
        "",
        "### 4. 📚 학술적 인용 및 근거 (Academic Grounding)",
        f"- **인용 표기 여부:** {'유 (Yes)' if acad.get('has_citations') else '무 (No - 근거 보강 필요)'}"
    ])

    unsupported = acad.get("unsupported_claims", [])
    if unsupported:
        report_lines.append("\n**출처 없이 단정된 주요 주장:**")
        for u in unsupported:
            report_lines.append(f"- 📌 {u}")

    hallucinations = acad.get("hallucination_risks", [])
    if hallucinations:
        report_lines.append("\n**환각 의심 인용/수치:**")
        for h in hallucinations:
            report_lines.append(f"- 🚨 {h}")

    report_lines.extend([
        "",
        "---",
        "",
        "## 💡 편집장(Chief Editor)의 액션 플랜 & 개선 권고",
    ])

    if verdict == "PASS":
        report_lines.append("1. **발행 권고:** 글의 논리, 미학적 정합성, 시각 다이어그램, 근거가 탄탄하여 바로 발행(Publish) 또는 상위 단계로 승격 가능합니다.")
    elif verdict == "NEEDS_REVISION":
        report_lines.append("1. **부분 수정 권고:** 위에서 지적된 논리적 비약 지점과 출처 결여 주장에 각주 또는 학술 문헌을 보강하세요.")
        report_lines.append("2. **시각 자료 반영:** 제공된 자동 보정 Mermaid 코드를 본문에 삽입하여 독자의 인지 부하를 줄이세요.")
        report_lines.append("3. **관점 정렬:** 표류된 비평적 스탠스를 초반 기조와 일치하도록 다듬으세요.")
    else:
        report_lines.append("1. **전면 재작성 필요:** 논리적 모순과 근거 결여가 심각하여 논증의 골조를 다시 세워야 합니다.")
        if corrected_code:
            report_lines.append("2. **구조 재설계:** 위에서 제안된 다이어그램을 아키텍처 기준으로 삼아 글의 전개 순서를 재정렬하세요.")

    final_report = "\n".join(report_lines)
    print(f" -> 판정 완료: [{verdict}] (총점 {total_score}점)")
    return {
        "total_score": total_score,
        "verdict": verdict,
        "final_report": final_report,
        "status": "editorial_synthesized"
    }


def vault_loader(state: EditorialState) -> Dict[str, Any]:
    """저장 노드: 검증 리포트를 Obsidian Vault에 영구 보관"""
    output_dir = REVIEW_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    title_slug = "".join([c if c.isalnum() or c in ("-", "_") else "_" for c in state.get("title", "Review")])[:30]
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = output_dir / f"Review_{title_slug}_{timestamp}.md"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(state["final_report"])

    print(f"[Vault Loader] 💾 검증 리포트 저장 완료: {filepath}")
    return {"saved_path": str(filepath), "status": "completed"}

# ---------------------------------------------------------------------------
# 4. Graph Construction
# ---------------------------------------------------------------------------

builder = StateGraph(EditorialState)

builder.add_node("academic_critic", academic_citation_critic)
builder.add_node("logical_critic", logical_coherence_critic)
builder.add_node("aesthetic_critic", aesthetic_stance_critic)
builder.add_node("diagram_critic", diagram_visual_critic)
builder.add_node("chief_editor", chief_editor_synthesizer)
builder.add_node("vault_loader", vault_loader)

# Start ➔ 4 Critic 병렬 실행 (Fan-out)
builder.add_edge(START, "academic_critic")
builder.add_edge(START, "logical_critic")
builder.add_edge(START, "aesthetic_critic")
builder.add_edge(START, "diagram_critic")

# 4 Critic ➔ Chief Editor 병합 (Fan-in)
builder.add_edge("academic_critic", "chief_editor")
builder.add_edge("logical_critic", "chief_editor")
builder.add_edge("aesthetic_critic", "chief_editor")
builder.add_edge("diagram_critic", "chief_editor")

# Chief Editor ➔ Vault Loader (HITL 인터럽트 지점)
builder.add_edge("chief_editor", "vault_loader")
builder.add_edge("vault_loader", END)

def build_editorial_verification_graph(checkpointer=None):
    return builder.compile(checkpointer=checkpointer, interrupt_before=["vault_loader"])

# 서버/플랫폼에서는 체크포인터를 주입하지 않는다(플랫폼이 영속성을 담당).
# 로컬 HITL 테스트에서는 build_editorial_verification_graph(checkpointer=MemorySaver())로 호출.
graph = build_editorial_verification_graph()
