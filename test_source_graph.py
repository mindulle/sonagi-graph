import os
from dotenv import load_dotenv
from graphs.source_verification_graph import graph

# .env 파일이 있다면 로드 (없어도 하드코딩된 기본값 사용)
load_dotenv()

config = {"configurable": {"thread_id": "test_run_001"}}

print("🚀 Starting Source Verification Graph Test...")
try:
    # 그래프 실행 (초기 상태 주입)
    for event in graph.stream({"status": "init"}, config):
        for node_name, node_state in event.items():
            print(f"\n--- Node: {node_name} completed ---")
            
    print("\n✅ Graph execution paused or finished.")
    
    # Checkpointer 상태 확인
    state = graph.get_state(config)
    print("Next node to execute (HITL Pause):", state.next)
except Exception as e:
    print(f"\n❌ Error during execution: {e}")
