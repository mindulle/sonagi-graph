import operator
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

# Sonagi 인프라 표준: CLIProxyAPI 게이트웨이 사용
# text-quality: 고품질 추론용 (내부적으로 gemini-pro 계열 매핑)
llm = ChatOpenAI(
    model="text-quality",
    api_key="dummy-key", # CLIProxyAPI는 인증 토큰을 요구하지만 유효성은 무시합니다.
    base_url="http://127.0.0.1:38107/v1"
)

# 1. 상태(State) 정의: 에러 로그 분석에 필요한 상태를 담습니다.
class TriageState(TypedDict):
    messages: Annotated[list, add_messages]
    error_log: str       # 원본 에러 로그
    repo_name: str       # 에러가 발생한 레포지토리 이름
    triage_report: str   # 분석 결과 리포트

# 2. 노드(Node) 정의
def extract_info_node(state: TriageState):
    """사용자의 메시지에서 에러 로그와 레포지토리 정보를 추출"""
    print("🔍 [Node: Extract Info] 에러 정보 추출 중...")
    
    last_msg = state["messages"][-1].content
    
    return {
        "error_log": last_msg,
        "repo_name": "unknown-repo" # 추후 고도화 가능
    }

def analyze_error_node(state: TriageState):
    """에러 로그를 분석하여 원인과 해결책 초안을 작성"""
    print("🧠 [Node: Analyze Error] CLIProxyAPI (text-quality) 로 에러 분석 중...")
    
    error_log = state.get("error_log", "")
    
    system_prompt = """당신은 수석 데브옵스/SRE 엔지니어입니다.
주어진 에러 로그를 분석하여 다음 형식의 간결한 Markdown 리포트를 작성해주세요:

### 🚨 Error Triage Report
- **에러 요약:** (1줄 요약)
- **예상 원인:** (가장 유력한 원인 1~2가지)
- **권장 조치:** (확인해야 할 파일이나 설정, 디버깅 스텝)
"""

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"다음 에러를 분석해주세요:\n\n{error_log}")
    ]
    
    # LLM 실제 호출
    response = llm.invoke(messages)
    report = response.content
    
    return {
        "triage_report": report,
        "messages": [AIMessage(content=report)]
    }

import requests

def notify_result_node(state: TriageState):
    """분석 결과를 n8n 웹훅을 통해 Discord/Notion 등으로 전송"""
    print("📡 [Node: Notify Result] n8n 웹훅으로 분석 결과 전송 중...")
    
    report = state.get("triage_report", "")
    
    # n8n으로 보낼 페이로드 구성
    payload = {
        "repo_name": state.get("repo_name", "unknown-repo"),
        "report": report
    }
    
    # 타사 API 단순 연동은 sonagi-infra 원칙에 따라 n8n에게 위임
    # n8n 쪽에 만들어둘 Webhook URL을 호출
    webhook_url = "https://n8n.sonagi.space/webhook/langgraph-triage-result"
    
    try:
        # 타임아웃을 짧게 주어 LangGraph 실행이 막히지 않도록 함
        requests.post(webhook_url, json=payload, timeout=5)
        print("✅ [Node: Notify Result] n8n으로 전송 완료!")
    except Exception as e:
        print(f"⚠️ [Node: Notify Result] n8n 전송 실패 (웹훅 미설정 또는 오류): {e}")
        
    return {}

# 3. 그래프 구성
def build_error_triage_graph():
    builder = StateGraph(TriageState)
    
    builder.add_node("extract_info", extract_info_node)
    builder.add_node("analyze_error", analyze_error_node)
    builder.add_node("notify_result", notify_result_node)
    
    builder.add_edge(START, "extract_info")
    builder.add_edge("extract_info", "analyze_error")
    builder.add_edge("analyze_error", "notify_result")
    builder.add_edge("notify_result", END)
    
    # 가장 가벼운 워크플로우이므로 현재는 체크포인터(HITL) 없이 바로 리포트만 뱉습니다.
    graph = builder.compile()
    return graph

# LangGraph Server용 진입점
graph = build_error_triage_graph()
