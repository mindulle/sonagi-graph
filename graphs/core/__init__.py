"""core — 모든 그래프가 공유하는 실행 토대 (W1).

| 모듈          | 책임                                          |
| ------------- | --------------------------------------------- |
| `env`         | 경로·서비스 주소의 단일 정의                   |
| `exec`        | subprocess 실행(타임아웃·재시도·구조화 에러)   |
| `artifacts`   | run 디렉터리·manifest·sha256·보존              |
| `contract`    | 입력/출력 스키마 + `verify()` 프로토콜         |
| `hitl`        | 승인 게이트 표준 상태                          |

의도적으로 얇다. 실행·경로·기록까지만 맡고, 그래프별 판단(렌더·린트·LLM)은
각 그래프에 남긴다. 하위 모듈은 import 시점에 파일을 만들거나 네트워크를
호출하지 않는다.

사용::

    from graphs.core import artifacts, env
    from graphs.core.exec import run
"""

from __future__ import annotations

__all__ = ["artifacts", "contract", "env", "exec", "hitl"]
