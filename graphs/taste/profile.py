"""taste/profile — 취향 프로필 파일의 초기값과 읽기 (GRAPH-35).

프로필은 "다음에 무엇을 먼저 보여줄까"를 정하는 요약본이다. 이번 이슈에서는
**빈 프로필을 처음 한 번 만들고 읽는 일**만 한다. 결정 기록을 프로필로
증류하는 일(태그 가중치·특징 편향 갱신)은 별도 이슈에서 다룬다.

파일 위치::

    SONAGI_TASTE_DIR/profile.json      # 기본 /home/ubuntu/sonagi-taste/profile.json

형식::

    {
      "version": 1,
      "updated_at": "2026-11-26T00:00:00Z",
      "decisions": 0,
      "tag_weights": {},
      "style_winrate": {},
      "feature_bias": {},
      "confidence": "early",
      "notes": ""
    }

`load_profile()`은 파일이 없으면 초기값을 **한 번만** 쓴다. 이미 있으면
손대지 않는다(사용자가 손으로 고친 값을 덮어쓰지 않는다).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

#: 취향 기록 루트. 테스트·다른 호스트는 이 환경 변수로 갈아끼운다.
TASTE_DIR_ENV = "SONAGI_TASTE_DIR"
DEFAULT_TASTE_DIR = "/home/ubuntu/sonagi-taste"

PROFILE_NAME = "profile.json"
PROFILE_VERSION = 1

#: 이 건수 미만이면 통계를 만들지 않고 `early`로 표시한다.
EARLY_THRESHOLD = 30


def taste_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """취향 기록 루트. 없으면 만들지 않는다(쓰기 직전에 만든다)."""
    source = os.environ if environ is None else environ
    return Path(source.get(TASTE_DIR_ENV) or DEFAULT_TASTE_DIR).expanduser()


def profile_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    return taste_dir(environ) / PROFILE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def initial_profile(now: Optional[str] = None) -> Dict[str, Any]:
    """아직 아무것도 배우지 않은 프로필. 필드는 고정한다."""
    return {
        "version": PROFILE_VERSION,
        "updated_at": now or _now(),
        "decisions": 0,
        "tag_weights": {},
        "style_winrate": {},
        "feature_bias": {},
        "confidence": "early",
        "notes": "",
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """임시 파일에 쓴 뒤 교체한다 — 반쯤 쓰인 JSON을 남기지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)


def load_profile(
    path: Optional[Path] = None,
    *,
    create: bool = True,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """프로필을 읽는다. 없으면 초기값을 쓰고 돌려준다.

    파일이 깨졌으면 초기값을 돌려주되 `notes`에 이유를 남긴다. 승인·조회가
    프로필 손상 때문에 멈추면 안 되기 때문이다.
    """
    target = Path(path) if path is not None else profile_path(environ)
    if not target.is_file():
        profile = initial_profile()
        if create:
            _write_json_atomic(target, profile)
        return profile
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        profile = initial_profile()
        profile["notes"] = f"프로필을 읽지 못했습니다: {type(exc).__name__}"
        return profile
    if not isinstance(loaded, dict):
        profile = initial_profile()
        profile["notes"] = "프로필 형식이 사전(dict)이 아닙니다"
        return profile
    # 빠진 필드는 기본값으로 채운다(이전 버전 파일도 읽을 수 있게).
    profile = initial_profile()
    profile.update(loaded)
    return profile
