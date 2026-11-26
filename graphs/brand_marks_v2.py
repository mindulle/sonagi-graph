"""마크 생성 v2 — 기존 인장을 **정련**한다 (GRAPH-33 후속).

v1(`graphs/brand_marks.py`)은 브리프에서 기하 프리미티브(링·삼각형·별)를 새로
그렸다. 게이트는 통과했지만 우리가 이미 가진 마스터 인장보다 수준이 낮아
"범용 로고 생성기"처럼 보였다. v2는 반대 방향이다: **원본 SVG의 구조를 읽고**
그 구조만 남겨 16px에서 살아남게 다듬는다.

```
source.svg ──analyze──▶ SourceModel (링 비율·방사 각도·코어 비율·씨앗 점)
                              │
brief hash + index ──seed──▶ 파라미터(링 수·획 단위·씨앗 밀도·컷 깊이)
                              │
                              ▼
                     가족별 기하(순수 함수) ──▶ 후보 SVG
```

원칙:

- **원본에서 온 것만 쓴다.** 방사 각도(20°), 링 비율(1.0·0.94·0.67·0.64),
  코어 비율(0.075), 씨앗 점 비율은 `SourceModel`에서 읽는다. 별·다각형 같은
  프리미티브를 새로 발명하지 않는다.
- **시드 결정론.** `hashlib.sha256` + splitmix64만 쓴다. 같은 브리프·인덱스·
  원본이면 같은 좌표·같은 SVG가 나온다. `random` 모듈은 쓰지 않는다.
- **제작 규칙은 게이트로 강제한다.** 획 굵기는 1.0/1.5/2.0 배율로 양자화하고,
  원형에는 광학 오버슈트를 주며, 색은 잉크·악센트·종이 셋뿐이다(그라디언트 금지).
- **자동 선택 없음.** 후보는 HITL에 전부 올리고 사람이 고른다(profile 쪽 규칙).

가족 셋 — 모두 같은 인장의 변주다. 바깥 링 골격은 후보마다 그대로 남는다:

| 가족 | 원본에서 물려받는 것 | 바꾸는 것 |
| --- | --- | --- |
| `seal-refined` | 링 비율·방사 각도·코어 | 마이크로 디테일 제거, 링 수·획 굵기 |
| `seal-monogram` | 링 골격·코어 위치 | 이름 머리글자를 중앙에 광학 정렬 |
| `seal-dandelion` | 방사 각도·줄기 길이 | 씨앗 밀도·줄기 굽힘·깃털 |

음각(`negative-cut`)은 **네 번째 가족이 아니라 수정자**다. 세 가족의 중심부를
원판 + 구멍(nonzero)으로 바꾸되 바깥 인장 링은 건드리지 않는다. 그래서 어떤
후보도 글자만·별만 남은 새 로고가 아니라 "같은 인장의 다른 정련"으로 읽힌다.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from graphs import brand_marks

ARTBOARD = 512
CENTER = ARTBOARD / 2.0

#: 후보 가족. 기본값은 이 순서를 그대로 돌려 쓴다(같은 가족이 몰리지 않게).
FAMILIES: Tuple[str, ...] = (
    "seal-refined",
    "seal-monogram",
    "seal-dandelion",
)

#: 가족 위에 얹는 수정자. 음각은 네 번째 가족이 아니라 1~3의 변형이다.
MODIFIERS: Tuple[str, ...] = ("none", "negative-cut")

#: refine 모드의 후보 수 계약(8~12). v1 explore의 2~12와 다르다.
MIN_CANDIDATES = 8
MAX_CANDIDATES = 12
DEFAULT_CANDIDATES = 8

#: 광학 획 배율. 마크 하나 안에서는 이 세 값의 조합만 쓴다.
OPTICAL_SCALE: Tuple[float, ...] = (1.0, 1.5, 2.0)

#: refine 모드가 쓰는 색 역할. `calm`은 의도적으로 뺀다.
PALETTE_ROLES: Tuple[str, ...] = ("paper", "ink", "accent")

#: 후보 원판 반지름(512 아트보드). 원본 인장(400 기준 r188)보다 안쪽으로 넣어
#: 256px 게이트의 사방 여백 4%와 16px 아이콘 여백을 함께 만족시킨다.
OUTER_RADIUS = 190.0

#: 광학 오버슈트 비율(반지름 대비). 원형 링이 사각 그리드에서 작아 보이는 것을
#: 보정한다. 게이트는 0.4~2.0% 범위만 통과시킨다.
OVERSHOOT_RANGE: Tuple[float, float] = (0.004, 0.02)

#: 16px에서 카운터(글자 속 빈 공간)가 닫히지 않기 위한 최소 간격(px).
COUNTER_MIN_PX = 0.75

#: refine 게이트가 추가로 요구하는 id. 하나라도 빠지면 통과로 세지 않는다.
REFINE_GATE_IDS = frozenset(
    {
        "candidate.palette_roles",
        "candidate.no_gradient",
        "candidate.stroke_scale",
        "candidate.optical_overshoot",
        "candidate.counters_16",
    }
)

_IGNORED_TAGS = {
    "defs",
    "style",
    "text",
    "tspan",
    "textPath",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "mask",
    "filter",
    "title",
    "desc",
    "metadata",
}

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_STROKE_WIDTH = re.compile(r'stroke-width="([0-9.]+)"')
_PAINT = re.compile(r'\b(fill|stroke)="([^"]*)"')
_NOT_A_COLOR = {"none", "transparent", "currentcolor", "inherit", "unset"}


class RefineSourceError(RuntimeError):
    """원본 마크를 읽거나 구조를 해석하지 못했을 때."""


# ---------------------------------------------------------------------------
# 원본 인장 해석
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceModel:
    """원본 SVG에서 읽은 **비율**만 담는다. 좌표는 512 아트보드로 다시 계산한다."""

    sha256: str
    view_box: Tuple[float, float]
    center: Tuple[float, float]
    outer_ratio: float  # 바깥 링 반지름 / (보드 한 변의 절반)
    ring_ratios: Tuple[float, ...]  # 1.0부터 안쪽 링까지
    ray_count: int
    ray_step: float  # 도(degree)
    ray_phase: float  # 첫 방사선 각도
    ray_length_ratio: float  # 방사선 길이 / 바깥 링 반지름
    seed_dot_ratio: float  # 씨앗 점 반지름 / 바깥 링 반지름
    core_ratio: float  # 중심 원 반지름 / 바깥 링 반지름
    has_gradient: bool
    text_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha256": self.sha256,
            "view_box": list(self.view_box),
            "center": list(self.center),
            "outer_ratio": round(self.outer_ratio, 4),
            "ring_ratios": [round(value, 4) for value in self.ring_ratios],
            "ray_count": self.ray_count,
            "ray_step": round(self.ray_step, 3),
            "ray_phase": round(self.ray_phase, 3),
            "ray_length_ratio": round(self.ray_length_ratio, 4),
            "seed_dot_ratio": round(self.seed_dot_ratio, 4),
            "core_ratio": round(self.core_ratio, 4),
            "has_gradient": self.has_gradient,
            "text_count": self.text_count,
        }

    @property
    def inner_ring_ratios(self) -> Tuple[float, ...]:
        return tuple(value for value in self.ring_ratios if value < 0.995)


def _walk(element: ET.Element) -> Iterable[Tuple[str, Mapping[str, str]]]:
    for child in element:
        tag = child.tag.split("}")[-1]
        if tag in _IGNORED_TAGS:
            continue
        yield tag, child.attrib
        yield from _walk(child)


def _num(value: Any, default: float = 0.0) -> float:
    match = _NUMBER.search(str(value or ""))
    return float(match.group(0)) if match else default


def _points(raw: str) -> List[Tuple[float, float]]:
    numbers = [float(value) for value in _NUMBER.findall(str(raw or ""))]
    return list(zip(numbers[0::2], numbers[1::2]))


def _view_box(root: ET.Element) -> Tuple[float, float]:
    raw = root.attrib.get("viewBox") or ""
    numbers = [float(value) for value in _NUMBER.findall(raw)]
    if len(numbers) >= 4 and numbers[2] > 0 and numbers[3] > 0:
        return numbers[2], numbers[3]
    width = _num(root.attrib.get("width"), 0.0)
    height = _num(root.attrib.get("height"), 0.0)
    if width > 0 and height > 0:
        return width, height
    return 400.0, 400.0


def _median(values: Sequence[float], default: float = 0.0) -> float:
    rows = sorted(float(value) for value in values)
    if not rows:
        return default
    middle = len(rows) // 2
    if len(rows) % 2:
        return rows[middle]
    return (rows[middle - 1] + rows[middle]) / 2.0


def analyze_source(svg_text: str, *, sha256: Optional[str] = None) -> SourceModel:
    """원본 SVG → 구조 비율. 텍스트·그라디언트·defs는 세지 않는다.

    인장이 아닌 SVG(선·면만 있는 로고)도 읽을 수 있다. 링·방사선이 없으면
    안전한 기본 비율로 폴백하고, 그 사실은 후보 게이트가 아니라 계획 meta에
    `source` 모델로 남는다.
    """
    text = str(svg_text or "")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RefineSourceError(
            "refine: 원본 마크를 XML로 읽지 못했다 — SVG 형식을 확인하라: "
            f"{str(exc)[:200]}"
        ) from exc

    circles: List[Dict[str, Any]] = []
    lines: List[Dict[str, Any]] = []
    for tag, attrs in _walk(root):
        if tag == "circle":
            circles.append(
                {
                    "cx": _num(attrs.get("cx")),
                    "cy": _num(attrs.get("cy")),
                    "r": _num(attrs.get("r")),
                    "stroke": str(attrs.get("stroke") or ""),
                    "stroke_width": _num(attrs.get("stroke-width"), 0.0),
                    "fill": str(attrs.get("fill") or ""),
                }
            )
        elif tag == "line":
            lines.append(
                {
                    "x1": _num(attrs.get("x1")),
                    "y1": _num(attrs.get("y1")),
                    "x2": _num(attrs.get("x2")),
                    "y2": _num(attrs.get("y2")),
                }
            )

    width, height = _view_box(root)
    board = max(width, height) or 400.0
    biggest = max(circles, key=lambda row: row["r"]) if circles else None
    center = (biggest["cx"], biggest["cy"]) if biggest else (width / 2.0, height / 2.0)
    cx, cy = center

    stroked = [
        row
        for row in circles
        if row["r"] > 0 and (row["stroke"] or row["stroke_width"] > 0)
    ]
    outer = max((row["r"] for row in stroked), default=board * 0.47)
    if outer <= 0:
        outer = board * 0.47

    ring_radii = sorted(
        {round(row["r"], 3) for row in stroked if row["r"] >= outer * 0.3},
        reverse=True,
    )
    if not ring_radii:
        ring_radii = [outer]
    ring_ratios = tuple(value / outer for value in ring_radii)

    near_center = max(1.5, outer * 0.03)
    core_radii = [
        row["r"]
        for row in circles
        if row["r"] > 0
        and row["r"] < outer * 0.3
        and math.hypot(row["cx"] - cx, row["cy"] - cy) <= near_center
    ]
    core_ratio = (max(core_radii) / outer) if core_radii else 0.075

    dot_radii = [
        row["r"]
        for row in circles
        if 0 < row["r"] < outer * 0.03
        and math.hypot(row["cx"] - cx, row["cy"] - cy) > near_center
    ]
    seed_dot_ratio = (_median(dot_radii, outer * 0.013) / outer) if outer else 0.013

    angles: List[float] = []
    lengths: List[float] = []
    candidates: List[Tuple[float, float]] = []
    for row in lines:
        first = math.hypot(row["x1"] - cx, row["y1"] - cy)
        second = math.hypot(row["x2"] - cx, row["y2"] - cy)
        if min(first, second) > near_center:
            continue
        far_x = row["x2"] if first <= second else row["x1"]
        far_y = row["y2"] if first <= second else row["y1"]
        length = math.hypot(far_x - cx, far_y - cy)
        if length < outer * 0.1:
            continue
        angle = math.degrees(math.atan2(far_y - cy, far_x - cx)) % 360.0
        candidates.append((angle, length))

    # 같은 길이의 선분 무리가 방사선이다. 유리 돔 대각선처럼 길이가 다른 선은
    # 버린다(가장 많은 길이 군집을 고른다).
    if candidates:
        buckets: Dict[float, List[Tuple[float, float]]] = {}
        for angle, length in candidates:
            key = round(length / outer, 2)
            buckets.setdefault(key, []).append((angle, length))
        dominant = max(buckets.values(), key=lambda rows: (len(rows), rows[0][1]))
        angles = [angle for angle, _ in dominant]
        lengths = [length for _, length in dominant]

    angles.sort()
    ray_count = len(angles)
    ray_step = 360.0 / ray_count if ray_count else 360.0 / 12
    if len(angles) >= 2:
        gaps = [
            (angles[index + 1] - angles[index]) % 360.0
            for index in range(len(angles) - 1)
        ]
        ray_step = _median(gaps, ray_step)
    ray_phase = angles[0] if angles else 270.0
    ray_length_ratio = (_median(lengths, outer * 0.36) / outer) if outer else 0.36

    gradient_count = sum(
        1
        for element in root.iter()
        if element.tag.split("}")[-1] in ("linearGradient", "radialGradient")
    )
    text_count = sum(
        1 for element in root.iter() if element.tag.split("}")[-1] == "text"
    )

    return SourceModel(
        sha256=sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest(),
        view_box=(width, height),
        center=(cx, cy),
        outer_ratio=(outer / (board / 2.0)) if board else 0.94,
        ring_ratios=ring_ratios,
        ray_count=ray_count or 12,
        ray_step=ray_step,
        ray_phase=ray_phase,
        ray_length_ratio=ray_length_ratio,
        seed_dot_ratio=max(0.008, min(0.03, seed_dot_ratio)),
        core_ratio=max(0.04, min(0.14, core_ratio)),
        has_gradient=gradient_count > 0,
        text_count=text_count,
    )


# ---------------------------------------------------------------------------
# 후보 계획 — 원본 구조 + 브리프 시드
# ---------------------------------------------------------------------------


def _n(value: float) -> str:
    return f"{round(float(value), 3):g}"


def _weights(unit: float) -> Tuple[float, float, float]:
    return tuple(round(unit * ratio, 1) for ratio in OPTICAL_SCALE)  # type: ignore[return-value]


def _seed_dot_radius(model: SourceModel, rng: brand_marks.Rng) -> float:
    return round(OUTER_RADIUS * model.seed_dot_ratio * rng.between(0.85, 1.15), 2)


def _ray_angles(model: SourceModel, count: int, phase_shift: float) -> List[float]:
    """원본 방사선의 위상·간격을 물려받아 `count`개 각도를 만든다."""
    return [
        (model.ray_phase + phase_shift + index * (360.0 / count)) % 360.0
        for index in range(count)
    ]


def _ring_plan(
    model: SourceModel, count: int, unit: float, overshoot: float
) -> Tuple[List[float], List[float]]:
    """링 반지름(오버슈트 포함)과 획 굵기. 굵기는 1.0/1.5/2.0 배율만 쓴다.

    원본 링 비율을 그대로 물려받되, 두 링의 간격이 획 굵기보다 좁으면 한 덩어리로
    보이므로 아래에서 픽셀 간격(획 절반 + 호흡)만큼 안쪽으로 당긴다.
    """
    ratios = [1.0]
    for value in model.inner_ring_ratios:
        if len(ratios) >= count:
            break
        if value < ratios[-1] - 0.02:
            ratios.append(value)
    for fallback in (0.88, 0.72, 0.58, 0.48):
        if len(ratios) >= count:
            break
        if ratios[-1] - fallback >= 0.10:
            ratios.append(fallback)
    radii = [round(OUTER_RADIUS * ratios[0] + overshoot, 3)]
    base, hair, bold = _weights(unit)
    pattern = (bold, base, hair, base)
    weights = [pattern[index % len(pattern)] for index in range(len(ratios))]
    # 원본의 이중 링(0.936)은 획 20px와 10px가 만나면 한 덩어리로 보인다.
    # 두 링 사이에 획 절반 + 호흡(0.45u)을 최소 간격으로 두고 안쪽으로 당긴다.
    for index in range(1, len(ratios)):
        target = OUTER_RADIUS * ratios[index] + overshoot
        gap = (weights[index - 1] + weights[index]) / 2.0 + max(4.0, unit * 1.2)
        radii.append(round(min(target, radii[-1] - gap), 3))
    return radii, weights


def _glyph_for(name: str, brand_name: str = "") -> str:
    """브랜드 이름에서 쓸 글리프 하나. 없으면 M으로 폴백한다(결정론)."""
    haystack = f"{name}{brand_name}"
    for char in haystack.upper():
        if char in brand_marks.GLYPHS:
            return char
    for char in haystack.upper():
        if "A" <= char <= "Z":
            return char if char in brand_marks.GLYPHS else "M"
    return "M"


def _monogram_box(
    glyph: str, box: float, weight: float
) -> Tuple[List[Tuple[float, float, float, float]], List[float]]:
    """글리프 선분을 `box` 크기로 키우고 획 반폭까지 포함한 bbox를 돌려준다."""
    segments = list(brand_marks.GLYPHS.get(glyph) or brand_marks.GLYPHS["M"])
    start_x = CENTER - box / 2.0
    start_y = CENTER - box / 2.0
    scaled = [
        (
            start_x + x1 * box,
            start_y + y1 * box,
            start_x + x2 * box,
            start_y + y2 * box,
        )
        for x1, y1, x2, y2 in segments
    ]
    xs = [value for row in scaled for value in (row[0], row[2])]
    ys = [value for row in scaled for value in (row[1], row[3])]
    half = weight / 2.0
    return scaled, [min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half]


def _circle(
    radius: float,
    *,
    cx: float = CENTER,
    cy: float = CENTER,
    fill: str = "none",
    stroke: Optional[str] = None,
    width: Optional[float] = None,
) -> Dict[str, Any]:
    shape: Dict[str, Any] = {
        "kind": "circle",
        "cx": round(cx, 3),
        "cy": round(cy, 3),
        "r": round(radius, 3),
        "fill": fill,
    }
    if stroke:
        shape["stroke"] = stroke
        shape["stroke_width"] = round(float(width or 0.0), 1)
    return shape


def _line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    stroke: str,
    width: float,
) -> Dict[str, Any]:
    return {
        "kind": "line",
        "x1": round(x1, 3),
        "y1": round(y1, 3),
        "x2": round(x2, 3),
        "y2": round(y2, 3),
        "stroke": stroke,
        "stroke_width": round(width, 1),
    }


def _path(d: str, *, fill: str, fill_rule: Optional[str] = None) -> Dict[str, Any]:
    shape: Dict[str, Any] = {"kind": "path", "d": d, "fill": fill}
    if fill_rule:
        shape["fill_rule"] = fill_rule
    return shape


def _frame(
    model: SourceModel, params: Dict[str, Any], *, minimum: int = 2
) -> List[Dict[str, Any]]:
    """모든 가족이 공유하는 인장 골격 — 원본 링 비율의 바깥 이중 링 + 안쪽 링.

    세 가족 모두 이 골격을 **먼저** 그린다. 한눈에 같은 인장 가족으로 읽히게
    하는 최소 조건이고, 테스트도 후보마다 이 링을 확인한다.
    """
    unit = params["stroke_unit"]
    count = max(int(minimum), int(params["ring_count"]))
    rings, weights = _ring_plan(model, count, unit, params["overshoot_px"])
    params["ring_count"] = count
    params["ring_radii"] = rings
    params["ring_weights"] = weights
    return [
        _circle(radius, stroke="ink", width=weight)
        for radius, weight in zip(rings, weights)
    ]


def _inner_field(params: Mapping[str, Any]) -> float:
    """가장 안쪽 링 안에서 실제로 그림을 그릴 수 있는 반지름(획 절반 + 호흡 제외)."""
    radii = list(params.get("ring_radii") or [])
    weights = list(params.get("ring_weights") or [])
    if not radii:
        return OUTER_RADIUS * 0.60
    half = (weights[-1] / 2.0) if weights else 0.0
    return max(24.0, float(radii[-1]) - half - float(params["stroke_unit"]) * 1.5)


# ---------------------------------------------------------------------------
# 음각(negative-space) — 가족 위에 얹는 수정자
# ---------------------------------------------------------------------------


def _polygon_area(points: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _oriented(
    points: Sequence[Tuple[float, float]], *, clockwise: bool
) -> List[Tuple[float, float]]:
    rows = [(float(x), float(y)) for x, y in points]
    return rows if (_polygon_area(rows) > 0) == clockwise else list(reversed(rows))


def _polygon_d(points: Sequence[Tuple[float, float]], *, clockwise: bool) -> str:
    rows = _oriented(points, clockwise=clockwise)
    head = f"M {_n(rows[0][0])} {_n(rows[0][1])}"
    body = "".join(f" L {_n(x)} {_n(y)}" for x, y in rows[1:])
    return f"{head}{body} Z"


def _disc_points(radius: float, steps: int = 72) -> List[Tuple[float, float]]:
    return [
        (
            CENTER + radius * math.cos(2.0 * math.pi * index / steps),
            CENTER + radius * math.sin(2.0 * math.pi * index / steps),
        )
        for index in range(steps)
    ]


def _cubic_points(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    p3: Tuple[float, float],
    steps: int = 18,
) -> List[Tuple[float, float]]:
    rows: List[Tuple[float, float]] = []
    for index in range(steps + 1):
        t = index / steps
        u = 1.0 - t
        rows.append(
            (
                u**3 * p0[0]
                + 3 * u * u * t * p1[0]
                + 3 * u * t * t * p2[0]
                + t**3 * p3[0],
                u**3 * p0[1]
                + 3 * u * u * t * p1[1]
                + 3 * u * t * t * p2[1]
                + t**3 * p3[1],
            )
        )
    return rows


def _seed_outline(height: float, width: float) -> List[Tuple[float, float]]:
    """씨앗(티어드롭) 외곽선. 위는 뾰족하고 아래는 둥글다."""
    tip = (CENTER, CENTER - height * 0.62)
    bottom = (CENTER, CENTER + height * 0.38)
    right_controls = (
        (CENTER + width * 0.55, CENTER - height * 0.30),
        (CENTER + width * 0.55, CENTER + height * 0.20),
    )
    left_controls = (
        (CENTER - width * 0.55, CENTER + height * 0.20),
        (CENTER - width * 0.55, CENTER - height * 0.30),
    )
    right = _cubic_points(tip, *right_controls, bottom)
    left = _cubic_points(bottom, *left_controls, tip)
    return right + left[1:-1]


def _ray_hole(
    angle: float, start_radius: float, end_radius: float, width: float
) -> List[Tuple[float, float]]:
    radians = math.radians(angle)
    start = (
        CENTER + start_radius * math.cos(radians),
        CENTER + start_radius * math.sin(radians),
    )
    end = (
        CENTER + end_radius * math.cos(radians),
        CENTER + end_radius * math.sin(radians),
    )
    return brand_marks._segment_quad(start, end, width)


def _cross(
    a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]
) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _convex_intersection(
    subject: Sequence[Tuple[float, float]], clip: Sequence[Tuple[float, float]]
) -> List[Tuple[float, float]]:
    """두 볼록 다각형의 교집합(Sutherland–Hodgman). 획이 만나는 조인트 보정용."""
    if len(subject) < 3 or len(clip) < 3:
        return []
    clip_rows = _oriented(clip, clockwise=True)
    sign = 1.0 if _polygon_area(clip_rows) > 0 else -1.0
    output = [(float(x), float(y)) for x, y in subject]
    for index, start in enumerate(clip_rows):
        end = clip_rows[(index + 1) % len(clip_rows)]
        if not output:
            break
        source = output
        output = []
        for position, current in enumerate(source):
            previous = source[position - 1]
            current_in = sign * _cross(start, end, current) >= -1e-9
            previous_in = sign * _cross(start, end, previous) >= -1e-9
            if current_in:
                if not previous_in:
                    output.append(_line_intersection(previous, current, start, end))
                output.append(current)
            elif previous_in:
                output.append(_line_intersection(previous, current, start, end))
    return [(round(x, 3), round(y, 3)) for x, y in output if len(output) >= 3]


def _line_intersection(
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    p3: Tuple[float, float],
    p4: Tuple[float, float],
) -> Tuple[float, float]:
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    denominator = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denominator) < 1e-12:
        return p2
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denominator
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def _overlap_fillers(
    quads: Sequence[Sequence[Tuple[float, float]]]
) -> List[List[Tuple[float, float]]]:
    """겹치는 구멍끼리의 교집합을 +1 도형으로 돌려준다.

    nonzero 채우기에서 구멍 둘이 겹치면 권선이 -2가 되어 다시 채워진다(조인트의
    검은 마름모). 교집합을 원판과 같은 방향(+1)으로 더하면 -2+1=-1이 되어
    진짜 구멍이 유지된다. 글자 조인트를 깨끗하게 만드는 보정이다.
    """
    rows: List[List[Tuple[float, float]]] = []
    for first in range(len(quads)):
        for second in range(first + 1, len(quads)):
            overlap = _convex_intersection(quads[first], quads[second])
            if len(overlap) >= 3 and abs(_polygon_area(overlap)) > 0.5:
                rows.append(overlap)
    return rows


def _carved(
    radius: float,
    holes: Sequence[Sequence[Tuple[float, float]]],
    fillers: Sequence[Sequence[Tuple[float, float]]] = (),
) -> Dict[str, Any]:
    """원판을 칠하고 구멍들을 반대 방향으로 빼서 진짜 음각을 만든다(nonzero).

    evenodd는 구멍끼리 겹치면 다시 채워지고, nonzero는 같은 방향 구멍이
    합집합으로 비워진다. 그래서 원판은 시계, 구멍은 전부 반시계로 맞춘다.
    `fillers`는 겹친 구멍의 교집합 보정으로, 원판과 같은 방향이다.
    """
    parts = [_polygon_d(_disc_points(radius), clockwise=True)]
    parts += [_polygon_d(hole, clockwise=False) for hole in holes if len(hole) >= 3]
    parts += [_polygon_d(filler, clockwise=True) for filler in fillers if len(filler) >= 3]
    return _path(" ".join(parts), fill="ink", fill_rule="nonzero")


def _cut_meta(params: Dict[str, Any], **values: Any) -> None:
    meta = {"modifier": "negative-cut", **values}
    params["cut"] = meta


def _modified_cut(
    family: str, model: SourceModel, params: Dict[str, Any], rng: brand_marks.Rng
) -> List[Dict[str, Any]]:
    """가족 1~3의 중심부를 원판 + 음각으로 바꾼다. 바깥 인장 링은 그대로 둔다."""
    unit = params["stroke_unit"]
    field = _inner_field(params)
    radius = field * 0.94
    holes: List[List[Tuple[float, float]]] = []
    shapes: List[Dict[str, Any]] = []

    if family == "seal-refined":
        core = OUTER_RADIUS * model.core_ratio
        reach = min(
            field * 0.62,
            OUTER_RADIUS * model.ray_length_ratio * params["reach_factor"],
        )
        holes.append(_disc_points(core * 0.45))
        for angle in _ray_angles(model, params["seed_count"], params["phase_shift"]):
            holes.append(_ray_hole(angle, core * 0.62, reach, unit))
        for angle in _ray_angles(model, params["seed_count"], params["phase_shift"]):
            radians = math.radians(angle)
            shapes.append(
                _circle(
                    params["seed_dot"],
                    cx=CENTER + reach * math.cos(radians),
                    cy=CENTER + reach * math.sin(radians),
                    fill="accent",
                )
            )
        _cut_meta(
            params,
            shape="sunburst",
            radius=round(radius, 3),
            holes=len(holes),
            reach=round(reach, 3),
        )
    elif family == "seal-monogram":
        mono = params.get("monogram") or {}
        quads = [
            brand_marks._segment_quad(
                (x1, y1), (x2, y2), float(mono.get("weight") or unit)
            )
            for x1, y1, x2, y2 in mono.get("segments") or []
        ]
        holes.extend(quads)
        shapes.append(
            _circle(
                unit * 0.9,
                cx=CENTER,
                cy=CENTER + field * 0.80,
                fill="accent",
            )
        )
        _cut_meta(
            params,
            shape="monogram",
            radius=round(radius, 3),
            holes=len(holes),
            glyph=(params.get("monogram") or {}).get("glyph"),
        )
    else:
        height = min(
            field * 1.02,
            OUTER_RADIUS * params["cut_depth"] * 2.0,
        )
        width = height * 0.42
        holes.append(_seed_outline(height, width))
        start = abs(CENTER - (CENTER - height * 0.62)) + unit * 1.6
        reach = radius - unit * 1.5
        for angle in _ray_angles(model, params["seed_count"], params["phase_shift"]):
            holes.append(_ray_hole(angle, start, reach, unit * 1.5))
        shapes.append(
            _circle(unit * 0.9, cx=CENTER, cy=CENTER + height * 0.58, fill="accent")
        )
        _cut_meta(
            params,
            shape="seed-rays",
            radius=round(radius, 3),
            holes=len(holes),
            height=round(height, 3),
        )

    fillers = _overlap_fillers(holes) if family == "seal-monogram" else []
    shapes.insert(0, _carved(radius, holes, fillers))
    return shapes


def _family_seal(
    model: SourceModel, rng: brand_marks.Rng, params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """seal-refined — 원본 인장에서 가장 가까운 가족. 골격은 그대로, 디테일만 덜어낸다."""
    unit = params["stroke_unit"]
    shapes = _frame(model, params, minimum=2)

    if params["modifier"] == "negative-cut":
        shapes += _modified_cut("seal-refined", model, params, rng)
        return shapes

    seed_count = params["seed_count"]
    reach = OUTER_RADIUS * model.ray_length_ratio * params["reach_factor"]
    core = OUTER_RADIUS * model.core_ratio
    dot = params["seed_dot"]
    for angle in _ray_angles(model, seed_count, params["phase_shift"]):
        radians = math.radians(angle)
        tip_x = CENTER + reach * math.cos(radians)
        tip_y = CENTER + reach * math.sin(radians)
        start_x = CENTER + core * 0.55 * math.cos(radians)
        start_y = CENTER + core * 0.55 * math.sin(radians)
        shapes.append(_line(start_x, start_y, tip_x, tip_y, stroke="ink", width=unit))
        shapes.append(_circle(dot, cx=tip_x, cy=tip_y, fill="accent"))

    # 코어 — 원본의 황금 심장을 두 겹 원으로 단순화한다.
    shapes.append(_circle(core, stroke="ink", width=params["weights"][1]))
    shapes.append(_circle(core * 0.52, fill="accent"))
    params["reach"] = round(reach, 3)
    params["core_radius"] = round(core, 3)
    return shapes


def _family_monogram(
    model: SourceModel, rng: brand_marks.Rng, params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """seal-monogram — 인장 링은 그대로 두고 이름 머리글자만 중앙에 앉힌다."""
    unit = params["stroke_unit"]
    shapes = _frame(model, params, minimum=3)

    glyph = params["glyph"]
    weight = params["glyph_weight"]
    inner = _inner_field(params)
    box = 2.0 * inner * params["glyph_fill"]
    segments, bounds = _monogram_box(glyph, box, weight)
    dx = CENTER - (bounds[0] + bounds[2]) / 2.0
    dy = CENTER - (bounds[1] + bounds[3]) / 2.0
    centered = [(x1 + dx, y1 + dy, x2 + dx, y2 + dy) for x1, y1, x2, y2 in segments]
    params["monogram"] = {
        "glyph": glyph,
        "box": round(box, 3),
        "weight": weight,
        "fill_ratio": params["glyph_fill"],
        "center_offset": [round(dx, 3), round(dy, 3)],
        "bbox_center": [CENTER, CENTER],
        "segments": centered,
    }

    if params["modifier"] == "negative-cut":
        shapes += _modified_cut("seal-monogram", model, params, rng)
        return shapes

    for x1, y1, x2, y2 in centered:
        shapes.append(_line(x1, y1, x2, y2, stroke="ink", width=weight))
    # 인장다운 악센트 — 아래쪽 링 안에 작은 점 하나.
    shapes.append(
        _circle(unit * 0.75, cx=CENTER, cy=CENTER + inner * 0.82, fill="accent")
    )
    return shapes


def _family_dandelion(
    model: SourceModel, rng: brand_marks.Rng, params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """seal-dandelion — 인장 링 안에서 원본 방사 각도·길이를 씨앗으로 추상화한다."""
    unit = params["stroke_unit"]
    shapes = _frame(model, params, minimum=3)
    field = _inner_field(params)

    if params["modifier"] == "negative-cut":
        shapes += _modified_cut("seal-dandelion", model, params, rng)
        return shapes

    reach = field * 0.72
    halo = reach * 0.45 + params["overshoot_px"]
    shapes.append(_circle(halo, stroke="ink", width=unit))
    params["ring_radii"].append(round(halo, 3))
    params["ring_weights"].append(unit)
    dot = params["seed_dot"]
    bend = rng.between(0.06, 0.11) * reach
    angles = _ray_angles(model, params["seed_count"], params["phase_shift"])
    for index, angle in enumerate(angles):
        radians = math.radians(angle)
        # 씨앗마다 길이를 조금씩 달리해 바퀴 같은 등속 인상을 없앤다.
        stem = reach * (0.96 + 0.04 * (index % 3))
        tip_x = CENTER + stem * math.cos(radians)
        tip_y = CENTER + stem * math.sin(radians)
        control_x = (
            CENTER
            + stem * 0.58 * math.cos(radians)
            - bend * math.sin(radians) * (1 if index % 2 else -1)
        )
        control_y = (
            CENTER
            + stem * 0.58 * math.sin(radians)
            + bend * math.cos(radians) * (1 if index % 2 else -1)
        )
        shapes.append(
            _path(
                f"M {_n(CENTER)} {_n(CENTER)} "
                f"Q {_n(control_x)} {_n(control_y)} {_n(tip_x)} {_n(tip_y)}",
                fill="none",
            )
        )
        shapes[-1].update({"stroke": "ink", "stroke_width": round(unit * 2.0, 1)})
        # 씨앗 깃털 — 줄기 끝에서 세 갈래로 부챗살처럼 벌어진다.
        feather = stem * 0.26
        for offset in (-1, 0, 1):
            feather_angle = math.radians(angle + offset * params["feather_spread"])
            shapes.append(
                _line(
                    tip_x,
                    tip_y,
                    tip_x + feather * math.cos(feather_angle),
                    tip_y + feather * math.sin(feather_angle),
                    stroke="ink",
                    width=unit,
                )
            )
        shapes.append(_circle(dot, cx=tip_x, cy=tip_y, fill="accent"))

    core = max(7.0, OUTER_RADIUS * model.core_ratio * 1.4)
    shapes.append(_circle(core, fill="accent"))
    params["core_radius"] = round(core, 3)
    params["reach"] = round(reach, 3)
    return shapes


_FAMILY_BUILDERS = {
    "seal-refined": _family_seal,
    "seal-monogram": _family_monogram,
    "seal-dandelion": _family_dandelion,
}


def _note(family: str, params: Mapping[str, Any], model: SourceModel) -> str:
    """후보 한 줄 한국어 메모 — 가족·수정자 + 원본에서 바뀐 점. 마케팅 문구는 없다."""
    unit = params["stroke_unit"]
    cut = params.get("modifier") == "negative-cut"
    tail = " 중심부는 음각." if cut else ""
    if family == "seal-refined":
        return (
            f"seal-refined · 고리 {params['ring_count']}겹·방사 "
            f"{params['seed_count']}줄. 유리 돔·각인을 덜고 획 {unit:g}로 다시 세웠다."
            f"{tail}"
        )
    if family == "seal-monogram":
        mono = params.get("monogram") or {}
        return (
            f"seal-monogram · {mono.get('glyph', '?')}자·획 "
            f"{mono.get('weight', 0):g}. 인장 링 안쪽에 글자를 광학 정렬했다.{tail}"
        )
    return (
        f"seal-dandelion · 씨앗 {params['seed_count']}개. 원본 "
        f"{round(model.ray_step, 1):g}° 방사 간격을 링 안의 씨앗으로 추상화했다.{tail}"
    )


def plan_candidates(
    model: SourceModel,
    brief: str,
    *,
    name: str = "",
    families: Optional[Sequence[str]] = None,
    count: int = DEFAULT_CANDIDATES,
    seed_hint: str = "",
) -> List[Dict[str, Any]]:
    """원본 구조 + 브리프 → 후보 계획. 같은 입력이면 언제나 같은 목록."""
    total = int(count)
    if total < MIN_CANDIDATES or total > MAX_CANDIDATES:
        raise ValueError(
            f"brand refine: candidates는 {MIN_CANDIDATES}~{MAX_CANDIDATES}개여야 한다 — "
            f"{count!r}"
        )
    chosen = tuple(families or FAMILIES)
    unknown = sorted(set(chosen) - set(FAMILIES))
    if unknown or not chosen:
        raise ValueError(
            f"brand refine: families는 {', '.join(FAMILIES)} 중에서 골라라 — "
            f"{list(families or [])!r}"
        )

    brief_text = str(brief or "")
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    occurrences: Dict[str, int] = {}
    for index in range(total):
        family = chosen[index % len(chosen)]
        # 음각은 가족 안에서 두 번째 변주부터 켠다 — 8개면 가족마다 최소 1개,
        # 12개면 가족마다 2개가 음각이 된다. 가족 정체성은 바뀌지 않는다.
        occurrence = occurrences.get(family, 0)
        occurrences[family] = occurrence + 1
        modifier = "negative-cut" if occurrence % 2 == 1 else "none"
        seed = brand_marks.seed_for(brief_text, index, seed_hint=seed_hint)
        rng = brand_marks.Rng(seed)
        unit = rng.pick((8.0, 10.0, 12.0))
        overshoot = round(OUTER_RADIUS * rng.between(*OVERSHOOT_RANGE), 2)
        params: Dict[str, Any] = {
            "family": family,
            "index": index,
            "stroke_unit": unit,
            "weights": _weights(unit),
            "overshoot_px": overshoot,
            "overshoot_ratio": round(overshoot / OUTER_RADIUS, 4),
            "ring_count": rng.pick((2, 2, 3)),
            "seed_count": rng.pick((8, 9, 10, 11, 12)),
            "seed_dot": _seed_dot_radius(model, rng),
            "phase_shift": round(rng.between(0.0, max(1.0, model.ray_step / 2.0)), 3),
            "reach_factor": round(rng.between(0.92, 1.12), 3),
            "feather_spread": rng.pick((24.0, 28.0, 32.0)),
            "cut_depth": round(rng.between(0.20, 0.28), 3),
            "glyph": _glyph_for(name, name),
            "glyph_fill": round(rng.between(0.52, 0.62), 3),
            "glyph_weight": round(unit * 2.0, 1),
            "modifier": modifier,
        }
        shapes = _FAMILY_BUILDERS[family](model, rng, params)
        params["shapes"] = shapes
        token = f"{seed & 0xFFFFFFFFFFFF:012x}"
        suffix = "-cut" if modifier == "negative-cut" else ""
        candidate_id = f"refine-{family}{suffix}-{token}"
        if candidate_id in seen:
            candidate_id = f"{candidate_id}-{index}"
        seen.add(candidate_id)
        rows.append(
            {
                "id": candidate_id,
                "family": family,
                "index": index,
                "seed": token,
                "seed64": seed,
                "params": params,
                "note": _note(family, params, model),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# SVG 생성 (순수 함수)
# ---------------------------------------------------------------------------


def _color(role: Optional[str], palette: Mapping[str, str]) -> Optional[str]:
    if not role or role == "none":
        return None
    value = palette.get(role) or palette.get("ink")
    return brand_marks.hex6(str(value)) if value else None


def _shape_svg(shape: Mapping[str, Any], palette: Mapping[str, str]) -> str:
    kind = str(shape["kind"])
    if kind == "circle":
        fill = _color(shape.get("fill"), palette) or "none"
        stroke = _color(shape.get("stroke"), palette)
        parts = [
            f'cx="{_n(float(shape["cx"]))}"',
            f'cy="{_n(float(shape["cy"]))}"',
            f'r="{_n(float(shape["r"]))}"',
            f'fill="{fill}"',
        ]
        if stroke:
            parts += [
                f'stroke="{stroke}"',
                f'stroke-width="{_n(float(shape.get("stroke_width") or 0.0))}"',
            ]
        return f"<circle {' '.join(parts)}/>"
    if kind == "line":
        stroke = _color(shape.get("stroke"), palette) or "none"
        return (
            f'<line x1="{_n(float(shape["x1"]))}" y1="{_n(float(shape["y1"]))}" '
            f'x2="{_n(float(shape["x2"]))}" y2="{_n(float(shape["y2"]))}" '
            f'stroke="{stroke}" stroke-width="'
            f'{_n(float(shape.get("stroke_width") or 0.0))}" '
            f'stroke-linecap="round"/>'
        )
    if kind == "path":
        fill = _color(shape.get("fill"), palette) or "none"
        stroke = _color(shape.get("stroke"), palette)
        parts = [f'd="{shape["d"]}"', f'fill="{fill}"']
        if shape.get("fill_rule"):
            parts.append(f'fill-rule="{shape["fill_rule"]}"')
        if stroke:
            parts += [
                f'stroke="{stroke}"',
                f'stroke-width="{_n(float(shape.get("stroke_width") or 0.0))}"',
                'stroke-linecap="round"',
                'stroke-linejoin="round"',
            ]
        return f"<path {' '.join(parts)}/>"
    raise RefineSourceError(f"brand refine: 알 수 없는 도형 kind={kind!r}")


def candidate_svg(candidate: Mapping[str, Any], palette: Mapping[str, str]) -> str:
    """후보 → SVG 문자열. 텍스트·스타일·그라디언트가 없다(결정론)."""
    params = candidate.get("params") or {}
    shapes = params.get("shapes") or []
    body = "\n  ".join(_shape_svg(shape, palette) for shape in shapes)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{ARTBOARD}" '
        f'height="{ARTBOARD}" viewBox="0 0 {ARTBOARD} {ARTBOARD}" fill="none">\n'
        f"  {body}\n</svg>\n"
    )


def lockup_svg(
    candidate: Mapping[str, Any],
    name: str,
    palette: Mapping[str, str],
    *,
    width: int = 1600,
    height: int = 600,
) -> str:
    """마크 + 워드마크 가로 락업 SVG. 워드마크는 `convert --outline-text`로 패스화한다."""
    font, style = (
        (brand_marks.LOCKUP_FALLBACK_FONT, brand_marks.LOCKUP_FALLBACK_STYLE)
        if any(ord(char) > 127 for char in str(name))
        else (brand_marks.LOCKUP_FONT, brand_marks.LOCKUP_STYLE)
    )
    mark = candidate_svg(candidate, palette)
    inner = mark.split(">", 1)[1].rsplit("</svg>", 1)[0]
    ink = brand_marks.hex6(palette.get("ink", "#161616"))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" fill="none">\n'
        f'  <g transform="translate(80 84) scale(0.72)">{inner}</g>\n'
        f'  <text x="520" y="320" font-family="{font}" font-size="132" '
        f'font-weight="700" fill="{ink}">{_xml_text(name)}</text>\n'
        "</svg>\n"
    )


def _xml_text(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_candidate(
    runner: Any,
    binary: str,
    candidate: Mapping[str, Any],
    out_dir: Path,
    palette: Mapping[str, str],
) -> Dict[str, Any]:
    """후보 SVG를 쓰고 512px PNG로 굽는다. 둘 중 하나라도 없으면 실패한다."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    svg = out / "candidate.svg"
    png = out / "candidate.png"
    svg.write_text(candidate_svg(candidate, palette), encoding="utf-8")
    if png.is_file():
        png.unlink()
    try:
        runner(
            [str(binary), "convert", str(svg), str(png), "--scale", "1.0"], timeout=180
        )
    except Exception as exc:  # noqa: BLE001
        raise brand_marks.MarkRenderError(
            "brand refine: 후보 SVG를 PNG로 굽지 못했다 — "
            f"vectorcraft-cli 설치를 확인하라: {str(exc).strip()[:300]}"
        ) from exc
    if not png.is_file() or png.stat().st_size <= 0:
        raise brand_marks.MarkRenderError(f"brand refine: 후보 PNG가 비었다 — {png}")
    return {
        "svg": str(svg),
        "png": str(png),
        "sha256": hashlib.sha256(svg.read_bytes()).hexdigest(),
    }


def render_lockup(
    runner: Any,
    binary: str,
    candidate: Mapping[str, Any],
    name: str,
    out_dir: Path,
    palette: Mapping[str, str],
    *,
    pdf: bool = True,
) -> Dict[str, Optional[str]]:
    """마크 + 워드마크 락업. 워드마크는 아웃라인해서 텍스트를 남기지 않는다."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "lockup-raw.svg"
    svg = out / "lockup.svg"
    raw.write_text(lockup_svg(candidate, name, palette), encoding="utf-8")
    try:
        runner(
            [str(binary), "convert", str(raw), str(svg), "--outline-text"], timeout=180
        )
    except Exception as exc:  # noqa: BLE001
        raise brand_marks.MarkRenderError(
            "brand refine: 락업 텍스트를 아웃라인하지 못했다 — "
            f"폰트({brand_marks.LOCKUP_FONT}) 설치를 확인하라: "
            f"{str(exc).strip()[:300]}"
        ) from exc
    if not svg.is_file() or svg.stat().st_size <= 0:
        raise brand_marks.MarkRenderError(f"brand refine: 락업 SVG가 비었다 — {svg}")
    pdf_path: Optional[str] = None
    if pdf:
        target = out / "lockup.pdf"
        runner([str(binary), "convert", str(svg), str(target)], timeout=180)
        if not target.is_file() or target.stat().st_size <= 0:
            raise brand_marks.MarkRenderError(
                f"brand refine: 락업 PDF가 비었다 — {target}"
            )
        pdf_path = str(target)
    return {"svg": str(svg), "pdf": pdf_path, "raw": str(raw)}


# ---------------------------------------------------------------------------
# 제작 규칙 게이트 (강제)
# ---------------------------------------------------------------------------


def _ok(gate_id: str, detail: str) -> Dict[str, Any]:
    return {"id": gate_id, "ok": True, "detail": detail, "skipped": False}


def _fail(gate_id: str, detail: str) -> Dict[str, Any]:
    return {"id": gate_id, "ok": False, "detail": detail, "skipped": False}


def stroke_widths(svg_text: str) -> List[float]:
    """SVG에 실제로 적힌 `stroke-width` 값(중복 제거, 오름차순)."""
    return sorted({float(match) for match in _STROKE_WIDTH.findall(str(svg_text))})


def paint_colors(svg_text: str) -> List[str]:
    """fill/stroke에 적힌 색값. none·url()은 뺀다."""
    colors = set()
    for _, value in _PAINT.findall(str(svg_text)):
        text = value.strip().lower()
        if not text or text in _NOT_A_COLOR or text.startswith("url("):
            continue
        colors.add(text)
    return sorted(colors)


def check_palette_roles(svg_text: str, palette: Mapping[str, str]) -> Dict[str, Any]:
    allowed = {
        brand_marks.hex6(palette[role]) for role in PALETTE_ROLES if role in palette
    }
    colors = paint_colors(svg_text)
    outside = sorted(set(colors) - allowed)
    if not colors:
        return _fail(
            "candidate.palette_roles", "색이 하나도 없다 — 도형에 색을 지정하라"
        )
    if outside:
        return _fail(
            "candidate.palette_roles",
            f"ink·accent·paper 밖 색 {len(outside)}개 — {', '.join(outside)}",
        )
    return _ok("candidate.palette_roles", f"색 {len(colors)}개 모두 ink·accent·paper")


def check_no_gradient(svg_text: str) -> Dict[str, Any]:
    text = str(svg_text)
    hits = sum(text.count(token) for token in ("Gradient", "url(#", "<filter", "<mask"))
    if hits:
        return _fail(
            "candidate.no_gradient",
            f"그라디언트/마스크 참조 {hits}개 — 정련 후보는 단색 도형만 쓴다",
        )
    return _ok("candidate.no_gradient", "그라디언트·마스크 0개")


def check_stroke_scale(params: Mapping[str, Any], svg_text: str) -> Dict[str, Any]:
    unit = float(params.get("stroke_unit") or 0.0)
    allowed = {round(unit * ratio, 1) for ratio in OPTICAL_SCALE}
    widths = stroke_widths(svg_text)
    if not widths:
        return _ok(
            "candidate.stroke_scale", "스트로크 없음(면 형태) — 배율 검사 해당 없음"
        )
    outside = [
        value
        for value in widths
        if not any(abs(value - candidate) <= 1e-6 for candidate in allowed)
    ]
    if outside:
        return _fail(
            "candidate.stroke_scale",
            "광학 배율 밖 획 "
            + ", ".join(f"{value:g}" for value in outside)
            + f" — 허용 {', '.join(f'{value:g}' for value in sorted(allowed))}",
        )
    return _ok(
        "candidate.stroke_scale",
        f"획 {len(widths)}종 모두 {unit:g} 기준 1.0/1.5/2.0 배율",
    )


def check_overshoot(params: Mapping[str, Any], svg_text: str) -> Dict[str, Any]:
    radius = OUTER_RADIUS
    overshoot = float(params.get("overshoot_px") or 0.0)
    low, high = OVERSHOOT_RANGE
    if not (low * radius <= overshoot <= high * radius):
        return _fail(
            "candidate.optical_overshoot",
            f"오버슈트 {overshoot:g}px가 허용 범위 "
            f"{low * radius:.2f}~{high * radius:.2f}px 밖이다",
        )
    rings = [float(value) for value in params.get("ring_radii") or []]
    if not params.get("ring_weights"):
        return _ok(
            "candidate.optical_overshoot",
            f"면 형태 — 스트로크 링 없음, 원판 반지름 +{overshoot:g}px",
        )
    stroked = [
        float(match)
        for match in re.findall(
            r'<circle[^>]*stroke="[^"]+"[^>]*r="([0-9.]+)"', str(svg_text)
        )
    ]
    stroked += [
        float(match)
        for match in re.findall(
            r'<circle[^>]*r="([0-9.]+)"[^>]*stroke="[^"]+"', str(svg_text)
        )
    ]
    stroked = sorted({round(value, 3) for value in stroked})
    if rings:
        missing = [
            value
            for value in rings
            if not any(abs(candidate - value) <= 0.02 for candidate in stroked)
        ]
        if missing:
            return _fail(
                "candidate.optical_overshoot",
                "오버슈트가 빠진 링 — " + ", ".join(f"{value:g}" for value in missing),
            )
    return _ok(
        "candidate.optical_overshoot",
        f"원형 반지름 +{overshoot:g}px(반지름의 {overshoot / radius:.2%})",
    )


#: 카운터가 있는 글리프와 「글리프 상자 대비 빈 공간 비율」. 획을 빼고 16px에서
#: 0.75px 이상 남아야 한다. (A의 삼각 카운터, D/O의 속공간)
GLYPH_COUNTERS: Dict[str, float] = {"A": 0.52, "D": 0.46, "O": 0.52, "P": 0.40}


def counter_gap_16(glyph: str, box: float, weight: float) -> Optional[float]:
    """글리프 카운터가 16px에서 남기는 간격(px). 카운터가 없으면 None."""
    fraction = GLYPH_COUNTERS.get(str(glyph).upper())
    if fraction is None:
        return None
    gap = box * fraction - weight
    return max(0.0, gap) * (16.0 / ARTBOARD)


def check_counters(candidate: Mapping[str, Any]) -> Dict[str, Any]:
    params = candidate.get("params") or {}
    mono = params.get("monogram") or {}
    if not mono:
        return _ok("candidate.counters_16", "카운터 없는 형태 — 해당 없음")
    gap = counter_gap_16(
        str(mono.get("glyph") or ""),
        float(mono.get("box") or 0.0),
        float(mono.get("weight") or 0.0),
    )
    if gap is None:
        return _ok("candidate.counters_16", f"{mono.get('glyph')} 카운터 없음")
    if gap < COUNTER_MIN_PX:
        return _fail(
            "candidate.counters_16",
            f"16px 카운터 {gap:.2f}px(< {COUNTER_MIN_PX}px) — 획을 줄이거나 글자를 키워라",
        )
    return _ok("candidate.counters_16", f"16px 카운터 {gap:.2f}px 유지")


def craft_gates(
    candidate: Mapping[str, Any],
    svg_text: str,
    palette: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """refine 후보의 제작 규칙 게이트 5종. 실패해도 후보를 지우지 않는다."""
    params = candidate.get("params") or {}
    return [
        check_palette_roles(svg_text, palette),
        check_no_gradient(svg_text),
        check_stroke_scale(params, svg_text),
        check_overshoot(params, svg_text),
        check_counters(candidate),
    ]


# ---------------------------------------------------------------------------
# 프레젠테이션 — 종이 512 · 16px · 다크 · 콘택트 시트 · 게이트 표
# ---------------------------------------------------------------------------


def presentation_images(
    candidate_png: Path,
    out_dir: Path,
    palette: Mapping[str, str],
    *,
    size: int = ARTBOARD,
) -> Dict[str, str]:
    """후보 PNG → 종이 배경 512 · 다크 512 · 실제 16px PNG(결정론)."""
    from PIL import Image

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paper = tuple(
        int(brand_marks.hex6(palette["paper"]).lstrip("#")[i : i + 2], 16)
        for i in (0, 2, 4)
    )
    ink = tuple(
        int(brand_marks.hex6(palette["ink"]).lstrip("#")[i : i + 2], 16)
        for i in (0, 2, 4)
    )

    with Image.open(candidate_png) as source:
        art = source.convert("RGBA")
        art = art.resize((size, size), Image.LANCZOS)

    light = Image.new("RGB", (size, size), paper)
    light.paste(art, (0, 0), art)
    light_path = out / "presentation-light.png"
    light.save(light_path, "PNG", optimize=True)

    # 다크: 잉크 픽셀만 종이색으로 바꾼 뒤 잉크 배경에 앉힌다(악센트는 유지).
    dark_art = art.copy()
    pixels = dark_art.load()
    for y in range(dark_art.height):
        for x in range(dark_art.width):
            red, green, blue, alpha = pixels[x, y]
            if alpha > 0 and (red, green, blue) == ink:
                pixels[x, y] = (*paper, alpha)
    dark = Image.new("RGB", (size, size), ink)
    dark.paste(dark_art, (0, 0), dark_art)
    dark_path = out / "presentation-dark.png"
    dark.save(dark_path, "PNG", optimize=True)

    small = art.resize((16, 16), Image.LANCZOS)
    small_path = out / "presentation-16.png"
    small.save(small_path, "PNG", optimize=True)
    return {
        "light": str(light_path),
        "dark": str(dark_path),
        "small": str(small_path),
    }


def _hex_rgb(value: str) -> Tuple[int, int, int]:
    digits = brand_marks.hex6(value).lstrip("#")
    return tuple(int(digits[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _load_font(size: int, *, bold: bool = False):
    from graphs.profiles import board as board_module

    return board_module._load_font(size, bold=bold)[0]


def _fit_image(canvas: Any, path: Path, box: Tuple[int, int, int, int]) -> bool:
    from PIL import Image

    left, top, right, bottom = box
    if not Path(path).is_file():
        return False
    with Image.open(path) as source:
        image = source.convert("RGBA")
        ratio = min(
            (right - left) / max(1, image.width),
            (bottom - top) / max(1, image.height),
        )
        size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        fitted = image.resize(size, Image.LANCZOS)
    x = left + ((right - left) - size[0]) // 2
    y = top + ((bottom - top) - size[1]) // 2
    canvas.paste(fitted, (x, y), fitted)
    return True


def _wrap(text: str, limit: int = 34) -> List[str]:
    words = str(text or "").split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines[:3]


def compose_candidate_sheet(
    rows: Sequence[Mapping[str, Any]],
    *,
    out: Path,
    palette: Mapping[str, str],
    brief: str = "",
    run_id: str = "",
    columns: int = 4,
) -> Dict[str, Any]:
    """후보들을 한 장에 모은다: 종이 512 격자 + 다크 줄 + 실제 16px 줄.

    `rows` 항목은 `id`·`family`·`note`·`light`·`dark`·`small` 경로를 가진다.
    바이트는 입력에만 의존한다(타임스탬프·난수 없음).
    """
    from PIL import Image, ImageDraw

    palette = dict(palette)
    paper = _hex_rgb(palette["paper"])
    ink = _hex_rgb(palette["ink"])
    muted = (110, 104, 96)
    rule = (214, 207, 199)
    columns = max(1, int(columns))
    count = len(rows)
    margin = 56
    gap = 28
    cell_w = 400
    cell_h = 520
    header = 190
    width = margin * 2 + columns * cell_w + max(0, columns - 1) * gap
    grid_rows = math.ceil(count / columns) if count else 0
    light_h = grid_rows * (cell_h + gap)
    strip_w = width - margin * 2
    strip_cell = (
        max(96.0, (strip_w - (count - 1) * 16) / max(1, count)) if count else 96.0
    )
    strip_label = 52
    dark_h = 220 + strip_label
    small_h = 190 + strip_label
    height = header + light_h + dark_h + small_h + margin

    canvas = Image.new("RGB", (width, height), paper)
    draw = ImageDraw.Draw(canvas)
    title_font = _load_font(44, bold=True)
    meta_font = _load_font(20)
    brief_font = _load_font(22)
    id_font = _load_font(19, bold=True)
    note_font = _load_font(16)
    small_font = _load_font(15)

    draw.text(
        (margin, margin - 8),
        "REFINE CONTACT SHEET",
        font=title_font,
        fill=_hex_rgb(palette["accent"]),
    )
    meta = " · ".join(
        part
        for part in (
            f"run {run_id}" if run_id else "",
            f"{count} candidates",
            "master seal refine",
        )
        if part
    )
    draw.text((margin, margin + 60), meta, font=meta_font, fill=muted)
    if brief:
        draw.text((margin, margin + 98), brief[:110], font=brief_font, fill=ink)
    draw.rectangle((margin, header - 26, width - margin, header - 25), fill=rule)

    top = header
    for index, row in enumerate(rows):
        grid_row, column = divmod(index, columns)
        cell_x = margin + column * (cell_w + gap)
        cell_y = top + grid_row * (cell_h + gap)
        draw.rectangle(
            (cell_x, cell_y, cell_x + cell_w - 1, cell_y + cell_h - 1),
            outline=rule,
            width=1,
        )
        _fit_image(
            canvas,
            Path(str(row.get("light") or "")),
            (cell_x + 14, cell_y + 14, cell_x + cell_w - 14, cell_y + 14 + 400),
        )
        draw.text(
            (cell_x + 16, cell_y + 424),
            str(row.get("id") or "—")[:44],
            font=id_font,
            fill=ink,
        )
        note_top = cell_y + 452
        for line in _wrap(str(row.get("note") or ""), 26):
            draw.text((cell_x + 16, note_top), line, font=note_font, fill=muted)
            note_top += 20

    dark_top = top + light_h + 16
    draw.rectangle((margin, dark_top, width - margin, dark_top + 1), fill=rule)
    draw.text((margin, dark_top + 14), "DARK · 512", font=meta_font, fill=muted)
    for index, row in enumerate(rows):
        cell_x = margin + index * (strip_cell + 16)
        _fit_image(
            canvas,
            Path(str(row.get("dark") or "")),
            (int(cell_x), dark_top + 46, int(cell_x + strip_cell), dark_top + 200),
        )

    small_top = dark_top + dark_h
    draw.rectangle((margin, small_top, width - margin, small_top + 1), fill=rule)
    draw.text(
        (margin, small_top + 14),
        "16PX · 실제 크기",
        font=meta_font,
        fill=muted,
    )
    for index, row in enumerate(rows):
        cell_x = margin + index * (strip_cell + 16)
        small_path = Path(str(row.get("small") or ""))
        if small_path.is_file():
            with Image.open(small_path) as source:
                tiny = source.convert("RGBA").resize((16, 16), Image.NEAREST)
            tile = Image.new("RGB", (16, 16), paper)
            tile.paste(tiny, (0, 0), tiny)
            canvas.paste(tile, (int(cell_x + (strip_cell - 16) / 2), small_top + 56))
        label = str(row.get("family") or row.get("id") or "")[:14]
        draw.text((int(cell_x + 2), small_top + 84), label, font=small_font, fill=muted)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG", optimize=True)
    return {
        "path": str(out),
        "width": width,
        "height": height,
        "count": count,
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
    }


def gate_table_markdown(
    rows: Sequence[Mapping[str, Any]],
    *,
    title: str = "후보 게이트 표",
) -> str:
    """후보 × 게이트 표(✓/✗)와 실패 사유 목록. 사람이 HITL에서 읽는다."""
    gate_ids: List[str] = []
    for row in rows:
        for gate in row.get("gates") or []:
            gate_id = str(gate.get("id"))
            if gate_id not in gate_ids:
                gate_ids.append(gate_id)
    lines = [f"# {title}", ""]
    header = ["후보", "가족"] + gate_ids
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    failures: List[str] = []
    for row in rows:
        gates = {str(gate.get("id")): gate for gate in row.get("gates") or []}
        cells = [str(row.get("id") or "—"), str(row.get("family") or "—")]
        for gate_id in gate_ids:
            gate = gates.get(gate_id)
            if gate is None:
                cells.append("누락")
            elif gate.get("skipped"):
                cells.append("−")
            elif gate.get("ok"):
                cells.append("✓")
            else:
                cells.append("✗")
        lines.append("| " + " | ".join(cells) + " |")
        for gate_id, gate in gates.items():
            if not gate.get("ok") and not gate.get("skipped"):
                failures.append(
                    f"- `{row.get('id')}` · {gate_id}: {gate.get('detail')}"
                )
    if failures:
        lines += ["", "## 실패 사유", ""] + failures
    lines += ["", "## 후보 메모", ""]
    for row in rows:
        lines.append(f"- `{row.get('id')}` — {row.get('note') or ''}")
    return "\n".join(lines) + "\n"
