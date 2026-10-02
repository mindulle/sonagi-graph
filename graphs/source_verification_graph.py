import operator
import os
import requests
from typing import Annotated, TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END
from langgraph.types import Send
from langgraph.checkpoint.memory import MemorySaver

from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

# 1. State Definition
class VerificationState(TypedDict):
    queries: List[str]
    raw_sources: Annotated[List[Dict[str, Any]], operator.add]
    verified_sources: Annotated[List[Dict[str, Any]], operator.add]
    status: str

class QueryState(TypedDict):
    query: str

# LLM 평가 결과 구조체 (Structured Output)
class VerificationResult(BaseModel):
    trust_score: int = Field(description="0 to 100 score based on reliability and primary source status")
    reason: str = Field(description="A short 1-sentence explanation of why this score was given")

# SearXNG 헬퍼 함수
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")

def fetch_searxng(query: str) -> List[Dict[str, Any]]:
    try:
        res = requests.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])[:5]
        return [{"title": r.get("title"), "url": r.get("url"), "content": r.get("content")} for r in results]
    except Exception as e:
        print(f"[Error] SearXNG search failed: {e}")
        return []

# 2. Nodes
def dispatcher_node(state: VerificationState) -> VerificationState:
    print("[Dispatcher] Setting up source discovery queries...")
    queries = [
        "Micro SaaS acquisition marketplace with verified MRR",
        "Indie hacker profitable projects revenue Stripe",
        "Buy and sell small profitable websites platform"
    ]
    return {"queries": queries, "status": "dispatching"}

def discovery_node(state: QueryState) -> VerificationState:
    query = state["query"]
    print(f"[Discovery] 🕵️‍♂️ Searching sources for: {query}")
    results = fetch_searxng(query)
    
    raw_sources = [{"query": query, "source_data": r} for r in results]
    return {"raw_sources": raw_sources}

def verifier_node(state: VerificationState) -> VerificationState:
    print(f"[Verifier] ⚖️ Evaluating {len(state.get('raw_sources', []))} discovered sources with LLM...")
    raw_sources = state.get("raw_sources", [])
    verified_sources = []
    
    # LLM 및 구조화된 출력 설정 (gemini-3-flash 사용)
    # CLIProxyAPI 엔드포인트를 강제하여 사내 프록시를 경유하도록 설정
    llm = ChatOpenAI(
        model="gemini-3-flash", 
        temperature=0,
        base_url=os.getenv("OPENAI_API_BASE", "http://127.0.0.1:38107/v1"),
        api_key=os.getenv("OPENAI_API_KEY", "dummy"),
        max_retries=0,
        timeout=10 # 무한 대기 방지
    )
    structured_llm = llm.with_structured_output(VerificationResult)
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a strict data evaluator finding reliable platforms for Micro-SaaS M&A and passive income.\n"
                   "CRITERIA:\n"
                   "1. Score 80-100 ONLY IF it is a primary marketplace (e.g., Acquire.com, Flippa) OR a community providing verified Stripe/MRR data.\n"
                   "2. Score < 50 if it is a generic blog post, SEO listicle, YouTube video, or secondary commentary without hard transaction data.\n"
                   "Output a strict JSON with 'trust_score' and 'reason'."),
        ("user", "Title: {title}\nURL: {url}\nSnippet: {content}")
    ])
    
    evaluator = prompt | structured_llm
    
    for item in raw_sources:
        data = item["source_data"]
        try:
            result = evaluator.invoke({
                "title": data.get("title", ""),
                "url": data.get("url", ""),
                "content": data.get("content", "")
            })
            
            item["trust_score"] = result.trust_score
            item["reason"] = result.reason
            
            print(f"  - [{result.trust_score}점] {data.get('title')[:30]}... ({result.reason})")
            
            # 80점 이상인 1급 정보원만 통과
            if result.trust_score >= 80:
                verified_sources.append(item)
        except Exception as e:
            print(f"  - [Error] LLM Eval failed for {data.get('url')[:30]}: {e}")
            
    print(f" -> 🛡️ Passed LLM verification: {len(verified_sources)} sources.")
    return {"verified_sources": verified_sources, "status": "verifying"}

def loader_node(state: VerificationState) -> VerificationState:
    print("[Loader] 💾 Formatting and saving verified sources to llm-wiki/10_Sources...")
    return {"status": "completed"}

# 3. Router
def continue_to_discovery(state: VerificationState):
    return [Send("discovery", {"query": q}) for q in state["queries"]]

# 4. Graph Construction
builder = StateGraph(VerificationState)

builder.add_node("dispatcher", dispatcher_node)
builder.add_node("discovery", discovery_node)
builder.add_node("verifier", verifier_node)
builder.add_node("loader", loader_node)

builder.set_entry_point("dispatcher")
builder.add_conditional_edges("dispatcher", continue_to_discovery, ["discovery"])
builder.add_edge("discovery", "verifier")
builder.add_edge("verifier", "loader")
builder.add_edge("loader", END)

memory = MemorySaver()
graph = builder.compile(checkpointer=memory, interrupt_before=["loader"])
