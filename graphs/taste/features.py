"""taste/features — 렌더 산출물에서 결정론적 시각 특징을 뽑는다 (GRAPH-35).

같은 파일을 두 번 넣으면 항상 같은 숫자가 나온다. 머신러닝 라이브러리는 쓰지
않고 **Pillow + 표준 라이브러리**만 쓴다. 값은 "심사 점수"가 아니라 취향을
비교하기 위한 거친 눈금이다 — 순위의 근거로만 쓰고, 절대 품질로 읽지 않는다.

공통 출력(키는 항상 같다)::

    {
      "whitespace_ratio":    0.0 ~ 1.0,   # 종이색(배경) 픽셀 비율
      "distinct_color_count": int,        # 서로 다른 색 수
      "max_contrast":        float,       # 배경 대비 가장 진한 글자색의 WCAG 비율
      "type_size_steps":     int | None,  # 서로 다른 글자 크기 단계 수
      "element_density":     float,       # 1000px²당 그려진 요소 수
      "alignment_edges":     int,         # 같은 x에 맞춰진 가장자리 수(추정)
      "source":              "svg" | "png"
    }

형식별 계산 방법
----------------

**PNG(픽셀)**

- 종이색 = 테두리 픽셀에서 가장 많은 색. 투명 PNG는 흰 종이 위에 합성한다.
- 여백 = 종이색과 채널당 12 이내로 가까운 픽셀의 비율(흰 페이지와 미색 종이를
  함께 종이로 본다).
- 색 수 = RGB를 채널당 16단계로 줄인 뒤 센 값(사진의 미세한 그라데이션을
  색 수로 오해하지 않게 하려는 장치).
- 대비 = 종이색과 3:1 이상 차이 나는 잉크색 중 가장 많은 색을 "글자색"으로 보고
  WCAG 공식으로 잰다. 옅은 안티에일리어싱을 글자색으로 오인하지 않기 위해서다.
- 밀도 = 종이색이 아닌 픽셀 덩어리(8방향 연결, 3px 미만 잡음 제외) 수.
- 정렬 = 덩어리 왼쪽 x를 폭의 2% 칸으로 나눴을 때 2개 이상 겹치는 칸 수.
- 글자 크기 단계는 픽셀만으로 알 수 없어 `None`이다(거짓 숫자를 만들지 않는다).

**SVG(구조)**

- 캔버스는 `width`/`height`(없으면 `viewBox`)에서 읽는다.
- 채워진 도형 넓이의 합을 캔버스로 자른다. 겹친 도형은 두 번 세므로 여백이
  실제보다 **작게** 나올 수 있다(보수적 추정).
- `path`·`polygon`은 외곽 상자의 35%, 글자는 `0.55 × 글자크기 × 글자수` 폭과
  `1.2 × 글자크기` 높이로 어림한다.
- 여백 = 1 − (그려진 넓이 / 캔버스).
- 대비 = 글자에 쓰인 색 중 가장 많은 색과 배경색의 WCAG 비율.
- 밀도 = 그려진 요소 수 / 캔버스 넓이 × 1000.
- 정렬 = x 좌표를 폭의 1% 칸으로 나눴을 때 2개 이상 겹치는 칸 수.

색은 `#rgb`·`#rrggbb`·`rgb()`와 이름 몇 개(black/white/gray/red/green/blue)만
읽는다. `url(#...)`·`currentColor`는 색으로 세지 않는다.

사용::

    from graphs.taste import features

    features.extract_features("/path/render.svg")
    features.safe_extract_features("/path/render.png")   # 실패하면 {}
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 픽셀 분석 시 긴 변을 이 크기로 줄인다(속도와 결정론의 균형).
MAX_PIXELS = 480
#: 종이색 판정 허용 오차(채널당). 흰 페이지와 미색 종이를 함께 종이로 본다.
PAPER_TOLERANCE = 12
#: 이 대비 미만의 잉크색은 안티에일리어싱으로 보고 글자색 후보에서 뺀다.
MIN_TEXT_CONTRAST = 3.0
#: PNG에서 이보다 작은 잉크 덩어리는 잡음으로 본다.
MIN_BLOB_PIXELS = 3
#: SVG path/polygon 외곽 상자 중 실제로 칠해졌다고 보는 비율.
PATH_FILL_RATIO = 0.35
#: 글자 폭·높이 어림 계수(폰트를 읽지 않고 구조만 볼 때).
TEXT_WIDTH_RATIO = 0.55
TEXT_LINE_RATIO = 1.2

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
_SKIP_TAGS = {
    "defs",
    "clippath",
    "mask",
    "pattern",
    "marker",
    "symbol",
    "filter",
    "lineargradient",
    "radialgradient",
    "metadata",
    "title",
    "desc",
    "style",
    "script",
}
_NAMED_COLORS = {
    "black": "#000000",
    "white": "#ffffff",
    "gray": "#808080",
    "grey": "#808080",
    "red": "#ff0000",
    "green": "#008000",
    "blue": "#0000ff",
}


class FeatureError(ValueError):
    """특징을 뽑을 수 없는 파일(형식·손상)일 때."""


# ---------------------------------------------------------------------------
# 색·대비
# ---------------------------------------------------------------------------


def normalize_color(value: Optional[str]) -> Optional[str]:
    """색 문자열을 `#rrggbb`로. 알 수 없으면 None."""
    if not value:
        return None
    text = str(value).strip().lower()
    if text in ("", "none", "transparent", "currentcolor", "inherit"):
        return None
    if text.startswith("url("):
        return None
    if text in _NAMED_COLORS:
        return _NAMED_COLORS[text]
    match = re.fullmatch(r"#([0-9a-f]{3})", text)
    if match:
        r, g, b = match.group(1)
        return f"#{r}{r}{g}{g}{b}{b}"
    match = re.fullmatch(r"#([0-9a-f]{6})", text)
    if match:
        return f"#{match.group(1)}"
    match = re.fullmatch(
        r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})", text
    )
    if match:
        channels = [max(0, min(255, int(part))) for part in match.groups()]
        return "#%02x%02x%02x" % tuple(channels)
    return None


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    text = hex_color.lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def relative_luminance(hex_color: str) -> float:
    """WCAG 상대 휘도(0~1)."""
    channels = []
    for value in _rgb(hex_color):
        part = value / 255.0
        channels.append(
            part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(color_a: str, color_b: str) -> float:
    """두 색의 WCAG 대비(1.0~21.0)."""
    light, dark = sorted(
        (relative_luminance(color_a), relative_luminance(color_b)), reverse=True
    )
    return round((light + 0.05) / (dark + 0.05), 2)


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def extract_features(path: str | Path) -> Dict[str, Any]:
    """SVG 또는 이미지 파일 하나에서 특징을 뽑는다(결정론)."""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".svg":
        return svg_features(target)
    if suffix in IMAGE_SUFFIXES:
        return image_features(target)
    raise FeatureError(f"지원하지 않는 형식입니다: {suffix or '(확장자 없음)'}")


def safe_extract_features(path: str | Path) -> Dict[str, Any]:
    """특징 추출 실패를 빈 사전으로 낮춘다 — 기록이 특징 때문에 멈추면 안 된다."""
    try:
        return extract_features(path)
    except (FeatureError, OSError, ValueError, ET.ParseError):
        return {}


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _style_map(element: ET.Element) -> Dict[str, str]:
    """`style="fill:#fff; font-size:12px"`를 사전으로."""
    raw = element.get("style") or ""
    result: Dict[str, str] = {}
    for chunk in raw.split(";"):
        if ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        result[key.strip().lower()] = value.strip()
    return result


def _length(value: Optional[str]) -> Optional[float]:
    """`1200`, `1200px`을 숫자로. `%`·`em`은 None."""
    if value is None:
        return None
    match = re.match(r"^\s*(-?[0-9.]+)\s*(px)?\s*$", str(value))
    return float(match.group(1)) if match else None


def _canvas(root: ET.Element) -> Tuple[float, float]:
    width = _length(root.get("width"))
    height = _length(root.get("height"))
    if width and height:
        return width, height
    view_box = root.get("viewBox") or root.get("viewbox") or ""
    parts = re.findall(r"-?[0-9.]+", view_box)
    if len(parts) == 4:
        return float(parts[2]) or 1200.0, float(parts[3]) or 900.0
    return width or 1200.0, height or 900.0


def _float_attr(element: ET.Element, name: str, default: float = 0.0) -> float:
    value = _length(element.get(name))
    return default if value is None else value


def _points_bbox(raw: str) -> Optional[Tuple[float, float, float, float]]:
    numbers = [float(part) for part in re.findall(r"-?[0-9.]+", raw or "")]
    if len(numbers) < 4:
        return None
    xs = numbers[0::2]
    ys = numbers[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _path_bbox(raw: str) -> Optional[Tuple[float, float, float, float]]:
    numbers = [float(part) for part in re.findall(r"-?[0-9.]+", raw or "")]
    if len(numbers) < 4:
        return None
    xs = numbers[0::2]
    ys = numbers[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _element_area(
    tag: str,
    element: ET.Element,
    font_size: float,
) -> Tuple[float, Optional[Tuple[float, float, float, float]]]:
    """요소가 칠하는 넓이와 외곽 상자(없으면 None)를 어림한다."""
    if tag == "rect":
        width = _float_attr(element, "width")
        height = _float_attr(element, "height")
        x, y = _float_attr(element, "x"), _float_attr(element, "y")
        return width * height, (x, y, x + width, y + height)
    if tag == "circle":
        radius = _float_attr(element, "r")
        cx, cy = _float_attr(element, "cx"), _float_attr(element, "cy")
        return math.pi * radius * radius, (
            cx - radius,
            cy - radius,
            cx + radius,
            cy + radius,
        )
    if tag == "ellipse":
        rx, ry = _float_attr(element, "rx"), _float_attr(element, "ry")
        cx, cy = _float_attr(element, "cx"), _float_attr(element, "cy")
        return math.pi * rx * ry, (cx - rx, cy - ry, cx + rx, cy + ry)
    if tag == "line":
        x1, y1 = _float_attr(element, "x1"), _float_attr(element, "y1")
        x2, y2 = _float_attr(element, "x2"), _float_attr(element, "y2")
        width = _float_attr(element, "stroke-width", 1.0)
        area = math.hypot(x2 - x1, y2 - y1) * max(width, 1.0)
        return area, (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    if tag in ("polygon", "polyline"):
        box = _points_bbox(element.get("points") or "")
        if not box:
            return 0.0, None
        area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
        return area * PATH_FILL_RATIO, box
    if tag == "path":
        box = _path_bbox(element.get("d") or "")
        if not box:
            return 0.0, None
        area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
        return area * PATH_FILL_RATIO, box
    if tag == "text":
        text = "".join(element.itertext()).strip()
        if not text:
            return 0.0, None
        width = TEXT_WIDTH_RATIO * font_size * len(text)
        height = TEXT_LINE_RATIO * font_size
        x = _float_attr(element, "x")
        y = _float_attr(element, "y") - font_size
        return width * height, (x, y, x + width, y + height)
    if tag == "image":
        width = _float_attr(element, "width")
        height = _float_attr(element, "height")
        x, y = _float_attr(element, "x"), _float_attr(element, "y")
        return width * height, (x, y, x + width, y + height)
    return 0.0, None


def svg_features(path: str | Path) -> Dict[str, Any]:
    """SVG 구조에서 특징을 뽑는다(픽셀로 굽지 않는다 — inkscape 불필요)."""
    tree = ET.parse(str(path))
    root = tree.getroot()
    width, height = _canvas(root)
    canvas_area = max(1.0, width * height)

    colors: set[str] = set()
    fill_areas: Counter = Counter()
    text_chars: Counter = Counter()
    font_sizes: set[float] = set()
    x_positions: List[float] = []
    painted = 0
    painted_area = 0.0
    background: Optional[str] = None
    background_area = 0.0

    def walk(element: ET.Element, inherited: Dict[str, str], depth: int = 0) -> None:
        nonlocal painted, painted_area, background, background_area
        if depth > 40:
            return
        tag = _local(element.tag)
        if tag in _SKIP_TAGS:
            return

        style = _style_map(element)
        if (style.get("display") or "").lower() == "none":
            return
        if (style.get("opacity") or element.get("opacity") or "1") in ("0", "0.0"):
            return

        fill = normalize_color(
            style.get("fill") or element.get("fill")
        ) or inherited.get("fill")
        stroke = normalize_color(style.get("stroke") or element.get("stroke"))
        font_raw = style.get("font-size") or element.get("font-size")
        font_size = _length(font_raw)
        if font_size is None:
            font_size = inherited.get("font_size")

        child_inherited = {
            "fill": fill,
            "stroke": stroke,
            "font_size": font_size,
        }

        area, box = _element_area(tag, element, float(font_size or 16.0))
        is_background = (
            tag == "rect"
            and box is not None
            and area >= canvas_area * 0.5
            and fill is not None
        )
        if is_background:
            # 캔버스 절반 이상을 덮는 사각형은 "칠한 내용"이 아니라 종이다.
            if area > background_area:
                background = fill
                background_area = area
            colors.add(fill)
        visible = (
            area > 0
            and not is_background
            and (fill is not None or stroke is not None or tag == "text")
        )
        if visible:
            painted += 1
            painted_area += area
            if fill:
                colors.add(fill)
                fill_areas[fill] += area
            if stroke:
                colors.add(stroke)
            if tag == "text":
                text = "".join(element.itertext()).strip()
                text_chars[fill or inherited.get("fill") or "#000000"] += len(text)
            if font_size:
                font_sizes.add(round(float(font_size), 2))
            if box:
                x_positions.append(box[0])

        for child in list(element):
            walk(child, child_inherited, depth + 1)

    walk(root, {})
    if background is None:
        background = "#ffffff"

    whitespace = 1.0 - min(1.0, painted_area / canvas_area)
    dominant_text = text_chars.most_common(1)[0][0] if text_chars else None
    if dominant_text is None:
        dominant_text = fill_areas.most_common(1)[0][0] if fill_areas else None
    contrast = contrast_ratio(dominant_text, background) if dominant_text else 1.0

    return {
        "whitespace_ratio": round(whitespace, 4),
        "distinct_color_count": len(colors),
        "max_contrast": contrast,
        "type_size_steps": len(font_sizes),
        "element_density": round(painted / canvas_area * 1000.0, 4),
        "alignment_edges": _alignment_edges(x_positions, width, ratio=0.01),
        "source": "svg",
    }


def _alignment_edges(positions: Sequence[float], width: float, *, ratio: float) -> int:
    """같은 칸에 2개 이상 모인 x 위치 수(추정)."""
    step = max(1.0, width * ratio)
    buckets = Counter(int(round(value / step)) for value in positions)
    return sum(1 for count in buckets.values() if count >= 2)


# ---------------------------------------------------------------------------
# PNG / 이미지
# ---------------------------------------------------------------------------


def _is_paper(pixel: Tuple[int, int, int], paper: Tuple[int, int, int]) -> bool:
    return (
        abs(pixel[0] - paper[0]) <= PAPER_TOLERANCE
        and abs(pixel[1] - paper[1]) <= PAPER_TOLERANCE
        and abs(pixel[2] - paper[2]) <= PAPER_TOLERANCE
    )


def _paper_color(pixels: Sequence[Tuple[int, int, int]], width: int, height: int):
    border = list(pixels[:width]) + list(pixels[-width:])
    for row in range(height):
        border.append(pixels[row * width])
        border.append(pixels[row * width + width - 1])
    return Counter(border).most_common(1)[0][0]


def _dominant_text_color(ink_colors: Counter, paper_hex: str) -> str:
    """종이와 충분히 다른 잉크색 중 가장 많은 색. 없으면 그냥 가장 많은 색."""
    for (red, green, blue), _count in ink_colors.most_common():
        candidate = "#%02x%02x%02x" % (red, green, blue)
        if contrast_ratio(candidate, paper_hex) >= MIN_TEXT_CONTRAST:
            return candidate
    red, green, blue = ink_colors.most_common(1)[0][0]
    return "#%02x%02x%02x" % (red, green, blue)


def image_features(path: str | Path) -> Dict[str, Any]:
    """이미지 픽셀에서 특징을 뽑는다(Pillow 필요)."""
    from PIL import Image  # 시각 특징을 실제로 뽑을 때만 가져온다

    with Image.open(str(path)) as handle:
        rgba = handle.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        composed = Image.alpha_composite(background, rgba).convert("RGB")
        composed.thumbnail((MAX_PIXELS, MAX_PIXELS))
        width, height = composed.size
        # `getdata()`는 Pillow 14에서 사라진다 — 바이트로 받아 3채널로 묶는다.
        raw = composed.tobytes()
        pixels = list(zip(raw[0::3], raw[1::3], raw[2::3]))

    if width <= 0 or height <= 0:
        raise FeatureError("이미지 크기가 0입니다")

    paper = _paper_color(pixels, width, height)
    paper_hex = "#%02x%02x%02x" % paper
    mask = bytearray(len(pixels))
    ink_colors: Counter = Counter()
    ink_count = 0
    for index, pixel in enumerate(pixels):
        if _is_paper(pixel, paper):
            continue
        mask[index] = 1
        ink_count += 1
        ink_colors[pixel] += 1

    whitespace = 1.0 - ink_count / len(pixels)
    distinct = len({(r >> 4, g >> 4, b >> 4) for r, g, b in pixels})
    if ink_colors:
        dominant_hex = _dominant_text_color(ink_colors, paper_hex)
        contrast = contrast_ratio(dominant_hex, paper_hex)
    else:
        contrast = 1.0

    blobs = _blobs(mask, width, height)
    left_edges = [left for left, _top, _right, _bottom, _size in blobs]
    return {
        "whitespace_ratio": round(whitespace, 4),
        "distinct_color_count": distinct,
        "max_contrast": contrast,
        "type_size_steps": None,
        "element_density": round(len(blobs) / (width * height) * 1000.0, 4),
        "alignment_edges": _alignment_edges(left_edges, width, ratio=0.02),
        "source": "png",
    }


def _blobs(
    mask: bytearray, width: int, height: int
) -> List[Tuple[int, int, int, int, int]]:
    """8방향 연결 잉크 덩어리의 외곽 상자와 크기 목록(작은 잡음 제외)."""
    seen = bytearray(len(mask))
    result: List[Tuple[int, int, int, int, int]] = []
    for start in range(len(mask)):
        if not mask[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        size = 0
        left = right = start % width
        top = bottom = start // width
        while stack:
            index = stack.pop()
            size += 1
            x, y = index % width, index // width
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx, ny = x + dx, y + dy
                    if nx < 0 or ny < 0 or nx >= width or ny >= height:
                        continue
                    neighbour = ny * width + nx
                    if mask[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
        if size >= MIN_BLOB_PIXELS:
            result.append((left, top, right, bottom, size))
    return result
