from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END, MessagesState

# CLIProxyAPI를 거쳐 로컬 모델 사용
llm = ChatOpenAI(model="gemini-3.1-pro-low", temperature=0)

def plan_action(state: MessagesState):
    sys_msg = SystemMessage(content="사용자의 요청을 분석해서 어떻게 해결할지 1~2줄로 계획을 세우세요. 실제 행동은 아직 하지 마세요.")
    response = llm.invoke([sys_msg] + state["messages"])
    return {"messages": [response]}

def execute_action(state: MessagesState):
    sys_msg = SystemMessage(content="위의 계획을 최종적으로 실행에 옮기고 결과를 보고하세요.")
    response = llm.invoke([sys_msg] + state["messages"])
    return {"messages": [response]}

# 그래프 조립
builder = StateGraph(MessagesState)
builder.add_node("plan_action", plan_action)
builder.add_node("execute_action", execute_action)
builder.add_edge(START, "plan_action")
builder.add_edge("plan_action", "execute_action")
builder.add_edge("execute_action", END)

# 사람의 개입 대기(HITL) 지점 명시
graph = builder.compile(interrupt_before=["execute_action"])
