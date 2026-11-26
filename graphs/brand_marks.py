"""brand mark 탐색 — 브리프 하나에서 결정론적 후보 마크를 만든다 (GRAPH-33).

`brand` profile의 기존 모드는 **사용자가 만든 SVG**를 받아 정규화·아웃라인·게이트만
한다. 이 모듈은 그 앞단을 맡는다: 브리프 해시와 후보 인덱스로 시드를 만들고,
monogram·seal·radial 세 가족의 기하학적 마크를 VectorCraft headless로 그린다.

```
brief ──hash──▶ seed ──splitmix64──▶ 파라미터(가족·굵기·회전·색 역할)
                                      │
                                      ▼
                            VectorCraft 명령 목록
                                      │
                                      ▼
                         vectorcraft-cli run --export SVG --export PNG
```

핵심 규칙:

- **난수는 시드에서만** 나온다. `hashlib.sha256` + splitmix64라 같은 브리프·인덱스면
  언제나 같은 좌표·같은 SVG·같은 PNG가 나온다. `random` 모듈은 쓰지 않는다.
- 마크는 **기하 도형**이다(모노그램 획·인장 프레임·방사형 꽃잎). 노이즈·랜덤 점이 아니다.
- 모든 좌표는 아트보드 안전 여백(`SAFE_MARGIN`) 안에 있고, 색은 넘겨받은 팔레트의
  역할(`ink`·`paper`·`accent`·`calm`)만 쓴다.
- 텍스트는 쓰지 않는다. 락업의 워드마크만 VectorCraft `text.create`로 만들고
  `convert --outline-text`로 패스화한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

#: 마크 후보의 기본 아트보드(px). 모든 좌표는 이 사각형 기준이다.
ARTBOARD = 512

#: 후보 가족. 순서대로 인덱스를 돌려 쓴다(같은 가족만 나오지 않게).
FAMILIES: Tuple[str, ...] = ("monogram", "seal", "radial")

DEFAULT_CANDIDATES = 6
MAX_CANDIDATES = 12
MIN_PASSING = 2

#: 사방 안전 여백(비율). 게이트는 이보다 느슨한 4%로 실제 렌더를 검사한다.
SAFE_MARGIN = 0.10

#: 브랜드 profile과 같은 기본 팔레트. profile이 정규화한 팔레트를 넘기면 그것을 쓴다.
DEFAULT_PALETTE: Dict[str, str] = {
    "ink": "#161616",
    "paper": "#fbf9f5",
    "accent": "#b4551f",
    "calm": "#1f4536",
}

MASK64 = (1 << 64) - 1

#: 락업 워드마크 서체. 컨테이너에 설치된 자유 폰트만 쓴다(아웃라인 실패 방지).
LOCKUP_FONT = "Lato"
LOCKUP_STYLE = "Bold"
#: 한글 등 비ASCII 이름은 Lato에 글리프가 없다 — 설치된 폴백 서체로 바꾼다.
LOCKUP_FALLBACK_FONT = "Droid Sans Fallback"
LOCKUP_FALLBACK_STYLE = "Regular"

_PAINT = re.compile(r'\b(fill|stroke)=("([^"]*)")')
_NOT_A_COLOR = {"none", "transparent", "currentcolor", "inherit", "unset"}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class MarkRenderError(RuntimeError):
    """VectorCraft headless 렌더가 산출물을 남기지 못했을 때."""


# ---------------------------------------------------------------------------
# 결정론적 난수 — splitmix64
# ---------------------------------------------------------------------------


class Rng:
    """시드 하나에서만 값을 뽑는 결정론적 난수. 같은 시드 → 같은 수열."""

    __slots__ = ("state",)

    def __init__(self, seed: int) -> None:
        self.state = (int(seed) ^ 0x9E3779B97F4A7C15) & MASK64
        if self.state == 0:
            self.state = 0x1234567890ABCDEF

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        value = self.state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
        return (value ^ (value >> 31)) & MASK64

    def below(self, limit: int) -> int:
        if limit <= 0:
            return 0
        return self.next() % limit

    def pick(self, values: Sequence[Any]) -> Any:
        return values[self.below(len(values))]

    def between(self, low: float, high: float) -> float:
        return low + (high - low) * (self.next() / float(1 << 64))

    def flag(self, probability: float = 0.5) -> bool:
        return (self.next() / float(1 << 64)) < probability


# ---------------------------------------------------------------------------
# 시드·후보 계획
# ---------------------------------------------------------------------------


def brief_digest(brief: str, concept: str = "", seed_hint: str = "") -> str:
    """브리프(+개념·시드 힌트)의 안정 지문. 산출물 meta에 그대로 남긴다."""
    payload = "\x1f".join([str(brief or ""), str(concept or ""), str(seed_hint or "")])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seed_for(brief: str, index: int, *, concept: str = "", seed_hint: str = "") -> int:
    """후보 하나의 64비트 시드. 브리프·개념·힌트·인덱스가 모두 들어간다."""
    digest = brief_digest(brief, concept, seed_hint)
    material = f"{digest}:{int(index)}".encode("utf-8")
    return int(hashlib.sha256(material).hexdigest()[:16], 16)


def _point(x: float, y: float) -> Tuple[float, float]:
    """0~1 정규 좌표 → 안전 여백 안의 아트보드 좌표."""
    span = ARTBOARD * (1.0 - 2.0 * SAFE_MARGIN)
    return (ARTBOARD * SAFE_MARGIN + x * span, ARTBOARD * SAFE_MARGIN + y * span)


def _segment_quad(
    start: Tuple[float, float], end: Tuple[float, float], weight: float
) -> List[Tuple[float, float]]:
    """두 점을 잇는 두께 `weight`의 사각형 네 꼭짓점(대각선 획)."""
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    nx = -dy / length * weight / 2.0
    ny = dx / length * weight / 2.0
    return [
        (x1 + nx, y1 + ny),
        (x2 + nx, y2 + ny),
        (x2 - nx, y2 - ny),
        (x1 - nx, y1 - ny),
    ]


#: 모노그램 획 사전 — 0~1 상자 안의 선분. 활자가 아니라 기하 획이다.
GLYPHS: Dict[str, Tuple[Tuple[float, float, float, float], ...]] = {
    "H": ((0.08, 0.05, 0.08, 0.95), (0.92, 0.05, 0.92, 0.95), (0.08, 0.5, 0.92, 0.5)),
    "N": ((0.10, 0.95, 0.10, 0.05), (0.90, 0.05, 0.90, 0.95), (0.10, 0.05, 0.90, 0.95)),
    "A": ((0.08, 0.95, 0.5, 0.05), (0.92, 0.95, 0.5, 0.05), (0.24, 0.62, 0.76, 0.62)),
    "K": ((0.12, 0.05, 0.12, 0.95), (0.14, 0.52, 0.88, 0.05), (0.24, 0.44, 0.90, 0.95)),
    "X": ((0.06, 0.06, 0.94, 0.94), (0.94, 0.06, 0.06, 0.94)),
    "M": (
        (0.06, 0.95, 0.06, 0.05),
        (0.94, 0.95, 0.94, 0.05),
        (0.06, 0.05, 0.5, 0.58),
        (0.94, 0.05, 0.5, 0.58),
    ),
    "T": ((0.04, 0.10, 0.96, 0.10), (0.5, 0.10, 0.5, 0.95)),
    "S": (
        (0.88, 0.10, 0.12, 0.10),
        (0.12, 0.10, 0.12, 0.48),
        (0.12, 0.48, 0.88, 0.52),
        (0.88, 0.52, 0.88, 0.90),
        (0.88, 0.90, 0.12, 0.90),
    ),
    "D": (
        (0.12, 0.05, 0.12, 0.95),
        (0.12, 0.05, 0.55, 0.05),
        (0.55, 0.05, 0.88, 0.32),
        (0.88, 0.32, 0.88, 0.68),
        (0.88, 0.68, 0.55, 0.95),
        (0.55, 0.95, 0.12, 0.95),
    ),
}


def _monogram(rng: Rng) -> Dict[str, Any]:
    glyph = rng.pick(tuple(GLYPHS))
    segments = GLYPHS[glyph]
    weight = rng.between(48, 82)
    accent_index = rng.below(len(segments)) if rng.flag(0.72) else -1
    mirror = rng.flag(0.18)
    shapes: List[Dict[str, Any]] = []
    for index, (x1, y1, x2, y2) in enumerate(segments):
        if mirror:
            x1, x2 = 1.0 - x1, 1.0 - x2
        points = _segment_quad(_point(x1, y1), _point(x2, y2), weight)
        shapes.append(
            {
                "kind": "quad",
                "points": [[round(x, 3), round(y, 3)] for x, y in points],
                "fill": "accent" if index == accent_index else "ink",
            }
        )
    # 작은 악센트 점 — 획이 단조로울 때만.
    if rng.flag(0.28):
        radius = rng.between(12, 20)
        cx, cy = _point(0.92, 0.08) if not mirror else _point(0.08, 0.08)
        shapes.append(
            {
                "kind": "ellipse",
                "cx": round(cx, 3),
                "cy": round(cy, 3),
                "rx": round(radius, 3),
                "ry": round(radius, 3),
                "fill": "accent",
            }
        )
    return {
        "family": "monogram",
        "glyph": glyph,
        "weight": round(weight, 3),
        "accent_index": accent_index,
        "mirror": mirror,
        "shapes": shapes,
    }


def _seal(rng: Rng) -> Dict[str, Any]:
    frame = rng.pick(("ring", "rounded"))
    inner = rng.pick(("polygon", "star", "diamond", "chevron"))
    sides = rng.pick((3, 4, 5, 6, 8))
    ring_weight = rng.between(16, 26)
    inner_radius = rng.between(108, 148)
    rotation = rng.between(0, 360)
    inner_role = rng.pick(("accent", "calm"))
    shapes: List[Dict[str, Any]] = []

    if frame == "ring":
        shapes.append(
            {
                "kind": "ellipse",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "rx": 188,
                "ry": 188,
                "stroke": "ink",
                "weight": round(ring_weight, 3),
            }
        )
    else:
        inset = ARTBOARD * SAFE_MARGIN + 12
        shapes.append(
            {
                "kind": "rect",
                "x": round(inset, 3),
                "y": round(inset, 3),
                "w": round(ARTBOARD - 2 * inset, 3),
                "h": round(ARTBOARD - 2 * inset, 3),
                "radius": 86,
                "stroke": "ink",
                "weight": round(ring_weight, 3),
            }
        )

    if inner == "polygon":
        shapes.append(
            {
                "kind": "polygon",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "radius": round(inner_radius, 3),
                "sides": sides,
                "rotation": round(rotation, 3),
                "fill": inner_role,
            }
        )
    elif inner == "star":
        shapes.append(
            {
                "kind": "star",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "radius1": round(inner_radius, 3),
                "radius2": round(inner_radius * 0.52, 3),
                "points": rng.pick((5, 6, 7)),
                "rotation": round(rotation, 3),
                "fill": inner_role,
            }
        )
    elif inner == "diamond":
        shapes.append(
            {
                "kind": "polygon",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "radius": round(inner_radius * 1.12, 3),
                "sides": 4,
                "rotation": 45.0,
                "fill": inner_role,
            }
        )
    else:
        # 안쪽 V자 — 두꺼운 두 획.
        low = _point(0.30, 0.66)
        mid = _point(0.50, 0.34)
        high = _point(0.70, 0.66)
        for start, end in ((low, mid), (mid, high)):
            points = _segment_quad(start, end, rng.between(38, 54))
            shapes.append(
                {
                    "kind": "quad",
                    "points": [[round(x, 3), round(y, 3)] for x, y in points],
                    "fill": inner_role,
                }
            )

    if rng.flag(0.72):
        diameter = rng.between(44, 68)
        shapes.append(
            {
                "kind": "ellipse",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "rx": round(diameter / 2, 3),
                "ry": round(diameter / 2, 3),
                "fill": rng.pick(("ink", "accent", "paper")),
            }
        )

    dots = rng.pick((0, 0, 4, 8))
    for index in range(dots):
        angle = math.radians(rotation + index * (360.0 / dots))
        cx = ARTBOARD / 2 + 188 * math.cos(angle)
        cy = ARTBOARD / 2 + 188 * math.sin(angle)
        diameter = rng.between(12, 22)
        shapes.append(
            {
                "kind": "ellipse",
                "cx": round(cx, 3),
                "cy": round(cy, 3),
                "rx": round(diameter / 2, 3),
                "ry": round(diameter / 2, 3),
                "fill": "accent",
            }
        )
    return {
        "family": "seal",
        "frame": frame,
        "inner": inner,
        "sides": sides,
        "ring_weight": round(ring_weight, 3),
        "rotation": round(rotation, 3),
        "shapes": shapes,
    }


def _radial(rng: Rng) -> Dict[str, Any]:
    mode = rng.pick(("petals", "rays", "rings"))
    count = rng.pick((5, 6, 7, 8, 10, 12))
    rotation = rng.between(0, 360)
    shapes: List[Dict[str, Any]] = []

    if mode == "petals":
        orbit = rng.between(84, 104)
        petal_w = rng.between(62, 84)
        petal_h = rng.between(140, 176)
        for index in range(count):
            angle = rotation + index * (360.0 / count)
            radians = math.radians(angle)
            cx = ARTBOARD / 2 + orbit * math.cos(radians)
            cy = ARTBOARD / 2 + orbit * math.sin(radians)
            shapes.append(
                {
                    "kind": "ellipse",
                    "cx": round(cx, 3),
                    "cy": round(cy, 3),
                    "rx": round(petal_w / 2, 3),
                    "ry": round(petal_h / 2, 3),
                    "rotation": round(angle, 3),
                    "fill": "accent" if index % 2 == 0 else "calm",
                }
            )
        shapes.append(
            {
                "kind": "ellipse",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "rx": rng.between(34, 48),
                "ry": rng.between(34, 48),
                "fill": "ink",
            }
        )
    elif mode == "rays":
        orbit = rng.between(118, 140)
        size = rng.between(58, 78)
        for index in range(count):
            angle = rotation + index * (360.0 / count)
            radians = math.radians(angle)
            cx = ARTBOARD / 2 + orbit * math.cos(radians)
            cy = ARTBOARD / 2 + orbit * math.sin(radians)
            shapes.append(
                {
                    "kind": "polygon",
                    "cx": round(cx, 3),
                    "cy": round(cy, 3),
                    "radius": round(size, 3),
                    "sides": 3,
                    "rotation": round(angle + 90.0, 3),
                    "fill": "accent" if index % 2 == 0 else "ink",
                }
            )
        shapes.append(
            {
                "kind": "ellipse",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "rx": rng.between(30, 42),
                "ry": rng.between(30, 42),
                "fill": "calm",
            }
        )
    else:
        radii = (66, 108, 150, 190)
        weights = (20, 12, 20, 12)
        for index, radius in enumerate(radii):
            shapes.append(
                {
                    "kind": "ellipse",
                    "cx": ARTBOARD / 2,
                    "cy": ARTBOARD / 2,
                    "rx": radius,
                    "ry": radius,
                    "stroke": "ink" if index % 2 == 0 else "accent",
                    "weight": weights[index],
                }
            )
        shapes.append(
            {
                "kind": "ellipse",
                "cx": ARTBOARD / 2,
                "cy": ARTBOARD / 2,
                "rx": rng.between(26, 38),
                "ry": rng.between(26, 38),
                "fill": "calm",
            }
        )
    return {
        "family": "radial",
        "mode": mode,
        "count": count,
        "rotation": round(rotation, 3),
        "shapes": shapes,
    }


_FAMILY_BUILDERS: Dict[str, Callable[[Rng], Dict[str, Any]]] = {
    "monogram": _monogram,
    "seal": _seal,
    "radial": _radial,
}


def plan_candidates(
    brief: str,
    *,
    concept: str = "",
    count: int = DEFAULT_CANDIDATES,
    seed_hint: str = "",
) -> List[Dict[str, Any]]:
    """브리프 → 후보 계획. 같은 입력이면 언제나 같은 목록(색은 여기 없다)."""
    total = int(count)
    if total < 1 or total > MAX_CANDIDATES:
        raise ValueError(
            f"brand explore: candidates는 1~{MAX_CANDIDATES}개여야 한다 — {count!r}"
        )
    plans: List[Dict[str, Any]] = []
    seen: set = set()
    for index in range(total):
        seed = seed_for(brief, index, concept=concept, seed_hint=seed_hint)
        family = FAMILIES[index % len(FAMILIES)]
        params = _FAMILY_BUILDERS[family](Rng(seed))
        token = f"{seed & 0xFFFFFFFFFFFF:012x}"
        candidate_id = f"{family}-{token}"
        if candidate_id in seen:  # 48비트 충돌은 사실상 없지만 계약은 지킨다.
            candidate_id = f"{candidate_id}-{index}"
        seen.add(candidate_id)
        plans.append(
            {
                "id": candidate_id,
                "family": family,
                "index": index,
                "seed": token,
                "seed64": seed,
                "params": params,
            }
        )
    return plans


# ---------------------------------------------------------------------------
# VectorCraft 명령
# ---------------------------------------------------------------------------


def _polygon_d(points: Iterable[Sequence[float]]) -> str:
    rows = list(points)
    if not rows:
        return ""
    head = f"M{rows[0][0]:g} {rows[0][1]:g}"
    body = "".join(f" L{x:g} {y:g}" for x, y in rows[1:])
    return f"{head}{body} Z"


def _resolve(role: Optional[str], palette: Mapping[str, str]) -> Optional[str]:
    if not role or role == "none":
        return None
    value = palette.get(role) or palette.get("ink")
    return hex6(str(value)) if value else None


def hex6(value: str) -> str:
    """VectorCraft paint용 6자리 hex. `#rrggbbaa`의 알파는 버린다."""
    text = str(value or "").strip().lower()
    digits = text.lstrip("#")
    if len(digits) == 8:
        return "#" + digits[:6]
    if len(digits) == 4:  # #rgba
        return "#" + "".join(char * 2 for char in digits[:3])
    if len(digits) == 3:
        return "#" + "".join(char * 2 for char in digits)
    return text


def _transform_points(
    points: Iterable[Sequence[float]],
    *,
    origin: Tuple[float, float],
    scale: float,
) -> List[Tuple[float, float]]:
    return [
        (round(origin[0] + float(x) * scale, 3), round(origin[1] + float(y) * scale, 3))
        for x, y in points
    ]


def _shape_command(
    shape: Mapping[str, Any],
    *,
    origin: Tuple[float, float],
    scale: float,
) -> Dict[str, Any]:
    kind = str(shape["kind"])
    ox, oy = origin
    if kind == "quad":
        points = _transform_points(shape["points"], origin=origin, scale=scale)
        return {"cmd": "path.create", "params": {"d": _polygon_d(points)}}
    if kind == "ellipse":
        rx = float(shape["rx"]) * scale
        ry = float(shape["ry"]) * scale
        cx = ox + float(shape["cx"]) * scale
        cy = oy + float(shape["cy"]) * scale
        params: Dict[str, Any] = {
            "x": round(cx - rx, 3),
            "y": round(cy - ry, 3),
            "width": round(rx * 2, 3),
            "height": round(ry * 2, 3),
        }
        if float(shape.get("rotation") or 0.0):
            params["rotation"] = round(float(shape["rotation"]), 3)
        return {"cmd": "shape.ellipse", "params": params}
    if kind == "rect":
        return {
            "cmd": "shape.rectangle",
            "params": {
                "x": round(ox + float(shape["x"]) * scale, 3),
                "y": round(oy + float(shape["y"]) * scale, 3),
                "width": round(float(shape["w"]) * scale, 3),
                "height": round(float(shape["h"]) * scale, 3),
                **(
                    {"radius": round(float(shape["radius"]) * scale, 3)}
                    if shape.get("radius")
                    else {}
                ),
            },
        }
    if kind == "polygon":
        return {
            "cmd": "shape.polygon",
            "params": {
                "cx": round(ox + float(shape["cx"]) * scale, 3),
                "cy": round(oy + float(shape["cy"]) * scale, 3),
                "radius": round(float(shape["radius"]) * scale, 3),
                "sides": int(shape.get("sides") or 6),
                "rotation": round(float(shape.get("rotation") or 0.0), 3),
            },
        }
    if kind == "star":
        return {
            "cmd": "shape.star",
            "params": {
                "cx": round(ox + float(shape["cx"]) * scale, 3),
                "cy": round(oy + float(shape["cy"]) * scale, 3),
                "radius1": round(float(shape["radius1"]) * scale, 3),
                "radius2": round(float(shape["radius2"]) * scale, 3),
                "points": int(shape.get("points") or 5),
                "rotation": round(float(shape.get("rotation") or 0.0), 3),
            },
        }
    raise MarkRenderError(f"brand explore: 알 수 없는 도형 kind={kind!r}")


def shape_color_roles(shapes: Sequence[Mapping[str, Any]]) -> List[str]:
    """도형이 쓰는 팔레트 역할 목록(게이트·테스트·문서용)."""
    roles: List[str] = []
    for shape in shapes:
        for key in ("fill", "stroke"):
            role = shape.get(key)
            if role and role != "none" and role not in roles:
                roles.append(str(role))
    return roles


def _paint_commands(
    shape: Mapping[str, Any],
    *,
    palette: Mapping[str, str],
    scale: float,
) -> List[Dict[str, Any]]:
    commands: List[Dict[str, Any]] = []
    fill = _resolve(shape.get("fill"), palette)
    stroke = _resolve(shape.get("stroke"), palette)
    if fill:
        commands.append({"cmd": "paint.setFill", "params": {"color": fill}})
    else:
        commands.append({"cmd": "paint.setFill", "params": {"none": True}})
    if stroke:
        commands.append({"cmd": "paint.setStroke", "params": {"color": stroke}})
        weight = shape.get("weight")
        if weight:
            commands.append(
                {
                    "cmd": "stroke.set",
                    "params": {"weight": round(float(weight) * scale, 3)},
                }
            )
    else:
        commands.append({"cmd": "paint.setStroke", "params": {"none": True}})
    return commands


def mark_commands(
    shapes: Sequence[Mapping[str, Any]],
    palette: Optional[Mapping[str, str]] = None,
    *,
    canvas: Tuple[int, int] = (ARTBOARD, ARTBOARD),
    origin: Tuple[float, float] = (0.0, 0.0),
    scale: float = 1.0,
    name: str = "brand-mark",
) -> List[Dict[str, Any]]:
    """도형 목록 → VectorCraft headless 명령 목록(결정론)."""
    colors = palette or DEFAULT_PALETTE
    width, height = int(canvas[0]), int(canvas[1])
    commands: List[Dict[str, Any]] = [
        {
            "cmd": "file.new",
            "params": {
                "width": width,
                "height": height,
                "units": "Pixels",
                "backgroundContents": "transparent",
                "name": name,
            },
        }
    ]
    for shape in shapes:
        commands.append(_shape_command(shape, origin=origin, scale=scale))
        commands.extend(_paint_commands(shape, palette=colors, scale=scale))
    return commands


def candidate_commands(
    candidate: Mapping[str, Any],
    palette: Optional[Mapping[str, str]] = None,
    *,
    canvas: Tuple[int, int] = (ARTBOARD, ARTBOARD),
    origin: Tuple[float, float] = (0.0, 0.0),
    scale: float = 1.0,
    name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    params = candidate.get("params") or {}
    shapes = params.get("shapes") or []
    return mark_commands(
        shapes,
        palette,
        canvas=canvas,
        origin=origin,
        scale=scale,
        name=name or f"brand-{candidate.get('id', 'mark')}",
    )


def lockup_commands(
    candidate: Mapping[str, Any],
    name: str,
    palette: Optional[Mapping[str, str]] = None,
    *,
    canvas: Tuple[int, int] = (1600, 600),
) -> List[Dict[str, Any]]:
    """마크 + 워드마크 가로 락업 명령. 마크는 512 상자를 0.72로 줄여 왼쪽에 둔다."""
    colors = palette or DEFAULT_PALETTE
    font, style = (
        (LOCKUP_FALLBACK_FONT, LOCKUP_FALLBACK_STYLE)
        if any(ord(char) > 127 for char in str(name))
        else (LOCKUP_FONT, LOCKUP_STYLE)
    )
    commands = candidate_commands(
        candidate,
        colors,
        canvas=canvas,
        origin=(80.0, 84.0),
        scale=0.72,
        name=f"{name} lockup",
    )
    commands.extend(
        [
            {
                "cmd": "text.create",
                "params": {
                    "x": 520,
                    "y": 190,
                    "text": str(name),
                    "size": 132,
                    # 영역을 넘치면 자동으로 줄인다 — 긴 브리프 이름도 캔버스 안에 둔다.
                    "area": {
                        "width": 1000,
                        "height": 240,
                        "fit": "shrinkText",
                        "fitMinPercent": 30,
                    },
                    "justify": "left",
                    "font": font,
                    "style": style,
                    "color": hex6(colors.get("ink", "#161616")),
                },
            },
            {"cmd": "paint.setFill", "params": {"color": colors.get("ink", "#161616")}},
            {"cmd": "paint.setStroke", "params": {"none": True}},
        ]
    )
    return commands


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def export_argv(
    binary: str,
    commands: Sequence[Mapping[str, Any]],
    exports: Sequence[Path],
) -> List[str]:
    """`vectorcraft-cli run --cmd … --export …` 인자 목록."""
    argv: List[str] = [str(binary), "run"]
    for command in commands:
        argv += ["--cmd", str(command["cmd"]), "--params", _json(command["params"])]
    for path in exports:
        argv += ["--export", str(path)]
    return argv


def run_commands(
    runner: Callable[..., Any],
    binary: str,
    commands: Sequence[Mapping[str, Any]],
    exports: Sequence[Path],
    *,
    timeout: float = 180,
) -> List[str]:
    """명령을 실행하고 내보낸 파일이 실제로 생겼는지 확인한다(fail closed)."""
    paths = [Path(path) for path in exports]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            path.unlink()
    argv = export_argv(binary, commands, paths)
    try:
        runner(argv, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise MarkRenderError(
            "brand explore: vectorcraft-cli headless 실행에 실패했다 — "
            "`VECTORCRAFT_BIN`/마운트를 확인하라: "
            f"{str(exc).strip()[:400]}"
        ) from exc
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise MarkRenderError(
            "brand explore: VectorCraft가 산출물을 남기지 않았다 — "
            f"{', '.join(missing)}"
        )
    empty = [str(path) for path in paths if path.stat().st_size <= 0]
    if empty:
        raise MarkRenderError(f"brand explore: 빈 산출물이 나왔다 — {', '.join(empty)}")
    return argv


def render_candidate(
    runner: Callable[..., Any],
    binary: str,
    candidate: Mapping[str, Any],
    out_dir: Path,
    palette: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """후보 하나를 SVG·PNG로 렌더한다. 두 파일이 없으면 `MarkRenderError`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    svg = out / "candidate.svg"
    png = out / "candidate.png"
    commands = candidate_commands(candidate, palette)
    run_commands(runner, binary, commands, [svg, png])
    return {
        "svg": str(svg),
        "png": str(png),
        "command_count": len(commands),
        "sha256": hashlib.sha256(svg.read_bytes()).hexdigest(),
    }


def render_lockup(
    runner: Callable[..., Any],
    binary: str,
    candidate: Mapping[str, Any],
    name: str,
    out_dir: Path,
    palette: Optional[Mapping[str, str]] = None,
    *,
    pdf: bool = True,
) -> Dict[str, Optional[str]]:
    """락업을 SVG(아웃라인)·PDF로 만든다. 텍스트는 `convert --outline-text`로 패스화."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "lockup-raw.svg"
    svg = out / "lockup.svg"
    commands = lockup_commands(candidate, name, palette)
    run_commands(runner, binary, commands, [raw])
    try:
        runner(
            [str(binary), "convert", str(raw), str(svg), "--outline-text"], timeout=180
        )
    except Exception as exc:  # noqa: BLE001
        raise MarkRenderError(
            "brand explore: 락업 텍스트를 아웃라인하지 못했다 — "
            f"폰트({LOCKUP_FONT}) 설치를 확인하라: {str(exc).strip()[:300]}"
        ) from exc
    if not svg.is_file() or svg.stat().st_size <= 0:
        raise MarkRenderError(f"brand explore: 락업 SVG가 비었다 — {svg}")
    pdf_path: Optional[str] = None
    if pdf:
        target = out / "lockup.pdf"
        runner([str(binary), "convert", str(svg), str(target)], timeout=180)
        if not target.is_file() or target.stat().st_size <= 0:
            raise MarkRenderError(f"brand explore: 락업 PDF가 비었다 — {target}")
        pdf_path = str(target)
    return {"svg": str(svg), "pdf": pdf_path, "raw": str(raw)}


# ---------------------------------------------------------------------------
# 색·픽셀 검사 (게이트가 쓴다)
# ---------------------------------------------------------------------------


def svg_colors(svg_text: str) -> List[str]:
    """fill/stroke에 실제로 지정된 색값. none·url()은 뺀다(소문자 정규화)."""
    colors = set()
    for match in _PAINT.finditer(str(svg_text)):
        value = (match.group(3) or "").strip().lower()
        if not value or value in _NOT_A_COLOR or value.startswith("url("):
            continue
        colors.add(value)
    return sorted(colors)


def single_color_svg(svg_text: str, color: str) -> str:
    """모든 유색 채우기·선을 `color` 하나로 바꾼다(단색 변형)."""
    solid = hex6(color)

    def replace(match: "re.Match[str]") -> str:
        value = (match.group(3) or "").strip().lower()
        if not value or value in _NOT_A_COLOR or value.startswith("url("):
            return match.group(0)
        return f'{match.group(1)}="{solid}"'

    return _PAINT.sub(replace, str(svg_text))


def image_stats(path: Path) -> Dict[str, Any]:
    """불투명 픽셀 수·비율·바운딩박스. 알파 128 초과만 센다."""
    from PIL import Image

    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        alpha = rgba.getchannel("A")
        mask = alpha.point(lambda value: 255 if value > 128 else 0)
        bbox = mask.getbbox()
        opaque = mask.histogram()[255]
    return {
        "width": width,
        "height": height,
        "opaque": opaque,
        "ratio": (opaque / (width * height)) if width * height else 0.0,
        "bbox": list(bbox) if bbox else None,
    }


def bbox_margins(stats: Mapping[str, Any]) -> Dict[str, float]:
    """바운딩박스의 사방 여백 비율(아트보드 기준). 마크가 없으면 전부 0."""
    width = float(stats.get("width") or 0)
    height = float(stats.get("height") or 0)
    bbox = stats.get("bbox")
    if not bbox or width <= 0 or height <= 0:
        return {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}
    left, top, right, bottom = [float(value) for value in bbox]
    return {
        "left": left / width,
        "top": top / height,
        "right": (width - right) / width,
        "bottom": (height - bottom) / height,
    }
