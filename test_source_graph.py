"""source_verification 그래프 실검증 — 로컬 SearXNG(:8080)와 LLM 프록시가 필요하다.

`pytest`가 수집하지 않도록 `__main__`에서만 실행한다. 예전에는 이 파일이 import되는
순간 그래프를 돌려서, 루트에서 `pytest --collect-only`만 해도 외부 호출이 나갔다.
"""

from dotenv import load_dotenv

from graphs.source_verification_graph import build_source_verification_graph
from langgraph.checkpoint.memory import MemorySaver

graph = build_source_verification_graph(checkpointer=MemorySaver())


def verify():
    load_dotenv()
    config = {"configurable": {"thread_id": "test_run_001"}}

    print("🚀 Starting Source Verification Graph Test...")
    try:
        for event in graph.stream({"status": "init"}, config):
            for node_name, node_state in event.items():
                print(f"\n--- Node: {node_name} completed ---")

        print("\n✅ Graph execution paused or finished.")

        state = graph.get_state(config)
        print("Next node to execute (HITL Pause):", state.next)
    except Exception as e:
        print(f"\n❌ Error during execution: {e}")


if __name__ == "__main__":
    verify()
