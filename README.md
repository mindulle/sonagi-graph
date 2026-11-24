# Sonagi Graph

Sonagi 생태계의 다양한 에이전틱 그래프(Agentic Graphs)를 정의하고 관리하는 독립 저장소입니다.

## 아키텍처 개요

- **LangGraph** 기반의 Python 코드로 워크플로우를 설계합니다.
- `llmops-instance` 서버에 배포되어 백그라운드 워커로 작동합니다.
- **Paperclip**과 **Kimaki**를 통해 모니터링, 제어(HITL), 오케스트레이션이 이루어집니다.

## 등록된 그래프

`langgraph.json`이 실행 단위다. 각 파일은 `graph`라는 이름으로 컴파일된 그래프를 노출한다.

| 그래프 | 파일 | 하는 일 | 외부 의존 | HITL |
| --- | --- | --- | --- | --- |
| `spike_test` | `graphs/spike_test_graph.py` | LangGraph 배선 확인용 최소 그래프 | 없음 | – |
| `error_triage` | `graphs/error_triage_graph.py` | 센트리 오류 트리아지 | LLM 프록시 | – |
| `market_intelligence` | `graphs/market_intelligence_graph.py` | 시장 신호 수집·분석 → DB 적재 | LLM 프록시, `requests` | `loader` 직전 |
| `design_ops` | `graphs/design_ops_graph.py` | ArtCraft 시안 3안 → 선택 | `vectorcraft-cli`, Paperclip | `publish` 직전 |
| `asset_craft` | `graphs/asset_craft_graph.py` | 브리프 → 렌더 후보 3안 | 크래프트 CLI, `sonagi-assets` | `select` 직전 |
| `design_token_sync` | `graphs/design_token_sync_graph.py` | 토큰 정본 → 소비처 컴파일 | `sonagi-assets`, MDS | 승인 게이트 |
| `video_ops` | `graphs/video_ops_graph.py` | EffectCraft 모션 + FilmCraft 컷편집 → CDN | `effectcraft-cli`, `filmcraft-cli` | `export_and_publish` 직전 |
| `editorial_verification` | `graphs/editorial_verification_graph.py` | 학술·논리·미학·다이어그램 4대 크리틱 → 판정 | LLM 프록시 | `vault_loader` 직전 |
| `visual_consistency` | `graphs/visual_consistency_graph.py` | 벡터·모션·이미지 3대 크리틱 → 판정 | LLM 프록시 | `vault_loader` 직전 |
| `copywriting_craft` | `graphs/copywriting_craft_graph.py` | 클리셰 필터 → 오감 주입 → 카피 3안 | LLM 프록시 | `hitl_checkpoint` 직전 |
| `knowledge_harvester` | `graphs/knowledge_harvester_graph.py` | 공공 오픈 데이터 6곳 → 위키 카탈로그 | 공개 API (키 불필요) | – |
| `source_verification` | `graphs/source_verification_graph.py` | SearXNG 검색 → LLM 신뢰도 심사 → 볼트 | 로컬 `SearXNG`, LLM 프록시 | `loader` 직전 |

### 그래프를 추가할 때

1. `graphs/<name>_graph.py`에 `graph = builder.compile(...)`를 둔다. 이름은 반드시 `graph`.
2. `langgraph.json`의 `graphs`에 `"<name>": "./graphs/<name>_graph.py:graph"`를 추가한다.
3. 사람이 개입해야 하면 `interrupt_before=[...]`를 쓰고, 재개 전에 무엇을 바꿔야 하는지 주석으로 남긴다.
4. 테스트는 `test_<name>.py`에 두고 LLM·CLI·HTTP를 monkeypatch한다. 루트 `pytest`는 오프라인으로 돈다.
   실장비가 필요한 검증은 `scripts/verify_<name>.py`로 분리한다.

## 크래프트 앱 호출 (VectorCraft)

VectorCraft는 바이너리가 **둘**이고 역할이 다르다. 헷갈리면 "왜 안 되지"에서 한 시간을 쓴다.

| 바이너리 | 용도 | 실행 조건 |
| --- | --- | --- |
| `vectorcraft` | 데스크톱 앱 · `--control PORT`로 제어 포트 개방 | **DISPLAY 필요**(없으면 winit 오류로 즉시 종료) |
| `vectorcraft-cli` | 헤드리스 자동화 | DISPLAY 불필요 |

```bash
# 문서 → PDF/PNG/vectorcraft(AST)
vectorcraft-cli convert in.svg out.pdf
vectorcraft-cli convert in.svg out.png --scale 2

# 명령 배치(구조 검사·내보내기)
vectorcraft-cli run --in in.svg --cmd document.inspect --export out.png

# 설치된 명령 749개 목록(저작·recolor 포함)
vectorcraft-cli commands | jq 'length'
```

`graphs/design_ops_graph.py`는 `shutil.which("vectorcraft-cli")`로 CLI를 찾는다.
문서 구조는 `info`가 아니라 **`run --cmd document.inspect`**로 받는다(`info`·`bench`
서브커맨드는 0.11에 없다). 응답은 JSON 한 줄씩 흘러나오므로 `step == "cmd"` 라인만 쓴다.

실측(2026-11-24, sonagi wordmark 450×116): `objects 12 · Path 9 · Compound Path 2`, 4ms.

## 시작하기

```bash
pip install langgraph-cli
langgraph up --wait --debugger-port 3000
```

## 브랜드 토큰

그래프는 팔레트를 직접 들고 있지 않는다. 색은 `sonagi-assets`의 SSOT에서 읽는다.

| SSOT                   | 내용                                         |
| ---------------------- | -------------------------------------------- |
| `tokens/semantic.json` | 팔레트(editorial·architecture)               |
| `registry/index.json`  | `tokens_version`·`ruleset_version`·스타일 수 |
| `registry/styles.json` | 스타일 `id`·축 조합                          |

```bash
export SONAGI_ASSETS_DIR=/home/ubuntu/sonagi-assets   # 기본값
```

```python
import brand

palette = brand.design_palette("architecture")  # 12개 역할, 모두 토큰 또는 파생
stamp = brand.contract()                        # 산출물에 남길 재현 좌표
style = brand.style("editorial")                # 이름 또는 style_id로 조회
```

토큰에 없는 역할은 **파생**한다(`brand.tint`/`brand.shade`). 같은 입력이면 같은
hex가 나오고, 토큰을 고치면 팔레트도 따라온다. 매핑 표는 `brand.py` docstring에 있다.

SSOT가 없으면 `BrandSourceMissing`으로 멈춘다. 조용히 기본색으로 그리지 않는다.

```bash
python3 -m pytest test_brand_tokens.py -q
```

서체와 선 두께도 같은 SSOT에서 온다.

```python
brand.typography()                 # {'display': {'stack': ..., 'family': ...}, ...}
brand.google_fonts_url({"display": "700", "mono": "600"})
brand.stroke("border-width")       # "2px"  (True 2px Stem)
brand.stroke_px("border-hairline") # 1.0
```

그래프 소스에 `Cormorant Garamond` 같은 서체 이름이나 `stroke-width="2"`를 직접
쓰지 않는다. 단, 아트워크 내부의 장식선 두께는 작품 값이므로 리터럴로 남긴다.

## Design Token Sync (토큰 정본 → 소비처 컴파일 → 사람 승인)

정본은 `@mindulle/tokens`(`mindulle-design-system/packages/tokens`, 2026-11-23
확정)이다. `sonagi-assets/tokens`는 그 투영이다. `token_sync.py`가 정본을 읽어
검사하고 소비처 산출물을 만들며, `graphs/design_token_sync_graph.py`가 그 위에
승인 게이트를 얹는다.

```
tokens/*.json ─ token_parse ─ transpile ─ ⏸ hitl_checkpoint ─ figma_bridge ─ ⏸ figma_apply
                    │            │              │                  │              │
            참조 해석·검사 4종   CSS·Tailwind   승인 → 리포트     브리지 확인     쓰기 게이트
                                 Craft 스와치                     요청+스크립트    (MCP)
```

```bash
export MDS_TOKENS_DIR=/home/ubuntu/mindulle-design-system/packages/tokens
export SONAGI_ASSETS_DIR=/home/ubuntu/sonagi-assets

python3 token_sync.py                       # 검사 + 산출물 → reports/design_tokens/
python3 -m pytest test_token_sync.py -q     # 해석·검사·결정론·그래프
python3 -m pytest test_figma_apply.py -q    # Figma 쓰기 요청·재읽기 대조
```

| 산출물                | 소비처                                                               |
| --------------------- | -------------------------------------------------------------------- |
| `mds-tokens.css`      | 웹 — `--mds-*` 변수, Light/Dark 블록                                 |
| `tailwind.tokens.cjs` | Tailwind — 색·간격·타이포                                            |
| `craft-swatches.json` | VectorCraft·DesignCraft·PhotoCraft·EffectCraft·FilmCraft 공용 팔레트 |
| `craft-swatches.svg`  | 사람 검수용 스와치 시트                                              |
| `figma-variables.js`  | Figma 콘솔용 배치 스크립트(`DRY_RUN = true` 기본)                     |
| `figma-apply-request.json` | MCP 쓰기 요청 — 컬렉션·모드 라벨·변수 hex                         |
| `token-report.json`   | 해시·검사·경고·승인 기록                                             |

**정본은 고치지 않는다.** 검사에서 나온 결함은 리포트 경고로만 남기고 MDS에서
고친다. 2026-11-24 MDS-71 수정 + 검사 계약 정리 후 실행 기준 **경고 0건, 치명 0건**:

```
before  {"leaves": 301, "resolved": 290, "unresolved": 11, "errors": 0, "warnings": 18}
after   {"leaves": 301, "resolved": 301, "unresolved": 0,  "errors": 0, "warnings": 0}
```

해소된 경고(MDS-71, `@mindulle/tokens`):

- `dangling_alias` 11 → 0 — 합성 타이포가 `{font.sans}` 대신
  `{typography.fontFamily.*}`를 쓴다. 해석 실패한 잎은 산출물에서 뺀다
  (없는 변수를 가리키는 CSS를 만들지 않는다).
- `non_dtcg_value` 4 → 0 — `state.error[-bg]`가 `var(--mds-…)` 대신
  `{semantic.<mode>.color.state.danger[-bg]}`를 쓴다.
- `projection_drift` 1 → 0 — `editorial.accent-ink`를 정본 `text.inverse`(#ffffff)에
  맞췄다(sonagi-assets).

해소된 경고(검사 계약 정리, sonagi-graph):

- `radius_layer_missing` → 검사 지점 이동. radius는 Light/Dark 분기가 없어 semantic
  레이어가 할 일이 없다(ADR 0010 2-Tier). 레이어 유무 대신 **primitive `sm/base/md`의
  값 계약(4/6/8px)** 을 본다 — 계층이 아니라 값이 계약이므로, 8px을 80px로 친 오타를
  잡는 것이 목적이다. `radius_contract_violation`(error) · `radius_contract_missing`
  (warning). `semantic.radius.*`를 만들면 `--mds-radius-*`가 primitive와 이름이
  겹치므로, 테마별 radius 요구가 생길 때 ADR 0010과 함께 연다.
- `space_half_step` → 허용. `spacing.1/3/5`(4·12·20px)는 CHARTER Pillar 2의 4px
  하프스텝이고, 4px 배수면 통과시킨다. `space_grid_violation`(6·10px 등)은 계속
  error다. 하프스텝 목록은 `detail.scales.space_grid.half_step_tokens`에 정보로 남는다.

검사 규칙과 심각도는 `token_sync.py` docstring의 표에 있다. 같은 입력이면 같은
바이트가 나오고(생성 시각·UUID 없음), `artifactSetHash`가 그 지문이다.

Figma 쓰기는 **MCP Desktop Bridge**로 한다(CDP 아님). figma-console은 local 모드에서
WebSocket 서버(`9223~9226`)를 띄우고 `/health`로 `connectedClients`를 알린다.
포트가 열려 있어도 플러그인이 안 붙으면 0이고, 그 상태의 `figma_execute`는
"No files connected"로 끝난다. 그래서 판정은 **`/health` 200 + `connectedClients>0`**
하나뿐이고, 우회 env는 두지 않는다.

```
figma_bridge ─ figma-apply-request.json  (계획 → hex·모드 라벨 요청)
             └ executeScript / readbackScript
figma_apply  ─ 브리지+승인+apply_figma 모두 참일 때만 쓰기 묶음 내보냄
MCP 쪽      ─ readback(전) → execute → readback(후) → verify_figma_readback()
```

실측(2026-11-24, `Pipeline Test` 파일): `created=29 · updated=29(재적용) · conflicts 0`,
재읽기 대조 `matched 29/29 · drift 0 · extra 0`. `python3 -m pytest test_figma_apply.py`
는 Figma 없이 모드 누락·동명 충돌·값 드리프트·잔여 변수 판정을 고정한다.

### 별칭 감사 (`figma_drift.py`, 읽기 전용)

실서비스 파일의 시맨틱 변수는 **별칭**(`state/error → state/danger → crimson/50`)이다.
값을 hex로만 비교하면 전부 `"alias"`로 읽혀 거짓 드리프트가 난다(2026-11-24 감사에서
29건). `figma_drift.py`는 별칭을 모드 이름 따라 끝까지 해석해 계획과 대조한다.

```bash
FIGMA_TOKEN=... python3 figma_drift.py --file-key AEoW19jmlUh3rFgzhhV1vH
python3 figma_drift.py --snapshot test_data/figma/snapshot_mindulle_2026-11-24.json
python3 figma_drift.py --file-key ... --json | jq .drift
```

| 종료 코드 | 의미 |
| --- | --- |
| 0 | 29/29 일치 |
| 1 | 드리프트·별칭 이상(순환·대상 없음)·모드 누락 |
| 2 | **확인 불가** — 토큰 없음/만료(401·403)/네트워크 |

**fail-closed**: 2를 0으로 바꾸지 않는다. 확인 못 한 것을 "이상 없음"으로 보고하면
감사가 없는 것보다 나쁘다.

- 스냅샷 `sonagi.figma.snapshot/1`은 컬렉션·모드를 id로 싣고 별칭을 id로 보존한다
  (`figma_snapshot_script()`). 그래서 `verify_figma_readback()`도 별칭 파일에서 거짓
  드리프트를 내지 않는다.
- 컬렉션명이 계획(`Semantic Colors`)과 파일(`Colors`)에서 다르면 **기대 변수 이름이
  80% 이상 겹치는 컬렉션**으로 붙이고 `collectionFallback: "name-overlap"`으로 밝힌다.
- 픽스처 `test_data/figma/snapshot_mindulle_2026-11-24.json`은 실서비스 파일에서 읽은
  Colors 29 + Color Primitives 43이다(값·별칭 사슬 실측, id는 합성).

## Asset Craft (자연어 → 렌더 후보 → 사람 선택)

`graphs/asset_craft_graph.py`는 브리프 한 줄로 렌더 후보 3개를 만들고, 결정론
린터로 재고, 사람이 고른 뒤 결정 원장을 남긴다. LLM은 픽셀을 만지지 않는다.

```
brief ──▶ scout ──▶ compose ──▶ render(변주 3) ──▶ critic ──▶ select ──▶ [사람] ──▶ approve
 IR        참고 자산     스타일 후보      SVG+manifest      lint 점수      상위 3안       원장
```

```python
from graphs.asset_craft_graph import build_graph

graph = build_graph()
config = {"configurable": {"thread_id": "craft-1"}}
graph.invoke(
    {"brief": "미니멀하고 차분한 랜딩. 금지: 네온",
     "content": "/path/to/content.md"},
    config,
)
# select에서 멈춘다. options를 보고 하나를 고른다.
graph.update_state(config, {"selection": "minimal-desktop-light"})
graph.invoke(None, config)
```

| 환경변수            | 기본                                     | 뜻                           |
| ------------------- | ---------------------------------------- | ---------------------------- |
| `SONAGI_ASSETS_DIR` | `/home/ubuntu/sonagi-assets`             | 렌더러·린터·명부가 있는 곳   |
| `SONAGI_CATALOG`    | `<assets>/test_data/golden/catalog.json` | 자산 카탈로그                |
| `SONAGI_CRAFT_OUT`  | `/tmp/asset_craft`                       | 변주 출력 루트               |
| `SONAGI_CRAFT_REFS` | `8`                                      | 스카우트가 고를 참고 자산 수 |

판단 로직은 `craft.py`에 있고 LangGraph에 의존하지 않는다(`test_craft.py`).

- 톤 사전은 의도적으로 작다: 미니멀·스위스·고급·대담·편집·따뜻·차분
- 스타일 점수 = 톤 가중치 × 축 가중치(palette 3 · type 2 · layout 2 · ornament 1)
- 린트 점수 = 100 − 25×fail − 5×warn, 동점이면 브리프 적합도로 정렬
- FAIL은 선택지에서 빠진다. 남는 후보가 없으면 그래프가 멈춘다
- 레퍼런스는 `license_scope`가 `bundle`/`output`인 것만. `blocked`·junk는 제외
- 스카우트 결과는 `scout` 결정으로 원장에 남는다(`8건 [illustration:3,icon:3,typography:2]: 이름, ...`)

### 레퍼런스 스카우트 (주제어 + 역할 쿼터)

톤 사전은 스타일 후보에만 쓴다. 레퍼런스는 실카탈로그에 톤 태그가 없어서
톤으로 고르면 `Archivo`(arch) · `border-none`(none) 같은 오매칭만 남는다.

1. 브리프에서 주제 명사를 뽑아 `TOPIC_LEXICON`으로 영문 주제어로 옮긴다
   (`민들레 홀씨 유리 문진` → dandelion · seed · glass · paperweight).
   톤·매체 단어(미니멀·랜딩·인쇄물·포스터)는 주제어가 아니다.
2. 카탈로그 `name`·`topics`·주제 태그를 **토큰 집합**으로 비교한다. 부분 문자열은
   쓰지 않는다(`arch` ≠ `Archivo`). 4자 이하 일반어(`none`·`rule`·`deco`)는 버린다.
3. 역할 쿼터를 채운다: illustration 3 · icon 3 · typography 2 (최대 8건).
   슬롯을 못 채우면 다른 역할로 메우지 않고 `shortfall`에 이유를 남긴다.

```python
import craft
report = craft.scout_references(craft.parse_brief("민들레 홀씨 유리 문진"), items)
report["references"]   # name·role·score·matched·reason
report["shortfall"]    # [{role, wanted, got, reason}]
```
