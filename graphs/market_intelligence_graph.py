import operator
from typing import Annotated, TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
import json

# 1. State Definition
class IntelligenceState(TypedDict):
    """
    시장 조사 파이프라인의 상태를 정의합니다.
    """
    raw_data: List[Dict[str, Any]]
    analyzed_data: List[Dict[str, Any]]
    errors: List[str]
    status: str # "collecting", "analyzing", "loading", "completed", "failed"

# 2. Nodes
def collector_node(state: IntelligenceState) -> IntelligenceState:
    """
    [Collector Node]
    네이버 검색어 API, 레딧(RSS), 알리익스프레스 API 등을 호출하여 원시 데이터를 수집합니다.
    """
    print("[Collector] Fetching raw data from various sources...")
    # Mock data collection
    raw_data = [
        {"source": "naver", "keyword": "AI 에이전트", "volume": 15000},
        {"source": "reddit", "topic": "Local LLM trends", "sentiment": "positive"},
        {"source": "aliexpress", "product": "Mini PC for AI", "price": 199.99}
    ]
    return {
        "raw_data": state.get("raw_data", []) + raw_data,
        "status": "collecting"
    }

def analyzer_node(state: IntelligenceState) -> IntelligenceState:
    """
    [Analyzer Node]
    수집된 원시 데이터를 LLM에 통과시켜 제휴 마케팅 글감으로서의 유효성을 판단하고 노이즈를 제거합니다.
    """
    print("[Analyzer] Processing raw data with LLM...")
    raw_data = state.get("raw_data", [])
    analyzed_data = []
    
    # Mock LLM processing
    for item in raw_data:
        # In real scenario, use LangChain LLM to parse and filter
        item["intent_score"] = 0.85 if "AI" in str(item) else 0.4
        item["is_valid"] = item["intent_score"] > 0.7
        if item["is_valid"]:
            analyzed_data.append(item)
            
    return {
        "analyzed_data": analyzed_data,
        "status": "analyzing"
    }

def loader_node(state: IntelligenceState) -> IntelligenceState:
    """
    [Loader Node]
    정제 완료된 데이터를 mindullemonitor2(100.69.255.15) 서버의 SQLite DB에 적재합니다.
    """
    print("[Loader] Loading analyzed data into SQLite DB for Metabase...")
    analyzed_data = state.get("analyzed_data", [])
    # Mock DB insertion
    print(f"-> Inserted {len(analyzed_data)} records into SQLite.")
    
    return {
        "status": "completed"
    }

# 3. Router (Conditional Edges)
def check_quality(state: IntelligenceState) -> str:
    """
    데이터 품질을 체크하여 흐름을 통제하는 Supervisor 역할을 합니다.
    """
    if state.get("errors"):
        return "error"
    if len(state.get("analyzed_data", [])) == 0:
        return "retry_collect"
    return "proceed"

# 4. Graph Construction
builder = StateGraph(IntelligenceState)

# Add Nodes
builder.add_node("collector", collector_node)
builder.add_node("analyzer", analyzer_node)
builder.add_node("loader", loader_node)

# Set Entry Point
builder.set_entry_point("collector")

# Add Edges
builder.add_edge("collector", "analyzer")

# Conditional Routing from Analyzer
builder.add_conditional_edges(
    "analyzer",
    check_quality,
    {
        "proceed": "loader",
        "retry_collect": "collector", # Not enough good data, collect more
        "error": END
    }
)

# End the graph after loading
builder.add_edge("loader", END)

# Compile the graph
graph = builder.compile()
