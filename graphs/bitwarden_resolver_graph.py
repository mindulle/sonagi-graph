import json
import subprocess
from typing import TypedDict, Optional
from pydantic import SecretStr

# LangGraph Imports
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

# 1. 상태(State) 정의
class BitwardenState(TypedDict):
    item_name: str
    vault_status: str
    secret_value: Optional[SecretStr]

# 2. 노드 함수들

def check_status_node(state: BitwardenState):
    """현재 Bitwarden CLI의 잠금 상태를 확인합니다."""
    print("🔍 [Bitwarden] 볼트 상태(bw status)를 확인합니다...")
    
    try:
        # 실제 환경에서의 CLI 호출
        result = subprocess.run(["bw", "status"], capture_output=True, text=True)
        if result.returncode == 0:
            status_data = json.loads(result.stdout)
            status = status_data.get("status", "locked")
        else:
            status = "locked"
    except FileNotFoundError:
        # bw CLI가 설치되지 않은 개발/테스트 환경을 위한 Mock 로직
        status = state.get("vault_status", "locked")  # 초기엔 잠겨있다고 가정
        
    print(f"   -> 현재 상태: {status.upper()}")
    return {"vault_status": status.lower()}

def wait_for_unlock_node(state: BitwardenState):
    """
    이 노드에 진입하기 직전(interrupt_before)에 그래프가 완전히 멈춥니다.
    사용자가 승인하여 그래프가 재개(Resume)되면 이 노드가 실행됩니다.
    """
    print("🔓 [Resume] 사용자의 Unlock 승인을 확인했습니다. 상태를 재검증합니다.")
    # 재개 시 특별한 상태 변경 없이 다시 check_status로 돌아가게 만듭니다.
    return {"vault_status": "unlocked_verified"}

def fetch_secret_node(state: BitwardenState):
    """볼트가 언락된 상태일 때, 시크릿을 안전하게 추출합니다."""
    item = state["item_name"]
    print(f"🔑 [Fetch] '{item}' 시크릿 추출 중...")
    
    try:
        # 실제 환경: bw get password "item_name"
        # result = subprocess.run(["bw", "get", "password", item], capture_output=True, text=True)
        # raw_password = result.stdout.strip()
        
        # 테스트를 위한 Mock 데이터
        raw_password = "super_secret_password_from_bw"
    except Exception as e:
        raw_password = "error_fetching_password"

    # ❗ 핵심: 평문 비밀번호를 Pydantic의 SecretStr로 감싸서 상태에 저장합니다.
    secure_secret = SecretStr(raw_password)
    print("   -> 🔒 시크릿 추출 완료 (로그에는 마스킹 처리됨)")
    
    return {"secret_value": secure_secret}

# 3. 라우팅 함수
def route_after_status(state: BitwardenState):
    """상태에 따라 대기할지, 시크릿을 추출할지 결정합니다."""
    if state["vault_status"] == "unlocked":
        return "fetch_secret"
    else:
        return "wait_for_unlock"

# 4. 그래프 빌드 및 컴파일
builder = StateGraph(BitwardenState)

builder.add_node("check_status", check_status_node)
builder.add_node("wait_for_unlock", wait_for_unlock_node)
builder.add_node("fetch_secret", fetch_secret_node)

builder.add_edge(START, "check_status")

# 상태 확인 후 잠겨있으면 대기 노드로, 풀려있으면 추출 노드로 분기
builder.add_conditional_edges(
    "check_status",
    route_after_status,
    {
        "wait_for_unlock": "wait_for_unlock",
        "fetch_secret": "fetch_secret"
    }
)

# 대기 노드에서 깨어나면 무조건 다시 상태를 확인하러 돌아감 (안전장치)
builder.add_edge("wait_for_unlock", "check_status")
builder.add_edge("fetch_secret", END)

# 체크포인터(메모리/DB) 추가
# 실제 프로덕션에서는 Postgres나 SQLite 기반의 AsyncSqliteSaver 등을 사용합니다.
memory = MemorySaver()

# ❗ 핵심: wait_for_unlock 노드 '실행 직전'에 그래프를 무조건 일시 정지(Interrupt)시킴
bitwarden_resolver_graph = builder.compile(
    checkpointer=memory,
    interrupt_before=["wait_for_unlock"]
)

# ----------------- 테스트 시나리오 -----------------
if __name__ == "__main__":
    import uuid
    # 스레드 ID: 대기 상태를 구분하는 고유 세션 ID
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    
    print("\n[STEP 1] 배포 스크립트가 시크릿을 요청합니다 (최초 실행)")
    initial_state = {"item_name": "PROD_DATABASE_URL", "vault_status": "locked"}
    
    # 1차 실행 (여기서 Locked 상태이므로 interrupt에 걸려 멈춤)
    for event in bitwarden_resolver_graph.stream(initial_state, config):
        pass
    
    # 현재 그래프가 멈춰있는지 확인
    state_snapshot = bitwarden_resolver_graph.get_state(config)
    if state_snapshot.next:
        print("\n🛑 [INTERRUPTED] 그래프 실행이 정지되었습니다!")
        print("💡 디스코드에 알림 발송: 'Bitwarden을 Unlock 후 승인 버튼을 눌러주세요.'")
        
        # ---------------------------------------------------------
        # 여기서 스크립트가 종료되거나 다른 일을 해도 됩니다. 상태는 DB에 저장됨.
        # 사용자가 CLI에서 터미널로 `bw unlock`을 수행했다고 가정합니다.
        # ---------------------------------------------------------
        
        print("\n... (사용자가 터미널에서 Unlock 완료 후, 디스코드 승인 버튼 클릭) ...")
        
        print("\n[STEP 2] 승인 신호를 받아 그래프를 재개(Resume)합니다.")
        # 재개할 때, 상태를 업데이트하여 'unlocked' 상태임을 알립니다.
        bitwarden_resolver_graph.update_state(config, {"vault_status": "unlocked"})
        
        # 다시 stream을 호출하면 멈췄던 지점부터 이어서 실행됨
        for event in bitwarden_resolver_graph.stream(None, config):
            pass
            
    final_state = bitwarden_resolver_graph.get_state(config).values
    print("\n✅ [최종 결과 확인]")
    print(f"조회 대상: {final_state['item_name']}")
    # SecretStr의 __str__ 출력 (**********로 마스킹됨)
    print(f"추출된 시크릿: {final_state['secret_value']}")
    # 실제 값을 써야 하는 노드에서 꺼내는 방법
    print(f"실제 값 추출: {final_state['secret_value'].get_secret_value()}")
