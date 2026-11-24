import uuid
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage, HumanMessage

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]

def plan_node(state: AgentState):
    print("🤖 [Node: plan_node] 분석 및 계획 수립 중...")
    return {"messages": [AIMessage(content="중요한 작업(예: DB 삭제 또는 결제)을 수행할 계획입니다. 승인이 필요합니다.")]}

def execute_node(state: AgentState):
    print("🤖 [Node: execute_node] 승인 확인 완료. 위험한 작업 실행 중...")
    return {"messages": [AIMessage(content="작업이 안전하게 완료되었습니다.")]}

def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("execute", execute_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", END)

    # 메모리 세이버가 있어야 스레드 상태를 저장하고 interrupt/resume이 가능합니다.
    
    # execute 노드 진입 직전에 실행을 중단(interrupt)합니다.
    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["execute"]
    )
    return graph

# LangGraph Server용 진입점
graph = build_graph()
