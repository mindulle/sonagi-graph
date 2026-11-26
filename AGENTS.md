# Sonagi Graph — Agent Instructions

## 그래프 실행 (Kimaki/Discord에서)

이 저장소의 그래프는 LangGraph 서버(:8123)에서 실행된다.
Discord 세션에서 그래프를 실행하려면 `scripts/graph_cli.py`를 쓴다.

### 사용 가능한 그래프

| 그래프 | 용도 | 입력 예시 |
|---|---|---|
| `error_triage` | Sentry 에러 분석 | `{"messages":[{"role":"user","content":"에러 로그"}]}` |
| `entry_pack` | 엔트리 변주·포장 (profile `web`/`print`/`brand`) | `{"brief":"...","spec":{"platform":"99designs"}}` |
| `entry_pack` (brand) | 마크 납품 — 정규화·아웃라인·게이트·팩 | `{"brief":"브랜드","spec":{"profile":"brand","brand":{"mark_path":"/path/mark.svg"}}}` |
| `entry_pack` (brand explore) | 브리프 → 시드 결정론 후보 6개 → 게이트 → HITL → 킷(락업 포함) | `{"brief":"브랜드","spec":{"profile":"brand","brand":{"mode":"explore","brief":"민들레 — 차분한 스튜디오","candidates":6}}}` |
| `copywriting_craft` | 카피 3안 생성 | `{"scene_id":"Act 1","raw_draft":"초안","context":"맥락"}` |
| `editorial_verification` | 글 4대 검증 | `{"title":"제목","content":"본문"}` |
| `visual_consistency` | 시각 일관성 검증 | `{"project_name":"프로젝트","vectors":[...],"motions":[...],"imagery":[...]}` |
| `knowledge_harvester` | 공공 데이터 수집 | `{"query":"Bauhaus","category":"design"}` |
| `source_verification` | SearXNG + LLM 신뢰도 심사 | `{"status":"init"}` |
| `video_ops` | 모션 + 컷편집 | `{"brief":"브리프","aspect_ratio":"16:9"}` |
| `design_token_sync` | 토큰 정본 → 소비처 컴파일 | `{"tokens_dir":"/path","out_dir":"/tmp/out"}` |

### 실행 패턴

```bash
# 1. 그래프 실행 (HITL 없음)
python3 scripts/graph_cli.py run <graph_id> --input '<json>' --wait --timeout 120

# 2. HITL (그래프가 멈추면 thread_id와 선택지가 출력됨)
python3 scripts/graph_cli.py run <graph_id> --input '<json>' --wait --timeout 120
# → next=["hitl"] 또는 next=["hitl_checkpoint"] 이면 대기
# → 사용자에게 선택지 제시 후:
python3 scripts/graph_cli.py approve <thread_id> --graph <graph_id> --values '<json>' --wait
```

### HITL 승인 패턴 (Discord 버튼)

1. 그래프가 `next=["hitl"]` 또는 `next=["hitl_checkpoint"]`로 멈춤
2. thread_id와 선택지를 Discord에 표시
3. 사용자가 선택 → `graph_cli.py approve <thread_id> --graph <graph> --values '{"approval_status":"approved"}'`
4. 결과를 Discord에 전송

### 주의사항

- `LANGGRAPH_URL` 기본값: `http://127.0.0.1:8123`
- research/knowledge_harvester는 네트워크 호출로 60~120초 걸릴 수 있음
- `skip_knowledge=True`로 레퍼런스 수집을, `skip_llm=True`로 방향 생성을 건너뛸 수 있음 (entry_pack)
- brand profile은 기본값이 `skip_knowledge`·`skip_llm`이라 네트워크·LLM 없이 결정론으로 돈다
