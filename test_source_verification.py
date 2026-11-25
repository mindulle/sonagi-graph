"""source_verification 그래프 검증 — L2(fixture E2E).

SearXNG(`requests.get`)와 LLM(`ChatOpenAI`)은 픽스처가 대신하고, Vault 쓰기는
`capture_vault_write`가 메모리로 돌린다. 네트워크·API 키·llm-wiki 실저장소 없이 돈다.
LLM 평가는 외부 판단이라 L3(결정론) 대상이 아니다.
"""

from __future__ import annotations

from graphs import source_verification_graph as sv

TRUSTED = "https://acquire.com/listing/1"


def _raw(query: str, url: str, title: str = "source"):
    return {
        "query": query,
        "source_data": {"title": title, "url": url, "content": "mrr 데이터"},
    }


def test_fetch_searxng_limits_to_five_and_maps_contract(fake_searxng):
    """SearXNG 응답은 상위 5건만, title/url/content 세 키로 정규화한다."""
    rows = [
        {
            "title": f"t{index}",
            "url": f"https://e{index}.test",
            "content": "c",
            "raw": 1,
        }
        for index in range(7)
    ]
    calls = fake_searxng({"micro saas": rows})

    results = sv.fetch_searxng("micro saas")

    assert len(results) == 5
    assert all(set(result) == {"title", "url", "content"} for result in results)
    assert calls[0]["url"] == f"{sv.SEARXNG_URL}/search"
    assert calls[0]["params"] == {"q": "micro saas", "format": "json"}
    assert calls[0]["timeout"] == 10


def test_fetch_searxng_swallows_transport_error(fake_searxng):
    """검색 서버가 죽어도 그래프는 멈추지 않는다(빈 목록으로 축소)."""
    fake_searxng(error=ConnectionError("searxng down"))

    assert sv.fetch_searxng("anything") == []


def test_discovery_pairs_each_result_with_its_query(monkeypatch):
    monkeypatch.setattr(
        sv,
        "fetch_searxng",
        lambda query: [{"title": "t", "url": "https://a.test", "content": "c"}],
    )

    state = sv.discovery_node({"query": "q-1"})

    assert state["raw_sources"] == [
        {
            "query": "q-1",
            "source_data": {"title": "t", "url": "https://a.test", "content": "c"},
        }
    ]


def test_verifier_keeps_only_sources_scoring_80_or_more(fake_llm):
    """80점 미만은 신뢰 목록에서 빠지고, 통과 항목에는 점수·사유가 붙는다."""
    created = fake_llm(
        score_map={TRUSTED: (95, "1차 마켓플레이스"), "blog.test": (20, "2차 요약")},
        default=30,
    )
    state = sv.verifier_node(
        {
            "raw_sources": [
                _raw("q", TRUSTED, "Acquire"),
                _raw("q", "https://blog.test/post", "Blog"),
                _raw("q", "https://unknown.test/x", "Unknown"),
            ]
        }
    )

    assert state["status"] == "verifying"
    assert len(state["verified_sources"]) == 1
    passed = state["verified_sources"][0]
    assert passed["source_data"]["url"] == TRUSTED
    assert passed["trust_score"] == 95
    assert passed["reason"] == "1차 마켓플레이스"
    assert created


def test_verifier_drops_items_whose_llm_call_fails(fake_llm):
    """개별 LLM 실패는 삼키고 그 항목만 버린다(부분 실패가 전체를 못 죽인다)."""
    fake_llm(score_map={TRUSTED: (90, "ok")}, raises_on=("bad.test",))
    state = sv.verifier_node(
        {
            "raw_sources": [
                _raw("q", TRUSTED, "Acquire"),
                _raw("q", "https://bad.test/post", "Boom"),
            ]
        }
    )

    urls = [item["source_data"]["url"] for item in state["verified_sources"]]
    assert urls == [TRUSTED]


def test_loader_without_sources_writes_nothing(capture_vault_write):
    state = sv.loader_node({"verified_sources": []})

    assert state == {"status": "completed"}
    assert capture_vault_write["files"] == []
    assert capture_vault_write["dirs"] == []


def test_loader_writes_markdown_sorted_by_trust_score(capture_vault_write):
    """검증 통과분은 점수 내림차순 마크다운으로 떨어진다."""
    state = sv.loader_node(
        {
            "verified_sources": [
                {
                    "source_data": {
                        "title": "Low",
                        "url": "https://low.test",
                        "content": "x" * 250,
                    },
                    "trust_score": 85,
                    "reason": "커뮤니티 검증",
                },
                {
                    "source_data": {
                        "title": "High",
                        "url": "https://high.test",
                        "content": "c",
                    },
                    "trust_score": 99,
                    "reason": "1차 마켓",
                },
            ]
        }
    )

    assert state == {"status": "completed"}
    assert len(capture_vault_write["files"]) == 1
    written = capture_vault_write["files"][0]["sink"].text
    assert "Verified_Platforms_" in capture_vault_write["files"][0]["path"]
    assert written.index("High") < written.index("Low")
    assert "⭐ 99/100" in written
    assert "1차 마켓" in written
    # 스니펫은 200자에서 잘린다(문서가 무한정 길어지지 않게).
    assert "x" * 200 + "..." in written
    assert "x" * 201 not in written


def test_graph_pauses_before_loader_and_resumes_offline(
    no_network, fake_searxng, fake_llm, capture_vault_write
):
    """L2: 네트워크 차단 상태에서 발견→검증까지 돌고 loader 앞에서 멈춘다."""
    queries = [
        "Micro SaaS acquisition marketplace with verified MRR",
        "Indie hacker profitable projects revenue Stripe",
        "Buy and sell small profitable websites platform",
    ]
    results = {
        query: [
            {"title": "Acquire", "url": TRUSTED, "content": "verified mrr"},
            {"title": "Listicle", "url": "https://blog.test/x", "content": "top 10"},
        ]
        for query in queries
    }
    fake_searxng(results)
    fake_llm(score_map={TRUSTED: (95, "1차 마켓")}, default=15)

    from langgraph.checkpoint.memory import MemorySaver

    graph = sv.build_source_verification_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "source-e2e"}}

    state = graph.invoke({"status": "init"}, config)

    assert graph.get_state(config).next == ("loader",)
    assert state["queries"] == queries
    assert len(state["raw_sources"]) == len(queries) * 2
    assert {row["query"] for row in state["raw_sources"]} == set(queries)
    assert len(state["verified_sources"]) == len(queries)
    assert all(item["trust_score"] >= 80 for item in state["verified_sources"])
    assert state["status"] == "verifying"

    final = graph.invoke(None, config)

    assert final["status"] == "completed"
    assert graph.get_state(config).next == ()
    assert len(capture_vault_write["files"]) == 1
