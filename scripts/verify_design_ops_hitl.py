"""DesignOpsGraph HITL 수동 검증 스크립트.

외부 결재 시스템 연동은 폐기되었다. 이 스크립트는 그래프가 `publish` 직전에 멈추고,
`approval_status="approved"`로 재개하면 배포 노드가 도는지 확인한다.
실장비(VectorCraft CLI)가 필요한 검증이라 `scripts/`에 둔다.
"""

import os
import sys
import uuid
from pprint import pprint

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graphs.design_ops_graph import build_graph
from langgraph.checkpoint.memory import MemorySaver


def run_hitl_graph():
    print("=== 🚀 DesignOpsGraph HITL 수동 검증 시작 ===")
    graph = build_graph(checkpointer=MemorySaver())

    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    initial_input = {"messages": [], "brief": "민들레 스튜디오 CI 시안", "run_id": f"run_{uuid.uuid4().hex[:6]}"}

    print("\n[1] 그래프 최초 실행 (drafts 생성 → HITL 게이트)")
    for event in graph.stream(initial_input, config):
        for key, value in event.items():
            print(f"👉 이벤트 발생 ({key}): {value}")

    state = graph.get_state(config)
    print("\n[현재 상태 확인]")
    print(f"- 다음 실행될 노드(next): {state.next}")

    if "publish" in state.next:
        print("\n🛑 실행이 중단되었습니다. 수동 승인 후 재개합니다...")
        graph.update_state(config, {"approval_status": "approved", "selected_draft_index": 0})

        print("\n[2] 승인 완료 후 그래프 재개 (resume)")
        for event in graph.stream(None, config):
            for key, value in event.items():
                print(f"👉 이벤트 발생 ({key}): {value}")

        final_state = graph.get_state(config)
        print("\n🎉 최종 메시지:")
        for msg in final_state.values.get("messages", []):
            print(f"[{msg.__class__.__name__}] {msg.content}")
        print("\n[최종 배포 자산]")
        pprint(final_state.values.get("published_assets"))
    else:
        print("\n⚠️ publish 노드 직전에서 멈추지 않았습니다. 배선을 확인하세요.")


if __name__ == "__main__":
    run_hitl_graph()
PY
# move to scripts/
git mv test_hitl.py scripts/verify_design_ops_hitl.py 2>/dev/null || mv test_hitl.py scripts/verify_design_ops_hitl.py
ls scripts/