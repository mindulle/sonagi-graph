import sys
import os
import uuid
import time
import requests
from pprint import pprint
from langchain_core.messages import HumanMessage

# 상위 디렉토리 모듈 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graphs.spike_test_graph import build_graph

# 환경변수에서 Paperclip 설정 로드
PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3000/api")
PAPERCLIP_API_KEY = os.environ.get("PAPERCLIP_API_KEY")
PAPERCLIP_COMPANY_ID = os.environ.get("PAPERCLIP_COMPANY_ID")
PAPERCLIP_AGENT_ID = os.environ.get("PAPERCLIP_AGENT_ID", "spike-agent")

def request_paperclip_approval(message_content: str):
    """
    실제 Paperclip REST API를 호출하여 승인 요청(Approval)을 생성합니다.
    환경변수에 API 키가 없으면 목(Mock) 동작을 수행합니다.
    """
    if not PAPERCLIP_API_KEY or not PAPERCLIP_COMPANY_ID:
        print("⚠️ [Paperclip API] PAPERCLIP_API_KEY 또는 PAPERCLIP_COMPANY_ID가 없어 목(Mock) 모드로 동작합니다.")
        approval_id = str(uuid.uuid4())
        print(f"📎 [Mock] 승인 요청 생성 완료 (Approval ID: {approval_id})")
        time.sleep(2)
        print(f"✅ [Mock] 관리자가 승인했습니다! (Approval ID: {approval_id})")
        return True

    # 실제 API 호출
    url = f"{PAPERCLIP_API_URL}/companies/{PAPERCLIP_COMPANY_ID}/approvals"
    headers = {
        "Authorization": f"Bearer {PAPERCLIP_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "type": "request_board_approval",
        "requestedByAgentId": PAPERCLIP_AGENT_ID,
        "issueIds": [], # 테스트용 빈 배열
        "payload": {
            "title": "중요 작업 실행 승인 요청",
            "summary": message_content,
            "recommendedAction": "해당 작업을 승인하고 다음 노드로 진행합니다.",
            "risks": ["되돌릴 수 없는 작업일 수 있습니다."]
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        approval_data = response.json()
        approval_id = approval_data.get("id")
        print(f"📎 [Paperclip API] 승인 요청 생성 완료 (Approval ID: {approval_id})")
        
        # 승인 상태 폴링 (실제 환경에서는 웹훅이나 재귀 호출 등으로 비동기 처리 권장)
        print("⏳ 관리자의 승인을 기다리는 중 (Polling)...")
        while True:
            poll_url = f"{PAPERCLIP_API_URL}/approvals/{approval_id}"
            poll_resp = requests.get(poll_url, headers=headers)
            poll_resp.raise_for_status()
            
            status = poll_resp.json().get("status")
            if status == "approved":
                print(f"✅ [Paperclip API] 관리자가 승인했습니다! (Approval ID: {approval_id})")
                return True
            elif status in ["rejected", "cancelled"]:
                print(f"❌ [Paperclip API] 승인이 거절되거나 취소되었습니다. (Status: {status})")
                return False
                
            time.sleep(3) # 3초 간격 폴링
            
    except Exception as e:
        print(f"❌ [Paperclip API] 승인 요청 중 오류 발생: {e}")
        return False

def run_hitl_workflow():
    print("=== 🚀 LangGraph HITL x Paperclip API 연동 테스트 시작 ===")
    graph = build_graph()
    
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    
    initial_input = {"messages": [HumanMessage(content="프로젝트 DB를 리셋해줘.")]}
    
    print("\n[1] 그래프 최초 실행 (plan 노드까지 진행)")
    for event in graph.stream(initial_input, config):
        for key, value in event.items():
            print(f"👉 이벤트 발생 ({key}): {value}")
            
    state = graph.get_state(config)
    print("\n[현재 상태 확인]")
    print(f"- 다음 실행될 노드(next): {state.next}")
    
    if "execute" in state.next:
        print("\n🛑 실행이 중단되었습니다. Paperclip 승인 시스템을 호출합니다...")
        
        # 방금 생성된 메시지 내용 추출
        last_message = state.values.get("messages", [])[-1].content
        
        is_approved = request_paperclip_approval(last_message)
        
        if is_approved:
            print("\n[2] 승인 완료 후 그래프 재개 (resume)")
            for event in graph.stream(None, config):
                for key, value in event.items():
                    print(f"👉 이벤트 발생 ({key}): {value}")
                    
            final_state = graph.get_state(config)
            print("\n🎉 최종 메시지:")
            for msg in final_state.values.get("messages", []):
                print(f"[{msg.__class__.__name__}] {msg.content}")
        else:
            print("\n🚨 작업이 취소되어 그래프 실행을 종료합니다.")

if __name__ == "__main__":
    run_hitl_workflow()
