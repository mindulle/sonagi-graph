# Graph Registry — 정비 인벤토리 (P0)

> 목적: 15개 그래프를 **유지·통합·폐기**로 분류하고, 어디서 무엇을 흡수할지 고정한다.
> 이 문서가 P1(core)·P2(검증)·P3(마이그레이션)의 기준표다.

- 최초 작성: 2026-11-25
- 대상: `graphs/*_graph.py` 15개, `scripts/` 5개
- 원칙: **반복되는 일은 그래프, 한 번짜리는 스크립트 — 단 스크립트도 core 위에서.**

---

## 1. 분류표

| 그래프 | 목적 | 노드 | 직접 subprocess | 테스트 | 결정 | 목표 |
| --- | --- | --- | --- | --- | --- | --- |
| `entry_pack` | 콘테스트 엔트리 변주·검증·포장 | 10 | 2 | ✅ | **KEEP (1순위)** | core 이관 + profile(web/brand/print) |
| `video_ops` | 브리프 → EffectCraft → FilmCraft → CDN | 5 | 5 | ✅ | **KEEP (1순위)** | core 이관 (subprocess 5→0) |
| `design_ops` | 벡터 아트워크 생성 → PDF/PNG → asset_hub | 4 | 4 | ✅ | **KEEP (1순위)** | core 이관 + publish 하드코딩 제거 |
| `editorial_verification` | 글 4대 검증 | 6 | 0 | ✅ | KEEP | core 계약 적용 |
| `visual_consistency` | 벡터·모션·이미지 일관성 | 5 | 0 | ✅ | KEEP | core 계약 적용 |
| `copywriting_craft` | 클리셰 필터 → 카피 3안 | 5 | 0 | ✅ | KEEP | core 계약 적용 |
| `design_token_sync` | 토큰 컴파일 → Figma | 5 | 0 | ❌ | KEEP | 테스트 추가 |
| `knowledge_harvester` | 공공 아카이브 수집 | 8 | 0 | ✅ | KEEP | core 계약 적용 |
| `source_verification` | SearXNG 검증 | 4 | 0 | ❌ | KEEP | 테스트 추가 |
| `market_intelligence` | 시장 수집·분석 | 4 | 0 | ❌ | KEEP | 테스트 추가 |
| `error_triage` | Sentry 오류 분석 | 3 | 0 | ✅ | KEEP | core 계약 적용 |
| `asset_craft` | 렌더 후보 생성·선택 | 7 | 1 | ❌ | **MERGE** | `core/assets` 어댑터로 흡수, 독립 그래프 등록 해제 |
| `logo_design` | 로고 패키지 검증 (press 이후) | 10 | 1 | ✅ | **MERGE (조건부)** | `entry_pack` brand profile이 context_test·critic·print_prep 흡수 후 폐기 |
| `package_series` | 3건 배치 납품 | 4 | 0 | ✅ | **MERGE (조건부)** | brand profile의 배치 모드로 흡수 |
| `spike_test` | 스파이크 | 2 | 0 | ❌ | **DELETE** | 등록 해제 |

### 판정 요약

- KEEP: 11 (그중 1순위 3개)
- MERGE: 3 (`asset_craft`, `logo_design`, `package_series`)
- DELETE: 1 (`spike_test`)

---

## 2. 흡수 대상 (스크립트 → 그래프/core)

| 현재 | 문제 | 흡수 위치 |
| --- | --- | --- |
| `scripts/verify_design_ops_hitl.py` | 그래프 밖 일회성 검증 | `design_ops`의 `verify()` |
| `scripts/verify_knowledge_harvester.py` | 동일 | `knowledge_harvester`의 `verify()` |
| `scripts/verify_video_ops.py` | 동일 | `video_ops`의 `verify()` |
| `scripts/inkscape_shim.py` | 변환기 shim이 그래프마다 재구현 | `core/exec` 또는 `core/assets` |
| `ASSETS_DIR`·`CATALOG`·`TOKENS`·`OUT_ROOT` 반복 선언 | 경로 불일치 위험 | `core/env` |
| `_run` 중복 구현 (entry_pack, asset_craft, logo_design, video_ops, design_ops) | 타임아웃·에러 형식 제각각 | `core/exec` |
| `graph_cli.py` | 실행/승인만 지원 | `runs`에 ledger 조회 추가 |

---

## 3. 검증 등급 (모든 그래프 공통)

| 등급 | 내용 | 강제 |
| --- | --- | --- |
| L0 | 임포트·컴파일 | CI |
| L1 | 노드 단위 테스트 | CI |
| L2 | fixture E2E (네트워크 없음) | CI |
| L3 | 결정론 (같은 입력 2회 → 동일 해시) | CI (해당 그래프) |
| L4 | 실데이터 스모크 (수동, 릴리즈 전) | 체크리스트 |

**완료 기준**: KEEP 그래프 전부 L2 이상, 결정론 대상은 L3.

---

## 4. 실행 웨이브

| 웨이브 | 작업 | 병렬 | 선행 |
| --- | --- | --- | --- |
| **W1** | `core/` (env·exec·artifacts·contract·hitl) | — | 없음 |
| **W1** | 검증 표준 + 미테스트 5개 그래프 테스트 | ✅ core와 병렬 | 없음 |
| **W1** | 가드레일 (PR 체크리스트, 직접 subprocess lint) | ✅ | 없음 |
| **W2** | `entry_pack` core 이관 | ✅ | W1 core |
| **W2** | `video_ops` core 이관 | ✅ | W1 core |
| **W2** | `design_ops` core 이관 | ✅ | W1 core |
| **W3** | profile 4종 (web/brand/print/social) | ❌ (같은 파일) | W2 entry_pack |
| **W4** | 흡수·폐기 (`asset_craft`→core, `logo_design`/`package_series`→brand, `spike_test` 삭제) | ✅ | W3 |
| **W5** | ledger 조회 (`graph_cli runs --json`) | ✅ | W1 artifacts |

---

## 5. 완료 정의 (Definition of Done)

1. KEEP 그래프 전부: 입력 스키마 + `verify()` + L2 테스트
2. CI에서 네트워크 없이 전 그래프 스모크
3. 결정론 그래프: 같은 입력 2회 동일 해시
4. `graph_cli runs`로 입력·게이트·산출 해시 조회
5. `graphs/*_graph.py` 내 직접 `subprocess.run` 0 (lint 차단)
6. 신규 작업은 profile 1파일 추가로 끝남
7. `spike_test` 삭제, `asset_craft` 독립 등록 해제

---

## 6. 리스크

- **과도한 추상화**: core가 두꺼워지면 그래프 하나 고치려고 core를 고쳐야 한다. core는 실행·경로·기록까지만.
- **동시 편집 충돌**: W2의 세 그래프는 파일이 분리돼 있으나 core 인터페이스 변경은 한 곳에서만.
- **결정론 오판**: 외부 API(LLM·SearXNG)를 쓰는 그래프는 L3 대상이 아니다. 렌더·변환 계열만 적용.

---

## 7. 가드레일 (P5)

> 목적: 그래프·core를 우회하는 임시 스크립트와 직접 실행을 **PR 단계에서** 막는다.

### 7.1 직접 실행 lint

`python3 scripts/check_graph_rules.py` — CI `guardrails` job에서 돈다. 표준 라이브러리만 쓰므로 설치 단계가 없다.

- 원칙: 프로세스 실행은 `graphs/core/exec.py` 한 곳에서만. `graphs/**` 에서 `subprocess.{run,Popen,call,check_call,check_output,getoutput,getstatusoutput}`, `os.system`·`os.popen`, `asyncio.create_subprocess_*` 직접 호출 금지(import 자체는 허용).
- 허용 목록은 스크립트 안의 두 상수로만 관리한다.
  - `ALLOWED_FILES` = `graphs/core/exec.py` — W1에서 도입된 유일한 실행 지점. core가 아직 없거나 이 파일에 호출이 0건이어도 lint는 통과한다.
  - `LEGACY_BASELINE` = P0 시점의 기존 위반(파일별 허용 상한). 아래 표가 정본이며 **늘리지 않는다**.

| 파일 | baseline | 해소 시점 |
| --- | --- | --- |
| `graphs/logo_design_graph.py` | 1 | W4 logo_design 병합 |
| `graphs/asset_craft_graph.py` | 1 | W4 asset_craft 흡수 |

- 해소 완료: `video_ops`(5→0, #61)·`entry_pack`(2→0, #60)·`design_ops`(4→0, #63)는 baseline에서 제거했다. 남은 2개 파일도 0건이 되면 같은 방식으로 항목을 지운다.
- core 이관 PR은 **baseline 항목 삭제까지**가 한 세트다. 안 지우면 그 파일에 신규 직접 실행이 생겨도 옛 상한만큼 통과해 버린다.

- 동작: 신규 파일의 직접 호출, 또는 baseline을 넘는 추가 호출이 있으면 exit 1. baseline보다 줄면 "항목을 삭제하세요" 안내만 낸다(실패 아님).
- W4 종료 후 `--strict`(baseline 무시)를 CI 기본값으로 올린다. 그때가 §5 완료 정의 5번이 닫히는 지점이다.
- 로컬 확인: `python3 scripts/check_graph_rules.py --path <파일 또는 디렉터리>`.

### 7.2 PR 체크리스트

`.github/pull_request_template.md` 가 모든 PR에 자동 적용된다. 항목은 입력 스키마, `verify()`, 테스트(L1/L2), 결정론(L3, 해당 시), 이 문서(그래프 레지스트리), 직접 실행 없음.

### 7.3 스크립트 수명 규칙

- 실험·일회성 스크립트는 **`scripts/expiring/`** 에만 둔다. 상시 운영 스크립트(`graph_cli.py`)는 `scripts/` 루트에 둔다.
- 파일 상단 docstring에 `owner`(책임자), `expires`(YYYY-MM-DD), `purpose`(한 줄)를 반드시 적는다.
- 만료일이 지난 스크립트는 삭제하거나 그래프의 `verify()`/`core`로 흡수한다. 리뷰에서 만료일을 확인한다.
- 반복해서 쓰게 된 스크립트는 스크립트로 남기지 않고 그래프 노드나 `core/`로 승격한다 — §2의 흡수 대상이 그 결과다.

---

## 8. profile 프레임워크 (P4a, GRAPH-17)

`entry_pack`은 공통 파이프라인(브리프→IR→방향→변주→렌더→린트→게이트→선택)만
소유하고, 납품 형식은 `graphs/profiles/`가 맡는다.

| 파일 | 책임 |
| --- | --- |
| `profiles/base.py` | `Profile` Protocol·`ProfileContext`·`register`/`get_profile`·공통 manifest |
| `profiles/web.py` | 기존 entry_pack 동작(SVG/PNG/JPG·카드/포스터 목업) — 공개 동작 불변 |
| `profiles/print.py` | 라벨·파우치 전개도 → 인쇄 PDF 게이트 → 전면 PNG/JPG·meta·목업 |

### 8.1 인터페이스

```python
class Profile(Protocol):
    id: str
    def defaults(self) -> dict: ...
    def plan(self, ir, spec, styles) -> list[dict]: ...
    def render(self, variant, ctx: ProfileContext) -> dict | None: ...
    def gates(self) -> list[Gate]: ...
    def pack(self, items, spec, state) -> dict: ...
    def mockups(self, items, spec) -> list[dict]: ...
```

해석 순서: `spec["profile"]` → `spec["contest_type"]` → `"web"`. 모르는 이름은
사용 가능 목록과 함께 `ProfileError`로 멈춘다. 새 형식은 파일 하나 + `register()`다.

### 8.2 print

`spec["package"]`가 계약이다(`type`·`brand`·`product`·`size_mm[2]` 필수, `colors`·
`barcode`·`mark_path` 선택). 게이트는 실패 시 납품을 중단한다(fail closed).

| 게이트 | 검사 |
| --- | --- |
| `print.export_check` | `export_print.py --check` — 재단/도련 3mm·안전여백·레이어 |
| `print.pdf` | 출력 PDF 존재 + 0바이트 초과 |
| `print.fonts` | `pdffonts`에 `emb=no` 없음 |
| `print.inkcov` | `gs -sDEVICE=inkcov`가 CMYK를 보고 |

테스트에서만 `spec["allow_print_checks"] = true`로 건너뛴다(게이트는 `skipped`로 기록,
manifest 상태는 `warn`). 문자열 `"false"` 같은 값은 우회로 인정하지 않는다.

- print 기본값은 `skip_knowledge`·`skip_llm`을 켠다 — 인쇄물은 방향/레퍼런스가 산출물에
  쓰이지 않으므로 네트워크·LLM 없이 결정론으로 돈다.
- `mark_path`는 파우치 템플릿에 직접, 라벨은 `render_label.py`가 모르므로 art 그룹에
  주입하고 manifest 해시를 갱신한다.
- `export_print.py`는 Chromium을 쓴다. snap Chromium은 `/tmp`(PrivateTmp)를 읽지 못하므로
  실행 루트(`SONAGI_OUT_ROOT`)가 Chromium에서 접근 가능한 경로여야 한다.


