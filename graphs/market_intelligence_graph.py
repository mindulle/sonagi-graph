import operator
from typing import Annotated, TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langgraph.types import Send
import json

# 1. State Definition
class IntelligenceState(TypedDict):
    """시장 조사 파이프라인의 상태를 정의합니다."""
    targets: List[str]  # 조사할 소스나 키워드 목록
    raw_data: Annotated[List[Dict[str, Any]], operator.add]
    analyzed_data: Annotated[List[Dict[str, Any]], operator.add]
    errors: Annotated[List[str], operator.add]
    status: str

# 개별 Collector 에이전트가 받을 하위 상태(Sub-state)
class TargetState(TypedDict):
    target: str

# 2. Nodes
def dispatcher_node(state: IntelligenceState) -> IntelligenceState:
    """
    [Dispatcher Node]
    어떤 소스들을 조사할지 결정하는 분배기 역할을 합니다.
    """
    print("[Dispatcher] Planning research targets...")
    # 실제로는 여기서 LLM이 어떤 키워드를 검색할지 동적으로 리스트를 만듭니다.
    targets = ["naver", "reddit", "aliexpress"] 
    return {"targets": targets, "status": "dispatching"}

def collector_node(state: TargetState) -> IntelligenceState:
    """
    [Collector Node - 동적 생성됨]
    전달받은 단일 타겟(소스)에 대해서만 전담하여 수집을 진행합니다. (여러 개가 병렬로 실행됨)
    """
    target = state["target"]
    print(f"[Collector] 🚀 Fetching data exclusively for target: {target}...")
    
    # Mock data collection
    new_raw_data = [{"source": target, "info": f"sample data from {target}"}]
    
    # Reducer가 있으므로 자신(에이전트)이 수집한 데이터만 반환하면 자동으로 취합(Fan-in)됩니다.
    return {"raw_data": new_raw_data, "status": f"collected_{target}"}

def analyzer_node(state: IntelligenceState) -> IntelligenceState:
    """[Analyzer Node] 수집된 모든 데이터를 분석합니다."""
    print(f"[Analyzer] Processing {len(state.get('raw_data', []))} raw data items...")
    # Mock analysis
    analyzed_data = [{"result": "filtered data", "count": len(state.get('raw_data', []))}]
    return {"analyzed_data": analyzed_data, "status": "analyzing"}

def loader_node(state: IntelligenceState) -> IntelligenceState:
    """[Loader Node] 정제 완료된 데이터를 DB에 적재합니다."""
    print("[Loader] Loading analyzed data into SQLite DB for Metabase...")
    return {"status": "completed"}

# 3. Router (Map-Reduce 맵핑 함수)
def continue_to_collectors(state: IntelligenceState):
    """
    타겟 리스트의 개수만큼 Send 객체를 반환하여, 
    'collector' 노드를 동적으로 여러 개 띄웁니다 (Fan-out).
    """
    return [Send("collector", {"target": t}) for t in state["targets"]]

# 4. Graph Construction
builder = StateGraph(IntelligenceState)

# Add Nodes
builder.add_node("dispatcher", dispatcher_node)
builder.add_node("collector", collector_node)
builder.add_node("analyzer", analyzer_node)
builder.add_node("loader", loader_node)

builder.set_entry_point("dispatcher")

# Dispatcher 이후 조건부 엣지를 통해 Send(동적 병렬 실행)를 발생시킵니다.
builder.add_conditional_edges("dispatcher", continue_to_collectors, ["collector"])

# 여러 개의 collector가 모두 종료되면, 자동으로 analyzer로 넘어갑니다. (Fan-in)
builder.add_edge("collector", "analyzer")
builder.add_edge("analyzer", "loader")
builder.add_edge("loader", END)

def build_market_intelligence_graph(checkpointer=None):
    # Loader(DB 적재) 직전에 실행을 일시 정지하여 사람의 승인(HITL)을 기다립니다.
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["loader"]
    )

graph = build_market_intelligence_graph()
