"""taste — 사람의 선택을 다음 실행의 판단으로 남기는 취향 기록 (GRAPH-35).

| 모듈 | 역할 |
| --- | --- |
| `ledger`   | 승인 한 건을 `decisions.jsonl`에 append-only로 남긴다 |
| `features` | 렌더 산출물(SVG/PNG)에서 결정론적 시각 특징을 뽑는다 |
| `rank`     | 취향 프로필 + 후보 특징으로 후보 순서와 이유를 만든다 |
| `profile`  | `profile.json`의 초기값·읽기 (증류는 별도 이슈) |

원칙:

- **덧붙이기만 한다.** 지난 결정 기록은 고치거나 지우지 않는다.
- **없는 통계를 만들지 않는다.** 결정이 30건 미만이면 명시적 태그와
  스타일 승률만 쓰고 신뢰도를 `early`로 표시한다.
- **설명은 사람 말로.** 이 모듈의 출력은 디자이너가 아닌 사용자가 읽는다.
"""

from __future__ import annotations

__all__ = ["features", "ledger", "profile", "rank"]
