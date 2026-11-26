"""플랫폼 프리셋 — 콘테스트 플랫폼별 제출 규격의 정본 (GRAPH-25).

플랫폼마다 이미지 폭·높이 상한, 파일 형식, JPG 품질, 엔트리 보드 생성 여부,
파일 이름 규칙이 다르다. 이 모듈이 그 표를 소유하고, profile(web/print)은
`spec` 값을 먼저 보고 비어 있으면 프리셋 값을 쓴다.

해석 순서:
1. `spec`에 명시한 값(항상 프리셋보다 우선)
2. `spec["platform"]`의 프리셋
3. 모르는 플랫폼이면 `generic` + 경고(`preset_warnings`)
4. `platform`이 없으면 `generic`(경고 없음)

사용::

    from graphs.profiles import presets

    preset, warnings = presets.resolve_for_spec(spec)
    width = int(spec.get("image_width") or preset.image_width)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: 납품 파일 형식. 현재는 PNG·JPG 두 가지이며, 프리셋은 이 부분집합만 쓴다.
KNOWN_FORMATS = ("png", "jpg")

#: `apply()`가 프리셋에서 채우는 키. `spec`에 값이 있으면 덮어쓰지 않는다.
PRESET_KEYS = (
    "image_width",
    "max_height",
    "formats",
    "jpg_quality",
    "board",
    "naming",
)


@dataclass(frozen=True)
class PlatformPreset:
    """플랫폼 하나의 제출 규격. 값은 profile이 그대로 소비한다."""

    id: str
    image_width: int
    max_height: int
    formats: Tuple[str, ...]
    jpg_quality: int
    board: bool
    naming: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "image_width": self.image_width,
            "max_height": self.max_height,
            "formats": list(self.formats),
            "jpg_quality": self.jpg_quality,
            "board": self.board,
            "naming": self.naming,
        }


#: 플랫폼 표. generic은 기존 web profile 기본값과 같아야 한다(동작 불변).
PRESETS: Dict[str, PlatformPreset] = {
    "99designs": PlatformPreset(
        id="99designs",
        image_width=2000,
        max_height=3000,
        formats=("png", "jpg"),
        jpg_quality=90,
        board=True,
        naming="99designs-{index:02d}-{id}",
    ),
    "loudsourcing": PlatformPreset(
        id="loudsourcing",
        image_width=1600,
        max_height=2400,
        formats=("png", "jpg"),
        jpg_quality=88,
        board=True,
        naming="loudsourcing-{index:02d}-{id}",
    ),
    "generic": PlatformPreset(
        id="generic",
        image_width=2000,
        max_height=3000,
        formats=("png", "jpg"),
        jpg_quality=90,
        board=False,
        naming="{id}",
    ),
}

GENERIC = PRESETS["generic"]


def platform_name(value: Any) -> str:
    """플랫폼 이름을 소문자 슬러그로 정규화한다(빈 값은 빈 문자열)."""
    return str(value or "").strip().lower()


def resolve(platform: Optional[str]) -> Tuple[PlatformPreset, List[str]]:
    """플랫폼 이름 → (프리셋, 경고). 모르는 이름은 generic으로 떨어뜨린다."""
    key = platform_name(platform)
    if not key:
        return GENERIC, []
    preset = PRESETS.get(key)
    if preset is None:
        return GENERIC, [
            f"알 수 없는 platform {key!r} — generic 프리셋을 쓴다. "
            f"사용 가능: {', '.join(sorted(PRESETS))}"
        ]
    return preset, []


def resolve_for_spec(
    spec: Optional[Mapping[str, Any]],
) -> Tuple[PlatformPreset, List[str]]:
    """`spec["platform"]`으로 프리셋을 고른다(없으면 generic, 경고 없음)."""
    spec = spec or {}
    return resolve(spec.get("platform"))


def formats_for_spec(
    spec: Mapping[str, Any], preset: PlatformPreset
) -> Tuple[str, ...]:
    """`spec["formats"]`를 정규화한다. 모르는 형식은 조용히 떨어뜨린다."""
    raw = spec.get("formats")
    if raw is None:
        return preset.formats
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[,/\s]+", raw) if part]
    values = tuple(
        dict.fromkeys(
            str(item).strip().lower().lstrip(".") for item in raw if str(item).strip()
        )
    )
    known = tuple(value for value in values if value in KNOWN_FORMATS)
    return known or preset.formats


def filename(pattern: str, item_id: Any, index: int) -> str:
    """이름 패턴을 파일 stem으로 만든다. 경로 구분자·공백은 안전하게 바꾼다.

    지원 자리표시자: `{id}`(변주 id), `{index}`(1부터). 패턴이 깨지면 id로 돌아간다.
    """
    variant = str(item_id)
    try:
        stem = str(pattern).format(id=variant, index=index)
    except (KeyError, IndexError, ValueError):
        stem = variant
    stem = re.sub(r"[\\/\s]+", "-", stem)
    stem = stem.strip("-._")
    return stem or variant


def apply(
    spec: Optional[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], PlatformPreset, List[str]]:
    """프리셋을 spec에 병합한다. 반환값은 (새 spec, 프리셋, 경고).

    `platform`이 없으면 아무것도 채우지 않는다 — 기존 web/print 동작을 건드리지
    않기 위해서다. 플랫폼이 있으면 비어 있는 키만 프리셋 값으로 채우고,
    `preset`(해석된 id)과 경고를 spec에 남긴다.
    """
    merged: Dict[str, Any] = dict(spec or {})
    if not platform_name(merged.get("platform")):
        return merged, GENERIC, []

    preset, warnings = resolve_for_spec(merged)
    for key in PRESET_KEYS:
        if merged.get(key) is None or merged.get(key) == "":
            merged[key] = getattr(preset, key)
    merged["preset"] = preset.id
    if warnings:
        merged["preset_warnings"] = warnings
    return merged, preset, warnings


__all__ = [
    "GENERIC",
    "KNOWN_FORMATS",
    "PRESET_KEYS",
    "PRESETS",
    "PlatformPreset",
    "apply",
    "filename",
    "formats_for_spec",
    "platform_name",
    "resolve",
    "resolve_for_spec",
]
