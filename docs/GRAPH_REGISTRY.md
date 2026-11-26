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

### 8.3 입상 후 핸드오프 도구

인쇄용 변환은 **입상 후 핸드오프 단계**의 일이다. 콘테스트 심사는 표현물 이미지로 이뤄지고,
CMYK·PDF/X·도련은 당선된 뒤 인쇄소에 넘길 때만 의미가 있다. 그래서 `ghostscript`(`gs`)·
`poppler-utils`(`pdffonts`)·`chromium` 설치는 **지금 하지 않는다**. langgraph 이미지에도 넣지
않고, handoff 단계를 실제로 켤 때 실행 환경에 갖춘다(GRAPH-19 결정, 2026-11-25).

- entry print profile은 이 세 도구를 **요구하지 않아야** 한다. 도구가 없어도 표현물
  (PNG/JPG·목업)은 나와야 하고, `print.export_check`·`print.fonts`·`print.inkcov`는
  handoff 게이트에서만 돈다. 없는 상태에서 entry가 실패하면 그건 회귀다.
- Inkscape는 필요 없다. 마크·SVG 조작과 래스터화는 마운트된 `vectorcraft-cli`가 맡는다.
  Inkscape를 넣으면 이미지가 수백 MB 늘어나는데 얻는 게 없다.
- handoff를 켤 때 필요한 것은 §8.2의 handoff 게이트와 같은 도구다: `ghostscript`(`gs`),
  `poppler-utils`(`pdffonts`), `chromium`, 그리고 `export_print.py`가 검사 단계에서 쓰는
  `pypdf`. 그때 이미지·실행 환경에 함께 넣는다.

### 8.4 brand

`spec["brand"]`가 계약이다. 마크는 **사용자가 만든 SVG**이며(`mark_path` 필수),
그래프는 마크를 그리지 않고 납품 가능한 상태로만 만든다. `palette`
(`ink`·`paper`·`accent`·`calm`)·`variants`(`sizes`·`backgrounds`)·`name`은 선택이다.

파이프라인은 정규화 → 아웃라인 → 게이트 → 팩 순서다.

| 단계 | 내용 |
| --- | --- |
| `normalize` | `<style>`·`var()`·`class`를 프레젠테이션 속성으로 편다(순수 파이썬, 결정론). `:root` 라이트 값만 쓰고 다크 테마는 버린다 |
| `outline` | `vectorcraft-cli convert IN OUT --outline-text` (CLI 없으면 실행형 안내와 함께 중단) |
| 게이트 | `<text>` 0 · `<style>`/`var()` 0 · 256px 불투명 픽셀 > 0(E015 빈 렌더) · PDF > 0B — fail closed |
| 팩 | `normalized.svg`·`outlined.svg`·`mark.pdf`·PNG 2000(최대 높이 3000)·JPG q90·사이즈/배경 PNG·`meta.json`·`entry.md` |

- 게이트 우회는 테스트에서만 `spec["allow_brand_checks"] = true`다(문자열 `"false"`는
  우회로 인정하지 않는다 — print와 같은 규칙). 우회한 실패는 `skipped`로 남고 manifest는 `warn`이다.
- 아이콘 변주는 2000px PNG를 Pillow로 줄인다(외부 프로세스 없음). 배경은 팔레트의
  `paper`(light)·`ink`(dark)다.
- 목업은 web의 카드 헬퍼를 재사용하고(없으면 같은 규격으로 직접), 아이콘 타일을 더한다.

#### 8.4.1 explore 모드 (GRAPH-33)

`spec.brand.mode = "existing" | "explore"`. 기본값은 `existing`이라 지금까지의 동작
(사용자 SVG → 정규화·아웃라인·게이트·팩)이 **바이트 단위로 그대로** 유지된다.
`explore`는 우리가 클라이언트일 때 브리프에서 **새 마크 방향**을 만든다.

```jsonc
{
  "profile": "brand",
  "brand": {
    "mode": "explore",
    "brief": "민들레 — 차분하고 고급스러운 스튜디오. 여백과 인쇄물.",
    "name": "mindulle",              // 선택(없으면 brief로 슬러그)
    "candidates": 6,                 // 기본 6, 2~12
    "palette": {"ink": "#161616", "paper": "#fbf9f5",
                "accent": "#b4551f", "calm": "#1f4536"},
    "mark_path": null                // 선택: 참고 SVG. 없어도 된다
  }
}
```

- `brief`는 `spec.brand.brief` 또는 `spec.brand.concept`(또는 top-level `spec.brief`)에서
  온다. `entry_pack`은 `spec_node`의 profile `enrich_spec` 훅으로 state의 `brief`를 넣는다.
- 후보 생성은 **시드 결정론**이다. `sha256(brief·concept·seed_hint)` + 후보 인덱스로
  64비트 시드를 만들고 splitmix64로 파라미터를 뽑는다. 같은 입력이면 좌표·SVG·PNG가
  같다. LLM·난수 모듈은 쓰지 않는다.

| 가족 | 형태 | 파라미터(시드에서 뽑음) |
| --- | --- | --- |
| `monogram` | 두꺼운 기하 획 이니셜(H·N·A·K·X·M·T·S·D) | 글리프·획 굵기·악센트 획·거울 |
| `seal` | 링/라운드 사각 프레임 + 내부 폴리곤·별·다이아·V | 프레임·내부형·면 수·회전·점 |
| `radial` | 꽃잎·광선·동심 링의 N회전 대칭 | 모드·개수·궤도·회전 |

후보 하나의 처리 순서:

| 단계 | 내용 |
| --- | --- |
| 생성 | `vectorcraft-cli run --cmd file.new/shape.*/path.create/paint.* --export 후보.svg --export 후보.png` |
| 정규화 | 기존 `normalize()` (순수 파이썬) |
| 아웃라인 | 기존 `vectorcraft-cli convert --outline-text` |
| 단색 변형 | 모든 `fill`/`stroke`를 팔레트 `ink` 하나로 치환한 `outlined-single-color.svg` |

후보 게이트(실패해도 후보를 버리지 않고 사유를 남긴다):

| 게이트 | 검사 |
| --- | --- |
| `brand.no_text` · `brand.no_style` · `brand.raster` | 기존 brand 게이트 재사용 |
| `candidate.palette` | SVG의 모든 fill/stroke 색이 넘겨받은 팔레트 안 |
| `candidate.safe_area` | 256px 렌더 바운딩박스의 사방 여백 ≥ 4% (아트보드 밖/가장자리 금지) |
| `candidate.single_color` | 단색 변형의 색이 정확히 1개이고 64px 렌더에 픽셀이 있다 |
| `candidate.legible_16` | 16px 단색 렌더의 잉크 5~90%, bbox ≥ 6×6px |

- **최소 2개 통과** 규칙: `BrandProfile.check_candidates`가 HITL 직전에 검사하고,
  미달이면 `CandidateGateError`로 멈춘다(후보별 실패 사유 포함). 실패한 후보를
  골라 pack하면 `BrandGateError`로 막힌다.
- HITL 선택지는 후보마다 하나씩 `id`·`family`·`seed`·`gate_summary`·`gates`를 싣는다
  (`entry_pack.gate_node`가 render 메타를 option으로 옮긴다).
- 선택 후 팩은 기존 파이프라인 그대로 `normalized.svg`·`outlined.svg`·`mark.pdf`·
  PNG 2000·JPG 90·아이콘 16~512 × light/dark를 만들고, explore에서만
  **락업**(`lockup.svg`·`lockup.pdf`, 워드마크 아웃라인)을 더한다. meta에는
  `mode`·`brief_sha256`·`candidate`·`candidate_gates`가 남는다.

#### 8.4.2 refine 모드 — 기존 인장 정련 (GRAPH-33 후속)

`spec.brand.mode = "refine"`은 **우리가 이미 가진 마스터 인장을 정련**한다.
v1 explore가 브리프에서 기하 프리미티브를 새로 그려 "범용 로고 생성기"처럼
보였던 문제를 뒤집는다. 원본 SVG의 구조를 읽고 그 구조만 남긴다.

```jsonc
{
  "profile": "brand",
  "brand": {
    "mode": "refine",
    "source_mark": "/home/ubuntu/mindulle-design-system/packages/brand-assets/01_master_seal/light/master-seal-light.svg",
    "brief": "민들레 — 차분한 스튜디오. 16px에서도 인장으로 읽혀야 한다.",
    "name": "mindulle",
    "candidates": 8,                 // 8~12
    "families": ["seal-refined", "seal-monogram", "seal-dandelion"],
                                     // 선택, 기본 전체(3가족)
    "palette": {"ink": "#161616", "paper": "#fbf9f5", "accent": "#b4551f"}
  }
}
```

- **계보 규칙**: 모든 후보는 같은 마스터 인장의 변주다. 세 가족 모두 후보마다
  바깥 링 골격을 **먼저** 그리고, 그 안쪽만 바꾼다. 글자만·방사선만 남은 새 로고는
  만들지 않는다(테스트가 후보마다 링 2겹 이상을 확인한다).
- `source_mark`는 필수다(`mark_path`와 다르다). 원본에서 **링 비율·방사 각도·
  방사선 길이·코어 비율·씨앗 점 비율**을 읽어 `SourceModel`로 만든다. 유리 돔
  대각선처럼 길이가 다른 선은 가장 많은 길이 군집(방사선)에서 제외한다.
- 시드는 `sha256(brief·concept·seed_hint)` + 후보 인덱스다. 같은 원본·브리프·
  인덱스면 좌표·SVG가 바이트 단위로 같다. LLM·난수 모듈은 쓰지 않는다.

| 가족 | 원본에서 물려받는 것 | 바꾸는 것 |
| --- | --- | --- |
| `seal-refined` | 링 비율·방사 각도·코어 | 마이크로 디테일 제거, 링 수·획 굵기 |
| `seal-monogram` | 링 골격·코어 위치 | 이름 머리글자를 중앙에 광학 정렬(±0.5px) |
| `seal-dandelion` | 방사 각도·줄기 길이 | 씨앗 밀도·줄기 굽힘·깃털 세 갈래 |

`negative-cut`은 **네 번째 가족이 아니라 수정자**다. 세 가족 안에서 두 번째 변주부터
켜지며(8개면 가족마다 최소 1개), 중심부를 원판 + 구멍(nonzero)으로 바꾼다.
바깥 인장 링은 그대로 두므로 같은 인장으로 읽힌다. 글자 조인트가 겹쳐 검게 메우는
문제는 교집합 보정(`_overlap_fillers`)으로 없앤다.

제작 규칙은 **게이트로 강제**한다(권고가 아니다):

| 게이트 | 검사 |
| --- | --- |
| `candidate.palette_roles` | 색이 `ink`·`accent`·`paper` 셋뿐 |
| `candidate.no_gradient` | 그라디언트·마스크·url(#) 0개 |
| `candidate.stroke_scale` | 모든 `stroke-width`가 같은 기준의 1.0/1.5/2.0 배율 |
| `candidate.optical_overshoot` | 원형 반지름에 0.4~2.0% 오버슈트, 링마다 적용 |
| `candidate.counters_16` | 카운터가 16px에서 0.75px 이상 열림 |
| 기존 후보 게이트 | `brand.no_text`·`no_style`·`raster`·`palette`·`safe_area`·`single_color`·`legible_16` |

- 후보는 HITL 전에 `candidate-contact.png`(종이 512 격자 + 다크 줄 + 실제 16px 줄,
  후보별 한국어 메모)와 `gate-table.md`로 모인다.
- **자동 선택 금지**: `BrandProfile.auto_select_allowed`가 refine에서 `False`라
  `approval_status=approved`만으로는 고르지 않고 `awaiting_selection`에 머문다.
  사람이 `selection`을 명시해야 팩이 만들어진다.
- 팩은 explore와 같은 규격(`normalized`·`outlined`·`mark.pdf`·PNG 2000·JPG 90·
  아이콘·락업)을 쓰되, 락업은 v2가 만든 마크 SVG를 그대로 앉힌다.


