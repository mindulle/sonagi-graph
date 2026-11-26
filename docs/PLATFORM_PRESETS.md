# 플랫폼 프리셋 · 엔트리 보드 (GRAPH-25)

콘테스트 플랫폼마다 제출 규격이 다르다. `graphs/profiles/presets.py`가 그 표를
소유하고, `web`·`print` profile이 pack 시점에 spec 값을 먼저 본 뒤 빈 키만
프리셋으로 채운다.

## 프리셋 표

| platform | image_width | max_height | formats | jpg_quality | board | naming |
| --- | --- | --- | --- | --- | --- | --- |
| `99designs` | 2000 | 3000 | png, jpg | 90 | ✅ | `99designs-{index:02d}-{id}` |
| `loudsourcing` | 1600 | 2400 | png, jpg | 88 | ✅ | `loudsourcing-{index:02d}-{id}` |
| `generic` | 2000 | 3000 | png, jpg | 90 | ❌ | `{id}` |

`generic` 값은 기존 `web` profile 기본값과 같다. platform을 지정하지 않은
spec은 프리셋 병합 자체를 하지 않으므로 기존 동작이 그대로 유지된다.

## 해석 순서

1. `spec`에 명시한 값 (항상 프리셋보다 우선)
2. `spec["platform"]`에 해당하는 프리셋
3. 모르는 platform → `generic` + `spec["preset_warnings"]`
4. platform 없음 → `generic` (경고 없음)

```python
from graphs.profiles import presets

spec, preset, warnings = presets.apply(
    {"platform": "99designs", "image_width": 1600}
)
# spec["image_width"] == 1600  (spec 우선)
# spec["max_height"] == 3000   (프리셋)
# spec["board"] is True
```

pack meta에는 `preset`, `preset_warnings`, `formats`, `naming`, `board`가 남는다.

## 엔트리 보드

`preset.board`가 true이고 선택된 엔트리가 있으면 `pack/board.png`를 만든다.

- 크기: **1920×1080** 고정
- 왼쪽: 선택 1순위 엔트리 이미지를 패널 안에 비율 유지로 맞춤
- 오른쪽: 스타일·테마, 엔트리 id, 방향 설명(자동 줄바꿈)
- 헤더: 플랫폼 라벨 + 방향 제목 / 푸터: `{PLATFORM} · ENTRY BOARD`
- Pillow만 사용. 좌표·색·글꼴 크기는 상수라 같은 입력이면 같은 바이트가 나온다.
- 글꼴을 하나도 읽지 못하면 Pillow 기본 글꼴로 떨어지고 meta의
  `board.font_fallback`이 `true`가 된다.

보드 경로와 크기·폰트 정보는 `meta.json`의 `board`와 `entry.md` 첫머리에
기록되고, run manifest 산출물에도 포함된다.
