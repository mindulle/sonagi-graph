---
title: Graph Run (Kimaki)
description: >
  Managed by a kimaki routine. Discord 메시지에서 LangGraph 그래프를 실행하고
  HITL 승인을 처리한다. 그래프 코드를 import하지 않고 scripts/graph_cli.py만 쓴다.
---

# Kimaki 그래프 실행 루틴

## 언제 쓰나

사용자가 Discord에서 다음 중 하나를 요청할 때:
- "이 에러 분석해줘" / "에러 트리아지" → `error_triage`
- "로고 만들어줘" / "마크 생성" → `logo_design`
- "패키지 3건 납품" → `package_series`
- "카피 써줘" → `copywriting_craft`
- "글 검증해줘" → `editorial_verification`
- "디자인 검증" → `visual_consistency`

## 실행 패턴

### 1. 단순 실행 (HITL 없음)

```bash
python3 scripts/graph_cli.py run <graph_id> --input '<json>' --wait --timeout 120
```

결과가 `run_status=success, next=[]`이면 완료. 리포트/결과를 Discord에 붙인다.

### 2. HITL 실행 (그래프가 멈춤)

```bash
python3 scripts/graph_cli.py run <graph_id> --input '<json>' --wait --timeout 120
# → next=["..."] 이면 HITL 대기. thread_id와 선택지를 사용자에게 보여준다.
```

사용자가 선택하면:
```bash
python3 scripts/graph_cli.py approve <thread_id> --graph <graph_id> --values '{"selected_option_index":0}' --wait
```

### 3. 상태 확인

```bash
python3 scripts/graph_cli.py status <thread_id>
```

## 그래프별 입력 예시

| 그래프 | 입력 |
|---|---|
| `error_triage` | `{"messages":[{"role":"user","content":"에러 로그"}]}` |
| `logo_design` | `{"brief":"브랜드명","mark_source":"existing","mark_path":"/path/to/mark.svg","concept":"여백을 살린 모노그램","content":"/path/to/brief.md"}` |
| `copywriting_craft` | `{"scene_id":"Act 1","raw_draft":"초안","context":"맥락"}` |
| `editorial_verification` | `{"title":"제목","content":"본문","target_stance":"관점"}` |
| `visual_consistency` | `{"project_name":"프로젝트","vectors":[...],"motions":[...],"imagery":[...]}` |
| `knowledge_harvester` | `{"query":"Bauhaus","category":"design"}` |

## 주의사항

- `LANGGRAPH_URL` 기본값: `http://127.0.0.1:8123` (배포 서버)
- 네트워크 호출이 있는 그래프(research, knowledge_harvester)는 60~120초 걸릴 수 있다
- `skip_research=True`로 research를 건너뛸 수 있다 (logo_design)
- `skip_vision=True`로 LLM vision 체크를 건너뛸 수 있다 (logo_design)

## HITL 승인 패턴

1. 그래프가 `next=["hitl"]` 또는 `next=["hitl_checkpoint"]` 등으로 멈춤
2. Kimaki가 thread_id와 선택지를 Discord에 표시
3. 사용자가 선택 → Kimaki가 `graph_cli.py approve` 실행
4. 결과를 Discord에 전송
