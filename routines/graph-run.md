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
- "로고/브랜드 마크 납품해줘" → `entry_pack` (`spec.profile="brand"`, `mark_path` 필수)
- "패키지 납품" → `entry_pack` (`spec.profile="print"` 또는 `"brand"`)
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
python3 scripts/graph_cli.py approve <thread_id> --graph <graph_id> \
    --values '{"selection":["..."],"approval_status":"approved"}' \
    --tag 차분함 --tag 여백 --reason "빈 공간이 편안해서" --wait
```

**모든 승인에 `--tag`와 `--reason`을 붙인다.** 이 값이 취향 기록(`decisions.jsonl`)에
남아 다음 후보 순위의 근거가 된다. 태그는 `--tag`를 반복해 1~3개, 이유는 사용자가
말한 표현을 그대로 짧게 적는다(예: "빈 공간이 편안해서"). 임의로 지어내지 않는다.

```bash
python3 scripts/graph_cli.py decisions --last 5    # 최근 기록
python3 scripts/graph_cli.py taste                 # 태그·스타일 승률·비평 일치 요약
```

### 3. 상태 확인

```bash
python3 scripts/graph_cli.py status <thread_id>
```

## 그래프별 입력 예시

| 그래프 | 입력 |
|---|---|
| `error_triage` | `{"messages":[{"role":"user","content":"에러 로그"}]}` |
| `entry_pack` | `{"brief":"핸드메이드 티 랜딩 — 차분한, 웹","spec":{"platform":"99designs","themes":["light","dark"],"variant_cap":12}}` |
| `entry_pack` (brand) | `{"brief":"브랜드","spec":{"profile":"brand","brand":{"mark_path":"/path/to/mark.svg","palette":{"ink":"#161616"}}}}` |
| `copywriting_craft` | `{"scene_id":"Act 1","raw_draft":"초안","context":"맥락"}` |
| `editorial_verification` | `{"title":"제목","content":"본문","target_stance":"관점"}` |
| `visual_consistency` | `{"project_name":"프로젝트","vectors":[...],"motions":[...],"imagery":[...]}` |
| `knowledge_harvester` | `{"query":"Bauhaus","category":"design"}` |

## 주의사항

- `LANGGRAPH_URL` 기본값: `http://127.0.0.1:8123` (배포 서버)
- 네트워크 호출이 있는 그래프(research, knowledge_harvester)는 60~120초 걸릴 수 있다
- `skip_knowledge=True`로 레퍼런스 수집을, `skip_llm=True`로 방향 생성을 건너뛸 수 있다 (entry_pack)
- brand profile은 기본값이 오프라인이다(`skip_knowledge`·`skip_llm`, 마크는 `mark_path`만 읽는다)

## HITL 승인 패턴

1. 그래프가 `next=["hitl"]` 또는 `next=["hitl_checkpoint"]` 등으로 멈춤
2. Kimaki가 thread_id와 선택지를 Discord에 표시
3. 사용자가 선택 → Kimaki가 `graph_cli.py approve` 실행
4. 결과를 Discord에 전송
