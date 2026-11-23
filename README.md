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

| SSOT | 내용 |
| --- | --- |
| `tokens/semantic.json` | 팔레트(editorial·architecture) |
| `registry/index.json` | `tokens_version`·`ruleset_version`·스타일 수 |
| `registry/styles.json` | 스타일 `id`·축 조합 |

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
