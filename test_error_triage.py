import sys
import os
from langchain_core.messages import HumanMessage

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from graphs.error_triage_graph import build_error_triage_graph

def run_test():
    print("=== 🛠️ Error Triage Workflow 테스트 ===")
    graph = build_error_triage_graph()
    
    # 1. 테스트용 가짜 에러 로그 입력
    fake_error = "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) FATAL: password authentication failed for user 'sonagi'"
    initial_input = {"messages": [HumanMessage(content=fake_error)]}
    
    # 2. 그래프 실행
    print("\n[그래프 실행 시작]")
    for event in graph.stream(initial_input):
        for node_name, state_update in event.items():
            print(f"\n✅ [{node_name}] 노드 실행 완료")
            if "triage_report" in state_update:
                print(state_update["triage_report"])

if __name__ == "__main__":
    run_test()
