<!--
PR 제목: <type>(<scope>): <한 줄 요약>
  type: feat | fix | refactor | docs | chore | test
  scope: 그래프 이름 또는 core | ci | docs

머지 조건: CI green + 아래 체크리스트 확인. (docs/GRAPH_REGISTRY.md §5·§7)
-->

## 무엇을 바꿨나

- 

## 그래프 변경 체크리스트

- [ ] **입력 스키마**를 갱신했다 — 새 입력 필드는 `State`/스키마에 타입과 기본값을 명시했고, `AGENTS.md`의 입력 예시도 맞췄다.
- [ ] **`verify()`** 를 갱신했다 — 새 산출물은 검증 항목(존재·형식·해시)이 있고, 검증 실패 시 상태값이 실패로 떨어진다.
- [ ] **테스트**를 추가·수정했다 — 노드 단위(L1)는 필수, fixture E2E(L2)는 네트워크 없이 돈다.
- [ ] **결정론(L3)** 은 해당되는 경우에만 — 렌더·변환 계열은 같은 입력 2회 → 동일 해시를 확인했고, LLM·SearXNG 의존 그래프는 L3 대상이 아님을 밝혔다.
- [ ] **그래프 레지스트리**(`docs/GRAPH_REGISTRY.md`)를 갱신했다 — 새 그래프는 분류표에 행을 추가했고, 통합·폐기 대상이면 목표 칸을 채웠다.
- [ ] **직접 실행 없음** — `graphs/**` 에서 `subprocess`/`os.system`/`asyncio subprocess`를 직접 부르지 않는다. 프로세스 실행은 `graphs/core/exec.py` 경유이며 `python3 scripts/check_graph_rules.py` 가 통과한다.
- [ ] **core 이관 PR이면** `scripts/check_graph_rules.py` 의 `LEGACY_BASELINE` 에서 이관한 파일 항목을 삭제했다. 남겨 두면 그 파일의 신규 직접 실행이 옛 상한만큼 통과한다.

## 스크립트를 추가했다면

- [ ] 일회성·실험 스크립트는 `scripts/expiring/` 에 있고, 파일 상단에 **소유자·만료일·목적**을 적었다.
- [ ] 만료일이 지나면 삭제하거나 그래프의 `verify()` 로 흡수한다.

## 확인한 것

```
# 로컬 검증 결과 붙여넣기 (pytest, check_graph_rules)
```

## 관련

- Plane: GRAPH-__
