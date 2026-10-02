import os
import glob
from dotenv import load_dotenv
from graphs.source_verification_graph import graph

load_dotenv()
config = {"configurable": {"thread_id": "test_resume_001"}}

print("🚀 [1/3] Running Graph (Dispatcher -> Discovery -> Verifier)...")
for event in graph.stream({"status": "init"}, config):
    pass # 콘솔 출력 생략

state = graph.get_state(config)
print(f"⏸️ [2/3] HITL PAUSE Triggered! Next node waiting: {state.next}")

print("🟢 [3/3] Human Approved (Resume). Executing Loader...")
for event in graph.stream(None, config):
    for node_name, node_state in event.items():
        print(f"--- Node: {node_name} successfully executed ---")

print("\n🎉 All done! Let's check the generated Vault file:")
