import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()

llm = ChatOpenAI(
    model="gemini-3-flash",
    temperature=0,
    base_url=os.getenv("OPENAI_API_BASE", "http://127.0.0.1:38107/v1"),
    api_key=os.getenv("OPENAI_API_KEY", "dummy"),
    max_retries=0,
    timeout=15
)

def test_call(worker_id):
    start = time.time()
    try:
        res = llm.invoke(f"Hello, just say 'Worker {worker_id} OK' and nothing else.")
        elapsed = time.time() - start
        return f"✅ Worker {worker_id} Success: {res.content.strip()} ({elapsed:.2f}s)"
    except Exception as e:
        elapsed = time.time() - start
        return f"❌ Worker {worker_id} Failed: {str(e)[:50]}... ({elapsed:.2f}s)"

print("🚀 Testing CLIProxyAPI Concurrency (Proxy Limit Test)...")
for concurrency in [2, 3, 5]:
    print(f"\n--- Testing with {concurrency} Concurrent Workers ---")
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(test_call, i) for i in range(1, concurrency + 1)]
        for f in as_completed(futures):
            print(f.result())
    print(f"⏱️ Total Batch Time: {time.time() - start_time:.2f}s")
