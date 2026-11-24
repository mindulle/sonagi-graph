# Sonagi Graph

Sonagi 생태계의 다양한 에이전틱 그래프(Agentic Graphs)를 정의하고 관리하는 독립 저장소입니다.

## 아키텍처 개요

- **LangGraph** 기반의 Python 코드로 워크플로우를 설계합니다.
- `llmops-instance` 서버에 배포되어 백그라운드 워커로 작동합니다.
- **Paperclip**과 **Kimaki**를 통해 모니터링, 제어(HITL), 오케스트레이션이 이루어집니다.

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
고친다. 2026-11-24 MDS-71 수정 후 실행 기준 **경고 2건, 치명 0건**:

```
before  {"leaves": 301, "resolved": 290, "unresolved": 11, "errors": 0, "warnings": 18}
after   {"leaves": 301, "resolved": 301, "unresolved": 0,  "errors": 0, "warnings": 2}
```

해소된 경고(MDS-71, `@mindulle/tokens`):

- `dangling_alias` 11 → 0 — 합성 타이포가 `{font.sans}` 대신
  `{typography.fontFamily.*}`를 쓴다. 해석 실패한 잎은 산출물에서 뺀다
  (없는 변수를 가리키는 CSS를 만들지 않는다).
- `non_dtcg_value` 4 → 0 — `state.error[-bg]`가 `var(--mds-…)` 대신
  `{semantic.<mode>.color.state.danger[-bg]}`를 쓴다.
- `projection_drift` 1 → 0 — `editorial.accent-ink`를 정본 `text.inverse`(#ffffff)에
  맞췄다(sonagi-assets).

남은 경고 2건은 **정본 계약상 의도된 상태**다. 재실측 때 결함으로 오인하지 말 것:

- `radius_layer_missing` — radius의 3단계 **값** 계약(4/6/8px)은
  `VISUAL_IDENTITY_CHARTER` Pillar 2와 sonagi-assets `RADIUS`가 정하고, 정본은
  primitive `borderRadius.sm/base/md`가 그 값을 갖는다. radius는 Light/Dark 분기가
  없어 semantic 레이어를 두지 않는다(ADR 0010 2-Tier). `semantic.radius.*`를 넣으면
  `--mds-radius-*` 이름이 primitive와 중복되고, 계층 정리는 ADR 0010이 major로 미뤘다.
- `space_half_step` — `spacing.1/3/5`(4·12·20px)는 의도된 4px 하프스텝이다.

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
