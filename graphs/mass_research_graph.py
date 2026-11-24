import os
import operator
import requests
from typing import Annotated, List, TypedDict
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# LangGraph & LangChain
from langgraph.graph import StateGraph, START, END
# 최신 LangGraph v1.0 권장사항에 따라 types에서 Send를 가져옵니다.
from langgraph.types import Send
from langchain_openai import ChatOpenAI
from langchain_core.prompts import PromptTemplate

# 검색 도구
from duckduckgo_search import DDGS

# .env 환경변수 로드 (OPENAI_API_KEY, OPENAI_API_BASE 등)
load_dotenv()

# --- 1. 상태(State) 및 Pydantic 모델 정의 ---
class ResearchState(TypedDict):
    query: str
    sub_queries: List[str]
    research_results: Annotated[List[str], operator.add]
    final_report: str

class SubQueryState(TypedDict):
    sub_query: str

class SubQueryModel(BaseModel):
    sub_queries: List[str] = Field(description="메인 쿼리를 분석하여 도출한 3개의 구체적인 하위 리서치 키워드")

# LLM 초기화 (환경변수의 OPENAI_API_KEY 자동 매핑)
# GPT-4o 등 지원 모델을 설정 (LiteLLM/Proxy를 통하는 경우 환경에 맞게)
llm = ChatOpenAI(model="claude-sonnet-4-6", temperature=0.2)

# --- 2. 노드(Node) 함수 정의 ---

def planner_node(state: ResearchState):
    """메인 질문을 3개의 세부 검색어로 분할합니다."""
    print(f"🎯 [Planner] 메인 쿼리 분석 중: {state['query']}")
    
    prompt = PromptTemplate.from_template(
        "당신은 리서치 전문가입니다. 다음 리서치 주제를 3개의 구체적인 하위 검색 키워드(영문)로 분할하세요.\n\n주제: {query}"
    )
    chain = prompt | llm.with_structured_output(SubQueryModel)
    
    result = chain.invoke({"query": state["query"]})
    print(f"   -> 분할된 쿼리: {result.sub_queries}")
    return {"sub_queries": result.sub_queries}


def parallel_research_node(state: SubQueryState):
    """각 세부 검색어로 DuckDuckGo 검색 후, Jina Reader로 마크다운을 추출합니다."""
    query = state["sub_query"]
    print(f"🔍 [Researcher] 리서치 및 Jina Reader 추출 중: {query}")
    
    try:
        # 1. DuckDuckGo 검색 (가장 연관성 높은 1개 URL 추출)
        ddgs = DDGS()
        search_results = list(ddgs.text(query, max_results=1))
        
        if not search_results:
            return {"research_results": [f"[{query}] 검색 결과가 없습니다."]}
            
        target_url = search_results[0]['href']
        print(f"   -> 타겟 URL 발견: {target_url}")
        
        # 2. Jina Reader API 호출
        headers = {}
        jina_api_key = os.getenv("JINA_API_KEY")
        if jina_api_key:
            headers["Authorization"] = f"Bearer {jina_api_key}"
            
        jina_url = f"https://r.jina.ai/{target_url}"
        response = requests.get(jina_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # 결과를 2000자로 제한 (토큰 폭발 방지)
        markdown_content = response.text[:2000]
        
        result_text = f"## Query: {query}\n**Source:** {target_url}\n\n{markdown_content}...\n"
        return {"research_results": [result_text]}
        
    except Exception as e:
        print(f"   -> ❌ 에러 발생 ({query}): {str(e)}")
        return {"research_results": [f"[{query}] 조사 중 에러 발생: {str(e)}"]}


def synthesizer_node(state: ResearchState):
    """수집된 텍스트를 LLM을 이용해 하나의 마크다운 리포트로 합성합니다."""
    print(f"✍️ [Synthesizer] 총 {len(state['research_results'])}개의 추출 데이터 병합 중...")
    
    combined_content = "\n\n---\n\n".join(state["research_results"])
    
    prompt = PromptTemplate.from_template(
        "당신은 Sonagi 시스템의 'Sonagi AI Research Agent'입니다. "
        "다음은 '{query}'에 대한 여러 웹사이트의 마크다운 추출본입니다.\n"
        "이 정보들을 종합하여 깔끔하고 전문적인 하나의 마크다운(Markdown) 최종 리포트를 작성해 주세요.\n"
        "리포트 작성 시 가상의 팀명(예: 테크 리포트 편집팀 등)을 지어내지 말고, "
        "반드시 'Sonagi AI Research Agent'라는 이름만 사용하여 작성자/출처를 표기해 주세요.\n\n"
        "정보:\n{data}"
    )
    
    chain = prompt | llm
    report = chain.invoke({"query": state["query"], "data": combined_content})
    
    return {"final_report": report.content}


# --- 3. 라우팅 로직 (Fan-out) ---
def continue_to_research(state: ResearchState):
    """Planner에서 도출한 쿼리 개수만큼 parallel_research_node를 병렬로 생성"""
    return [Send("parallel_research_node", {"sub_query": q}) for q in state["sub_queries"]]


# --- 4. 그래프(Graph) 빌드 ---
workflow = StateGraph(ResearchState)

workflow.add_node("planner_node", planner_node)
workflow.add_node("parallel_research_node", parallel_research_node)
workflow.add_node("synthesizer_node", synthesizer_node)

workflow.add_edge(START, "planner_node")
workflow.add_conditional_edges("planner_node", continue_to_research, ["parallel_research_node"])
workflow.add_edge("parallel_research_node", "synthesizer_node")
workflow.add_edge("synthesizer_node", END)

mass_research_graph = workflow.compile()

if __name__ == "__main__":
    # 테스트 실행
    print("🚀 대량 리서치 파이프라인 시작...")
    test_state = {"query": "The impact of LangGraph on AI Agents in 2026"}
    
    # 그래프 실행
    result = mass_research_graph.invoke(test_state)
    
    print("\n" + "="*50)
    print("✅ [최종 완성된 마크다운 리포트]")
    print("="*50 + "\n")
    print(result["final_report"])

    # 결과를 llm-wiki로 자동 저장
    import datetime
    report_title = test_state["query"].replace(" ", "_").replace("/", "_")
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = f"/home/ubuntu/llm-wiki/03_Resources/General_Resources/{report_title}_{timestamp}.md"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(result["final_report"])
    print(f"\n📂 리포트가 위키에 저장되었습니다: {file_path}")
