"""print profile — 라벨·파우치 전개도 → 인쇄 PDF 게이트 → 납품 팩 (GRAPH-17).

`spec["package"]` 하나로 인쇄물 한 종을 만든다. 라벨은 sonagi-assets의
`scripts/render_label.py`를 그대로 쓰고, 파우치는 같은 레이어 규약
(`dieline`·`fold`·`art`·`barcode`)의 최소 전면 템플릿을 직접 만든다.

두 단계가 있다(`spec["print_ready"]`, 기본 `False`).

- **entry (False)** — 콘테스트 제출용 표현물. SVG 렌더 → PNG(vectorcraft
  convert) → JPG(Pillow, 흰 배경) → 목업 → meta/entry.md. 게이트는 순수
  파이썬·SVG 구조 검사만 한다. `gs`·`chromium`·`pdffonts`·`export_print.py`를
  **한 번도 호출하지 않는다**(langgraph 컨테이너에 vectorcraft-cli만 있는 환경).
- **handoff (True)** — 인쇄소 넘김용. PDF/X 변환(`export_print.py`), 잉크량
  (`gs -sDEVICE=inkcov`), 폰트 임베드(`pdffonts`) 검사까지 한다.

게이트는 **실패 시 멈춘다**(fail closed). 어떤 게이트든 실패하면 meta·entry.md를
포함한 팩 산출물이 만들어지지 않는다. 테스트에서만
`spec["allow_print_checks"] = True`로 건너뛸 수 있다(실패는 `skipped`로 남는다).

spec 예::

    {
      "profile": "print",
      "print_ready": False,          # True면 인쇄소 핸드오프(PDF/X)까지
      "package": {
        "type": "label",
        "brand": "SONAGI",
        "product": "비 오는 날의 차",
        "net": "50g · 10 티백",
        "ingredients": "녹차 100% (국산)",
        "origin": "MADE IN KOREA",
        "size_mm": [90, 60],
        "barcode": "8801234567890",
        "colors": {"ink": "#161616", "paper": "#fbf9f5",
                   "accent": "#b4551f", "calm": "#1f4536"},
        "mark_path": "/path/mark.svg"
      }
    }

entry 게이트:
- `print.assets`      — SVG·PNG·JPG가 존재하고 0바이트보다 크다
- `print.layers`      — `<g id="dieline">`·`<g id="fold">` 레이어가 있다
- `print.barcode`     — 바코드가 있으면 조용영역 rect(`barcode-quiet`)가 있다
- `print.render_size` — 렌더 PNG가 image_width·max_height 상한 안에 들어온다

handoff 게이트:
- `print.export_check` — `export_print.py --check` (재단/도련 3mm·안전여백·레이어)
- `print.pdf`          — 출력 PDF 존재 + 0바이트 초과
- `print.fonts`        — `pdffonts`에 임베드 안 된 폰트 없음
- `print.inkcov`       — `gs -sDEVICE=inkcov`가 CMYK를 보고
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from graphs.core import artifacts as core_artifacts
from graphs.core import env as core_env
from graphs.core import exec as core_exec
from graphs.profiles import base
from graphs.profiles import presets as profile_presets

MM = 96 / 25.4  # 1mm = 3.7795px
PACKAGE_TYPES = ("label", "pouch")
BLEED_MM = 3.0
SAFE_PAD_MM = 6.0
MARK_SIZE_MM = 14.0

DEFAULT_SPEC: Dict[str, Any] = {
    "image_width": 2000,
    "max_height": 3000,
    "jpg_quality": 90,
    "bleed_mm": BLEED_MM,
    "allow_print_checks": False,
    # False(entry): 콘테스트 제출용 표현물만. True(handoff): 인쇄소 PDF/X까지.
    "print_ready": False,
    # False(기본): PNG·JPG 표현물에서 전개도(dieline)·접힘선(fold)을 숨긴다.
    # True면 가이드를 그대로 보여준다. 원본 SVG는 항상 두 레이어를 유지한다.
    "guides": False,
    # 인쇄물은 방향/레퍼런스가 산출물에 쓰이지 않는다 — 네트워크·LLM 없이 결정론으로 돈다.
    "skip_knowledge": True,
    "skip_llm": True,
}

DEFAULT_COLORS = {
    "ink": "#161616",
    "paper": "#fbf9f5",
    "accent": "#b4551f",
    "calm": "#1f4536",
}

#: 전개도 레이어 계약 — 형식별로 반드시 있어야 하는 그룹 id.
#: 접힘선이 없는 형식(예: 단면 스티커)은 이 표에 `()`로 두고 사유를 적는다.
LAYER_CONTRACT: Dict[str, Tuple[str, ...]] = {
    "label": ("dieline", "fold"),
    "pouch": ("dieline", "fold"),
}

ENTRY_GATES: Tuple[base.Gate, ...] = (
    base.Gate("print.assets", "SVG·PNG·JPG가 존재하고 0바이트보다 크다"),
    base.Gate("print.layers", "SVG에 dieline·fold 레이어 그룹이 있다"),
    base.Gate("print.barcode", "바코드가 있으면 조용영역(barcode-quiet) rect가 있다"),
    base.Gate("print.render_size", "렌더 PNG가 image_width·max_height 상한 안에 있다"),
)

HANDOFF_GATES: Tuple[base.Gate, ...] = (
    base.Gate(
        "print.export_check",
        "export_print.py --check: 재단/도련 3mm·안전여백·레이어 분리",
    ),
    base.Gate("print.pdf", "출력 PDF가 존재하고 0바이트보다 크다"),
    base.Gate("print.fonts", "pdffonts: 임베드되지 않은 폰트가 없다"),
    base.Gate("print.inkcov", "gs -sDEVICE=inkcov: CMYK 색공간"),
)

#: 두 단계가 요구할 수 있는 게이트 전체(문서·린트 판정용). 실행되는 게이트는
#: `pack`이 `spec["print_ready"]`로 고른다.
GATES: Tuple[base.Gate, ...] = ENTRY_GATES + HANDOFF_GATES


class PrintGateError(RuntimeError):
    """인쇄 게이트 실패. 어떤 검사가 왜 막았는지 그대로 들고 있다."""

    def __init__(self, failures: Sequence[Mapping[str, Any]]):
        self.failures = [dict(item) for item in failures]
        lines = [f"인쇄 게이트 실패 {len(self.failures)}건 — 납품을 중단한다."]
        for item in self.failures:
            lines.append(f"- {item.get('id')}: {item.get('detail')}")
        lines.append("테스트에서만 spec['allow_print_checks']=true로 건너뛸 수 있다.")
        super().__init__("\n".join(lines))


def _run(argv: Sequence[Any], timeout: Optional[float] = 300, **kwargs: Any) -> str:
    """core/exec 경유 실행. 테스트는 이 함수만 monkeypatch하면 된다."""
    kwargs.setdefault("cwd", core_env.ASSETS_DIR)
    return core_exec.run([str(arg) for arg in argv], timeout=timeout, **kwargs).stdout


def _context(state: Mapping[str, Any]) -> base.ProfileContext:
    ctx = state.get("profile_ctx")
    if isinstance(ctx, base.ProfileContext):
        return ctx
    return base.ProfileContext(run=_run, out_root=Path(core_env.out_root("entry_pack")))


def _tail(exc: BaseException, limit: int = 400) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[-limit:] if len(text) > limit else text


# ---------------------------------------------------------------------------
# spec 정규화
# ---------------------------------------------------------------------------


def _number(value: float) -> str:
    return f"{float(value):g}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    if slug:
        return slug
    # 한글 등 비ASCII 브랜드는 해시로 안정적인 슬러그를 만든다(충돌 방지).
    return "brand-" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:6]


def package_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """`spec['package']`를 검증·정규화한다. 누락은 실행 전에 멈춘다."""
    package = spec.get("package")
    if not isinstance(package, Mapping):
        raise base.ProfileError(
            "print profile: spec['package']가 필요하다 — "
            "type(label|pouch)·brand·product·size_mm[2]를 담은 사전을 넣어라"
        )
    missing = [key for key in ("brand", "product", "size_mm") if key not in package]
    if missing:
        raise base.ProfileError(
            f"print profile: spec['package']에 필수 항목이 없다 — {', '.join(missing)}"
        )

    kind = str(package.get("type") or "label").strip().lower()
    if kind not in PACKAGE_TYPES:
        raise base.ProfileError(
            f"print profile: package.type은 {'|'.join(PACKAGE_TYPES)} 중 하나다 — {kind!r}"
        )

    size = package.get("size_mm")
    if not isinstance(size, (list, tuple)) or len(size) != 2:
        raise base.ProfileError(
            "print profile: package.size_mm은 [가로mm, 세로mm] 두 값이어야 한다"
        )
    try:
        width, height = float(size[0]), float(size[1])
    except (TypeError, ValueError):
        raise base.ProfileError(
            f"print profile: package.size_mm이 숫자가 아니다 — {size!r}"
        ) from None
    if width <= 0 or height <= 0:
        raise base.ProfileError("print profile: package.size_mm은 양수여야 한다")

    mark = package.get("mark_path")
    if mark and not Path(str(mark)).is_file():
        raise base.ProfileError(f"print profile: mark_path 파일이 없다 — {mark}")

    normalized = dict(package)
    normalized["type"] = kind
    normalized["size_mm"] = [width, height]
    normalized["colors"] = {**DEFAULT_COLORS, **(package.get("colors") or {})}
    normalized.setdefault("barcode", "8800000000000")

    # 바코드 조용영역과 안전 여백이 들어갈 최소 크기. 모자라면 전개도가 음수 좌표로
    # 나가므로 렌더 전에 멈춘다(작은 사이즈는 실제 라벨에서도 읽을 수 없다).
    _, bars_w = ean_bars(str(normalized["barcode"]))
    quiet = 4.6 * MM
    pad = 6 * MM
    if width * MM - 2 * pad < bars_w + 2 * quiet + 12 * MM:
        raise base.ProfileError(
            f"print profile: size_mm 가로가 너무 작다 — 바코드+조용영역+여백에 "
            f"{((bars_w + 2 * quiet + 2 * pad + 12 * MM) / MM):.0f}mm 이상 필요하다"
        )
    if height * MM - 2 * pad < 14 * MM + 2 * quiet:
        raise base.ProfileError(
            f"print profile: size_mm 세로가 너무 작다 — 바코드 높이+조용영역+여백에 "
            f"{((14 * MM + 2 * quiet + 2 * pad) / MM):.0f}mm 이상 필요하다"
        )
    return normalized


# ---------------------------------------------------------------------------
# 전개도(SVG)
# ---------------------------------------------------------------------------


def ean_bars(digits: str) -> Tuple[List[Tuple[int, int]], int]:
    """EAN-13 근사 막대 패턴(결정론). `render_label.py`와 같은 알고리즘."""
    seed = int(hashlib.sha256(str(digits).encode()).hexdigest()[:8], 16)
    bars: List[Tuple[int, int]] = []
    x = 0
    for index in range(60):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        width = 1 + (seed >> 16) % 3
        if index % 2 == 0:
            bars.append((x, width))
        x += width
    return bars, x


def _is_cjk(char: str) -> bool:
    """한중일 문자는 한 글자씩 줄바꿈해도 뜻이 깨지지 않는다."""
    return ord(char) >= 0x2E80


def _char_width(char: str, size_px: float) -> float:
    return size_px if _is_cjk(char) else size_px * 0.55


def _text_width(text: str, size_px: float) -> float:
    return sum(_char_width(char, size_px) for char in text)


def _wrap_tokens(text: str) -> List[str]:
    """줄바꿈 단위로 나눈다. CJK는 글자, 라틴은 공백으로 구분한 단어."""
    tokens: List[str] = []
    word = ""
    for char in text:
        if _is_cjk(char):
            if word:
                tokens.append(word)
                word = ""
            tokens.append(char)
        elif char.isspace():
            if word:
                tokens.append(word)
                word = ""
            tokens.append(" ")
        else:
            word += char
    if word:
        tokens.append(word)
    return tokens


def wrap_text(text: str, size_px: float, max_width: float) -> List[str]:
    """CJK는 1em 글자 단위, 라틴은 단어 단위로 줄바꿈한다.

    라틴 단어는 중간에서 자르지 않는다. 한 단어가 폭을 넘을 때만 글자 단위로
    쪼개고, 그 외에는 공백에서 줄을 바꾼다. 빈 줄(`\\n`)은 그대로 보존한다.
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    size_px = max(float(size_px), 1.0)
    max_width = max(float(max_width), size_px)
    lines: List[str] = []

    for paragraph in raw.splitlines():
        current = ""
        width = 0.0
        pending_space = False

        for token in _wrap_tokens(paragraph):
            if token == " ":
                pending_space = bool(current)
                continue

            token_w = _text_width(token, size_px)
            space_w = _char_width(" ", size_px) if pending_space else 0.0
            if current and width + space_w + token_w > max_width:
                lines.append(current)
                current, width = "", 0.0
                pending_space = False
                space_w = 0.0

            if pending_space and current:
                current += " "
                width += space_w
            pending_space = False

            if token_w > max_width:
                # 한 단어가 한 줄보다 길 때만 글자 단위로 쪼갠다.
                for char in token:
                    char_w = _char_width(char, size_px)
                    if current and width + char_w > max_width:
                        lines.append(current)
                        current, width = "", 0.0
                    current += char
                    width += char_w
            else:
                current += token
                width += token_w

        lines.append(current)

    return lines


def _hide_guide_layers(svg_text: str) -> str:
    """표현물용 사본에서 전개도·접힘선 그룹을 숨긴다(레이어 삭제 아님)."""
    pattern = re.compile(r'<g\b(?=[^>]*\bid="(?:dieline|fold)")', re.IGNORECASE)

    def hide(match: re.Match[str]) -> str:
        return match.group(0) + ' display="none"'

    return pattern.sub(hide, svg_text)


def _text(value: Any) -> str:
    """SVG 텍스트 노드 이스케이프 — `&`, `<`, `>`."""
    return escape(str(value))


def _attr(value: Any) -> str:
    """SVG 속성값 이스케이프 — 따옴표까지."""
    return escape(str(value), {'"': "&quot;"})


def _xml_package(package: Mapping[str, Any]) -> Dict[str, Any]:
    """render_label.py에 넘길 spec 사본.

    그 스크립트는 값을 이스케이프하지 않고 SVG에 꽂는다. `&`·`<`가 든 상품명이
    깨진 XML을 만들지 않도록 경계에서 한 번만 이스케이프한다(파우치는 직접 만든다).
    """
    escaped = dict(package)
    for key in ("brand", "product", "net", "ingredients", "origin", "type_family"):
        if escaped.get(key) is not None:
            escaped[key] = _text(escaped[key])
    if escaped.get("barcode") is not None:
        escaped["barcode"] = _text(escaped["barcode"])
    escaped["colors"] = {
        key: _attr(value) for key, value in (package.get("colors") or {}).items()
    }
    return escaped


def _mark_box(
    width_px: float, pad: float, has_mark: bool
) -> Optional[Tuple[float, float, float]]:
    """우상단 마크의 (x, y, size). 마크가 없으면 None."""
    if not has_mark:
        return None
    size = MARK_SIZE_MM * MM
    return width_px - pad - size, pad + 6, size


def _origin_position(
    width_px: float, pad: float, has_mark: bool
) -> Tuple[float, float]:
    """출처 텍스트의 (x, y). 마크가 있으면 겹치지 않게 마크 아래에 둔다.

    기준선(baseline)은 마크 하단 + 폰트 크기(8px) + 여유(4px)다. x는 안전 여백
    안쪽에서 오른쪽 정렬하므로 텍스트가 캔버스 밖으로 나가지 않는다.
    """
    box = _mark_box(width_px, pad, has_mark)
    y = pad + 54 if box is None else box[1] + box[2] + 8 + 4
    return width_px - pad, y


_ORIGIN_TEXT_RE = re.compile(
    r'<text\b[^>]*\btext-anchor="end"[^>]*>.*?</text>', re.IGNORECASE | re.DOTALL
)


def _relayout_origin(svg_text: str, package: Mapping[str, Any], width_px: float) -> str:
    """출처 텍스트를 마크와 겹치지 않는 위치로 옮긴다(레이아웃 후처리).

    `render_label.py`가 그리는 출처 텍스트는 우상단 마크와 같은 자리다. 원본
    스크립트를 고치지 않고 SVG에서 위치만 다시 계산한다. 해당 텍스트가 없으면
    원문을 그대로 돌려준다.
    """
    has_mark = bool(package.get("mark_path"))
    x, y = _origin_position(width_px, SAFE_PAD_MM * MM, has_mark)

    def move(match: re.Match[str]) -> str:
        tag = match.group(0)
        tag = re.sub(r'\bx="[^"]*"', f'x="{x:.1f}"', tag, count=1)
        tag = re.sub(r'\by="[^"]*"', f'y="{y:.1f}"', tag, count=1)
        return tag

    return _ORIGIN_TEXT_RE.sub(move, svg_text)


def _mark_tag(package: Mapping[str, Any], width_px: float, pad: float) -> str:
    """mark_path(SVG)를 base64 이미지 태그로 만든다(없으면 빈 문자열)."""
    mark = package.get("mark_path")
    if not mark:
        return ""
    data = base64.b64encode(Path(str(mark)).read_bytes()).decode("ascii")
    box = _mark_box(width_px, pad, True)
    assert box is not None
    x, y, size = box
    return (
        f'<image id="mark" x="{x:.1f}" y="{y:.1f}" width="{size:.1f}" height="{size:.1f}" '
        f'href="data:image/svg+xml;base64,{data}"/>'
    )


def _inject_mark(svg_text: str, package: Mapping[str, Any], width_px: float) -> str:
    """스크립트가 만든 SVG의 art 그룹 끝에 mark를 넣는다(라벨 경로)."""
    tag = _mark_tag(package, width_px, SAFE_PAD_MM * MM)
    if not tag:
        return svg_text
    start = svg_text.find('<g id="art">')
    if start < 0:
        return svg_text
    close = svg_text.find("</g>", start)
    if close < 0:
        return svg_text
    return svg_text[:close] + tag + svg_text[close:]


def build_pouch_svg(package: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """파우치 전면 최소 템플릿. `render_label`과 같은 레이어·여백 규약."""
    trim_w, trim_h = package["size_mm"]
    width, height = float(trim_w) * MM, float(trim_h) * MM
    colors = package.get("colors") or DEFAULT_COLORS
    ink = colors.get("ink", DEFAULT_COLORS["ink"])
    paper = colors.get("paper", DEFAULT_COLORS["paper"])
    accent = colors.get("accent", DEFAULT_COLORS["accent"])
    calm = colors.get("calm", DEFAULT_COLORS["calm"])
    type_family = package.get("type_family", "Archivo, sans-serif")

    pad = 6 * MM  # 안전 여백 6mm (기준 3mm 이상)
    digits = str(package.get("barcode") or "8800000000000")
    bars, bars_w = ean_bars(digits)
    bars_h = 14 * MM
    quiet = 4.6 * MM
    bx = width - pad - (bars_w + quiet * 2)
    by = height - pad - (bars_h + quiet * 2)
    text_w = bx - pad - 2 * MM

    parts = [
        f'<rect id="page" width="{width:.1f}" height="{height:.1f}" fill="{_attr(paper)}"/>',
        # 전개도·접힘선은 아트와 분리된 그룹
        f'<g id="dieline" fill="none" stroke="#ff00ff" stroke-width="0.5" stroke-dasharray="4 3">'
        f'<rect x="0.5" y="0.5" width="{width - 1:.1f}" height="{height - 1:.1f}"/></g>',
        f'<g id="fold" fill="none" stroke="#00a0ff" stroke-width="0.5" stroke-dasharray="2 4">'
        f'<line x1="{width * 0.62:.1f}" y1="0" x2="{width * 0.62:.1f}" y2="{height:.1f}"/></g>',
        '<g id="art">',
        f'<rect x="{pad}" y="{pad}" width="{width - 2 * pad:.1f}" height="{1.2 * MM:.1f}" fill="{_attr(calm)}"/>',
        f'<text x="{pad}" y="{pad + 26:.1f}" font-family="{_attr(type_family)}" font-size="20" font-weight="700" fill="{_attr(ink)}">{_text(package["brand"])}</text>',
        f'<text x="{pad}" y="{pad + 54:.1f}" font-family="{_attr(type_family)}" font-size="13" font-weight="700" fill="{_attr(ink)}">{_text(package["product"])}</text>',
        f'<text x="{pad}" y="{pad + 74:.1f}" font-family="monospace" font-size="9" fill="{_attr(ink)}" fill-opacity="0.7">{_text(package.get("net", ""))}</text>',
    ]
    mark = _mark_tag(package, width, pad)
    if mark:
        parts.append(mark)
    origin_x, origin_y = _origin_position(width, pad, bool(mark))

    parts_ing_y = pad + 92
    for line in wrap_text(str(package.get("ingredients", "")), 8, text_w):
        parts.append(
            f'<text x="{pad}" y="{parts_ing_y:.1f}" font-family="monospace" font-size="8" '
            f'fill="{_attr(ink)}" fill-opacity="0.6">{_text(line)}</text>'
        )
        parts_ing_y += 11

    parts += [
        '<g id="barcode">',
        f'<rect id="barcode-quiet" x="{bx:.1f}" y="{by:.1f}" width="{bars_w + quiet * 2:.1f}" '
        f'height="{bars_h + quiet * 2:.1f}" fill="{_attr(paper)}"/>',
        f'<g id="barcode-bars" fill="{_attr(ink)}">',
    ]
    for offset, bar_width in bars:
        parts.append(
            f'<rect x="{bx + quiet + offset:.1f}" y="{by + quiet:.1f}" '
            f'width="{bar_width:.1f}" height="{bars_h:.1f}"/>'
        )
    parts += [
        "</g>",
        f'<text x="{bx + quiet + bars_w / 2:.1f}" y="{by + quiet + bars_h + 9:.1f}" '
        f'font-family="monospace" font-size="8" text-anchor="middle" fill="{_attr(ink)}">{_text(digits)}</text>',
        "</g>",
        f'<text x="{origin_x:.1f}" y="{origin_y:.1f}" font-family="monospace" font-size="8" '
        f'text-anchor="end" fill="{_attr(accent)}">{_text(package.get("origin", ""))}</text>',
        "</g>",
    ]

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.1f}" height="{height:.1f}" '
        f'viewBox="0 0 {width:.1f} {height:.1f}">' + "".join(parts) + "</svg>"
    )
    manifest = {
        "brand": package["brand"],
        "product": package["product"],
        "size_mm": list(package["size_mm"]),
        "canvas_px": [round(width, 1), round(height, 1)],
        "layers": ["page", "dieline", "fold", "art", "barcode"],
        "barcode": {"digits": digits, "quiet_mm": 4.6, "bars": len(bars)},
        "sha256": hashlib.sha256(svg.encode()).hexdigest(),
    }
    return svg, manifest


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------


def _ok(gate_id: str, detail: str) -> Dict[str, Any]:
    return {"id": gate_id, "ok": True, "detail": detail, "skipped": False}


def _fail(gate_id: str, detail: str) -> Dict[str, Any]:
    return {"id": gate_id, "ok": False, "detail": detail, "skipped": False}


def _gate_export_check(
    ctx: base.ProfileContext, svg: Path, size_mm: Sequence[float], bleed: float
) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        ctx.assets_dir / "scripts/export_print.py",
        "--src",
        svg,
        "--trim",
        f"{_number(size_mm[0])}x{_number(size_mm[1])}",
        "--bleed",
        _number(bleed),
        "--check",
    ]
    try:
        ctx.run(cmd)
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "print.export_check",
            "export_print.py --check 실패 — 재단 크기·도련 3mm·안전여백·레이어(dieline/fold)를 "
            f"확인하라: {_tail(exc)}",
        )
    return _ok(
        "print.export_check", f"재단 {size_mm[0]}×{size_mm[1]}mm · 도련 {bleed}mm 통과"
    )


def _gate_pdf(pdf: Path) -> Dict[str, Any]:
    if not pdf.is_file():
        return _fail(
            "print.pdf", f"출력 PDF가 없다 — export_print 실행 결과를 확인하라: {pdf}"
        )
    size = pdf.stat().st_size
    if size <= 0:
        return _fail(
            "print.pdf", f"출력 PDF가 0바이트다 — gs 변환 로그를 확인하라: {pdf}"
        )
    return _ok("print.pdf", f"{pdf.name} {size:,}B")


def _unembedded_fonts(output: str) -> Optional[List[str]]:
    """pdffonts 출력에서 emb=no 폰트 이름을 뽑는다.

    열 위치는 구분선(`----`)의 대시 구간에서 얻는다 — 폰트 이름이 길어 열 폭이
    늘어나도 정렬이 유지된다. 헤더/구분선이 없으면 `None`(해석 불가)을 돌려주고,
    호출부가 게이트를 실패시킨다(fail closed).
    """
    lines = output.splitlines()
    header_index = next(
        (
            i
            for i, line in enumerate(lines)
            if line.strip().startswith("name") and "emb" in line
        ),
        None,
    )
    if header_index is None:
        return None
    separator_index = next(
        (
            i
            for i in range(header_index + 1, len(lines))
            if lines[i].strip() and set(lines[i].strip()) <= {"-", " "}
        ),
        None,
    )
    if separator_index is None:
        return None
    spans = [(m.start(), m.end()) for m in re.finditer(r"-+", lines[separator_index])]
    if len(spans) < 4:
        return None
    start, end = spans[3]
    names: List[str] = []
    for line in lines[separator_index + 1 :]:
        if not line.strip():
            continue
        if line[start:end].strip().lower() == "no":
            names.append(line.split()[0])
    return names


def _gate_fonts(ctx: base.ProfileContext, pdf: Path) -> Dict[str, Any]:
    try:
        output = ctx.run(["pdffonts", pdf])
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "print.fonts",
            "pdffonts 실행 실패 — poppler-utils 설치와 폰트 임베드 설정을 확인하라: "
            f"{_tail(exc)}",
        )
    unembedded = _unembedded_fonts(output or "")
    if unembedded is None:
        return _fail(
            "print.fonts",
            "pdffonts 출력을 해석할 수 없다 — poppler-utils 버전과 폰트 표 형식을 확인하라: "
            f"{_tail(output or '빈 출력')}",
        )
    if unembedded:
        names = ", ".join(sorted(set(unembedded)))
        return _fail(
            "print.fonts",
            f"임베드되지 않은 폰트가 있다 — 폰트를 아웃라인화하거나 "
            f"-dEmbedAllFonts=true를 유지하라: {names}",
        )
    return _ok("print.fonts", "모든 폰트 임베드 확인")


def _gate_inkcov(ctx: base.ProfileContext, pdf: Path) -> Dict[str, Any]:
    try:
        output = ctx.run(["gs", "-q", "-o", "-", "-sDEVICE=inkcov", pdf])
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "print.inkcov",
            "gs inkcov 실행 실패 — ghostscript 설치와 PDF 무결성을 확인하라: "
            f"{_tail(exc)}",
        )
    match = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+CMYK", output or "")
    if not match:
        return _fail(
            "print.inkcov",
            "CMYK 색공간을 확인할 수 없다 — gs -sDEVICE=inkcov 출력에 CMYK 라인이 없다. "
            "export_print의 -sColorConversionStrategy=CMYK 경로를 확인하라",
        )
    coverage = ", ".join(f"{float(value):.3f}" for value in match.groups())
    return _ok("print.inkcov", f"CMYK 잉크량 {coverage}")


def run_gates(
    ctx: base.ProfileContext,
    *,
    svg: Path,
    pdf: Path,
    size_mm: Sequence[float],
    bleed: float,
    allow: bool = False,
) -> List[Dict[str, Any]]:
    """핸드오프 게이트 4종. 실패가 있으면 `PrintGateError`(allow면 skipped)."""
    results = [
        _gate_export_check(ctx, svg, size_mm, bleed),
        _gate_pdf(pdf),
        _gate_fonts(ctx, pdf),
        _gate_inkcov(ctx, pdf),
    ]
    failures = [item for item in results if not item["ok"]]
    if allow:
        for item in failures:
            item["skipped"] = True
    elif failures:
        raise PrintGateError(failures)
    return results


# ---------------------------------------------------------------------------
# entry 게이트 — 순수 파이썬 / SVG 구조. 외부 프로세스 없음
# ---------------------------------------------------------------------------


def _svg_ids(svg: Path) -> Dict[str, ET.Element]:
    """SVG에서 id 속성을 가진 요소를 모은다(깨진 XML이면 예외)."""
    root = ET.fromstring(Path(svg).read_text(encoding="utf-8"))
    found: Dict[str, ET.Element] = {}
    for element in root.iter():
        ident = element.get("id")
        if ident and ident not in found:
            found[ident] = element
    return found


def _local_name(tag: Any) -> str:
    """네임스페이스가 붙은 태그(`{ns}rect`)를 로컬 이름으로 줄인다."""
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _tagged_group(ids: Mapping[str, ET.Element], ident: str) -> Optional[ET.Element]:
    """`id`가 붙은 요소가 실제 `<g>` 그룹일 때만 돌려준다(레이어 계약)."""
    element = ids.get(ident)
    if element is not None and _local_name(element.tag) == "g":
        return element
    return None


def _positive_rect(element: ET.Element) -> bool:
    try:
        return (
            float(element.get("width", "0")) > 0
            and float(element.get("height", "0")) > 0
        )
    except (TypeError, ValueError):
        return False


def _gate_entry_files(paths: Mapping[str, Path]) -> Dict[str, Any]:
    """산출물 3종이 실제로 있고 비어 있지 않은지(0바이트는 실패)."""
    problems: List[str] = []
    present: List[str] = []
    for label, path in paths.items():
        target = Path(path)
        if not target.is_file():
            problems.append(f"{label} 없음({target.name})")
            continue
        size = target.stat().st_size
        if size <= 0:
            problems.append(f"{label} 0바이트({target.name})")
            continue
        present.append(f"{label} {size:,}B")
    if problems:
        return _fail(
            "print.assets",
            "엔트리 산출물이 불완전하다 — " + ", ".join(problems),
        )
    return _ok("print.assets", " · ".join(present))


def _gate_entry_layers(svg: Path, kind: str) -> Dict[str, Any]:
    """전개도 레이어 계약(dieline/fold)을 SVG 구조로 확인한다."""
    required = LAYER_CONTRACT.get(kind)
    if required is None:
        return _fail(
            "print.layers",
            f"전개도 레이어 계약이 없는 형식이다 — {kind!r}를 LAYER_CONTRACT에 추가하라",
        )
    if not required:
        return _ok(
            "print.layers", f"{kind}: 이 형식은 필수 레이어가 없다(표에 사유 기록됨)"
        )
    try:
        ids = _svg_ids(svg)
    except Exception as exc:  # noqa: BLE001 — 깨진 XML은 게이트 실패
        return _fail(
            "print.layers", f"SVG를 해석할 수 없다 — 레이어 검사 불가: {_tail(exc)}"
        )
    missing = [name for name in required if _tagged_group(ids, name) is None]
    if missing:
        tags = ", ".join(f'<g id="{name}">' for name in missing)
        return _fail(
            "print.layers",
            f"전개도 레이어 누락 — {tags} 그룹이 필요하다. "
            "render_label.py·build_pouch_svg의 레이어 규약을 확인하라",
        )
    return _ok("print.layers", f"레이어 확인: {', '.join(required)}")


def _gate_entry_barcode(svg: Path, package: Mapping[str, Any]) -> Dict[str, Any]:
    """바코드가 있으면 그룹 안에 조용영역 rect가 있어야 한다."""
    digits = str(package.get("barcode") or "").strip()
    try:
        ids = _svg_ids(svg)
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "print.barcode", f"SVG를 해석할 수 없다 — 바코드 검사 불가: {_tail(exc)}"
        )
    group = _tagged_group(ids, "barcode")
    if group is None:
        if not digits:
            return _ok("print.barcode", "바코드 없는 사양 — 조용영역 검사 대상 아님")
        return _fail(
            "print.barcode",
            f'<g id="barcode"> 그룹이 없다 — {digits} 바코드와 조용영역을 넣어라',
        )
    quiet = [
        element
        for element in group.iter()
        if _local_name(element.tag) == "rect"
        and element.get("id") == "barcode-quiet"
        and _positive_rect(element)
    ]
    if not quiet:
        return _fail(
            "print.barcode",
            f'{digits or "기본"} 바코드에 조용영역이 없다 — <g id="barcode"> 안에 '
            'width·height가 양수인 <rect id="barcode-quiet">를 넣어라',
        )
    return _ok("print.barcode", f"{digits or '기본'} 바코드 조용영역 rect 확인")


def _gate_entry_render_size(png: Path, width: int, max_height: int) -> Dict[str, Any]:
    """렌더 PNG가 요청한 상한 안에 들어왔는지(0px도 실패)."""
    try:
        from PIL import Image

        with Image.open(png) as image:
            px_w, px_h = image.size
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "print.render_size",
            f"렌더 PNG를 읽을 수 없다 — 변환 결과를 확인하라: {_tail(exc)}",
        )
    if px_w <= 0 or px_h <= 0:
        return _fail("print.render_size", f"렌더 PNG 크기가 비었다 — {px_w}×{px_h}px")
    over: List[str] = []
    if px_w > int(width) + 1:
        over.append(f"가로 {px_w}px > 상한 {int(width)}px")
    if px_h > int(max_height) + 1:
        over.append(f"세로 {px_h}px > 상한 {int(max_height)}px")
    if over:
        return _fail(
            "print.render_size",
            "렌더 PNG가 상한을 넘었다 — "
            + ", ".join(over)
            + ". image_width·max_height를 올리거나 SVG 캔버스 크기를 확인하라",
        )
    return _ok(
        "print.render_size", f"{px_w}×{px_h}px ≤ 상한 {int(width)}×{int(max_height)}px"
    )


def run_entry_gates(
    *,
    svg: Path,
    png: Path,
    jpg: Path,
    package: Mapping[str, Any],
    width: int,
    max_height: int,
    allow: bool = False,
) -> List[Dict[str, Any]]:
    """엔트리 게이트 4종. 실패가 있으면 `PrintGateError`(allow면 skipped).

    순수 파이썬·SVG 구조 검사만 한다 — `gs`·`chromium`·`pdffonts`·
    `export_print.py`를 부르지 않으므로 vectorcraft-cli만 있는 컨테이너에서 돈다.
    """
    results = [
        _gate_entry_files({"svg": Path(svg), "png": Path(png), "jpg": Path(jpg)}),
        _gate_entry_layers(Path(svg), str(package.get("type") or "label")),
        _gate_entry_barcode(Path(svg), package),
        _gate_entry_render_size(Path(png), width, max_height),
    ]
    failures = [item for item in results if not item["ok"]]
    if allow:
        for item in failures:
            item["skipped"] = True
    elif failures:
        raise PrintGateError(failures)
    return results


# ---------------------------------------------------------------------------
# 목업 (Pillow, 결정론 — 난수·시각 없음)
# ---------------------------------------------------------------------------


def _flat_label_mock(page: Path, out: Path, package: Mapping[str, Any]) -> None:
    """평면 라벨: 종이 위에 전면 패널을 얹고 브랜드 캡션을 단다."""
    from PIL import Image, ImageDraw

    canvas = Image.new("RGB", (1400, 900), (239, 237, 232))
    draw = ImageDraw.Draw(canvas)
    frame = (170, 120, 1230, 720)
    draw.rounded_rectangle(
        frame, radius=10, fill=(255, 255, 255), outline=(219, 214, 205), width=2
    )

    with Image.open(page) as image:
        fitted = image.convert("RGB")
        ratio = min((frame[2] - frame[0]) - 80, (frame[3] - frame[1]) - 80) / max(
            fitted.width, fitted.height
        )
        size = (max(1, int(fitted.width * ratio)), max(1, int(fitted.height * ratio)))
        fitted = fitted.resize(size, Image.LANCZOS)
        x = frame[0] + ((frame[2] - frame[0]) - size[0]) // 2
        y = frame[1] + ((frame[3] - frame[1]) - size[1]) // 2
        canvas.paste(fitted, (x, y))

    caption = f"{package.get('brand', '')} · {package.get('product', '')}"
    draw.text((170, 770), caption[:80], fill=(60, 58, 54))
    canvas.save(out, "PNG", optimize=True)


def _pouch_mock(page: Path, out: Path, package: Mapping[str, Any]) -> None:
    """파우치 전면: 상단 실링 바 + 몸체 실루엣에 전면 패널을 앉힌다."""
    from PIL import Image, ImageDraw

    colors = package.get("colors") or DEFAULT_COLORS
    canvas = Image.new("RGB", (1400, 900), (232, 229, 222))
    draw = ImageDraw.Draw(canvas)
    body = [(420, 150), (980, 150), (1010, 820), (390, 820)]
    draw.polygon(body, fill=(250, 249, 245), outline=(206, 201, 192))
    draw.rectangle(
        (420, 150, 980, 196), fill=colors.get("calm", DEFAULT_COLORS["calm"])
    )

    with Image.open(page) as image:
        fitted = image.convert("RGB")
        inner = (450, 230, 950, 780)
        ratio = min(inner[2] - inner[0], inner[3] - inner[1]) / max(
            fitted.width, fitted.height
        )
        size = (max(1, int(fitted.width * ratio)), max(1, int(fitted.height * ratio)))
        fitted = fitted.resize(size, Image.LANCZOS)
        x = inner[0] + ((inner[2] - inner[0]) - size[0]) // 2
        y = inner[1] + ((inner[3] - inner[1]) - size[1]) // 2
        canvas.paste(fitted, (x, y))

    draw.text((420, 840), str(package.get("brand", ""))[:60], fill=(60, 58, 54))
    canvas.save(out, "PNG", optimize=True)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class PrintProfile:
    """라벨·파우치 인쇄물. 게이트를 통과한 PDF만 팩에 넣는다."""

    id = "print"

    def defaults(self) -> Dict[str, Any]:
        return dict(DEFAULT_SPEC)

    def validate(self, spec: Mapping[str, Any]) -> None:
        """spec_node에서 knowledge·LLM 전에 package 계약을 확인한다."""
        package_spec(spec)

    def plan(
        self,
        ir: Mapping[str, Any],
        spec: Mapping[str, Any],
        styles: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        package = package_spec(spec)
        width, height = package["size_mm"]
        variant_id = f"{package['type']}-{_slug(package['brand'])}-{_number(width)}x{_number(height)}"
        return [
            {
                "id": variant_id,
                "kind": package["type"],
                "direction": str(package["brand"]),
                "style": "print",
                "theme": "print",
                "size_mm": [width, height],
                "package": package,
            }
        ]

    def render(
        self, variant: Mapping[str, Any], ctx: base.ProfileContext
    ) -> Optional[Dict[str, Any]]:
        run_dir = Path(ctx.run_dir or ctx.out_root)
        out = run_dir / variant["id"]
        out.mkdir(parents=True, exist_ok=True)
        package = dict(variant["package"])

        spec_path = out / f"{variant['id']}.spec.json"
        spec_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        if variant.get("kind") == "pouch":
            svg_text, manifest_data = build_pouch_svg(package)
            svg = out / f"{variant['id']}.svg"
            svg.write_text(svg_text, encoding="utf-8")
            manifest_path = out / f"{variant['id']}.manifest.json"
            manifest_path.write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
        else:
            render_spec = out / f"{variant['id']}.render-spec.json"
            render_spec.write_text(
                json.dumps(
                    _xml_package(package), ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n",
                encoding="utf-8",
            )
            ctx.run(
                [
                    sys.executable,
                    ctx.assets_dir / "scripts/render_label.py",
                    "--spec",
                    render_spec,
                    "--out",
                    out,
                    "--name",
                    variant["id"],
                ]
            )
            svg = out / f"{variant['id']}.svg"
            manifest_path = out / f"{variant['id']}.manifest.json"
            # render_label.py는 mark를 모른다 — art 그룹 끝에 넣고, 출처 텍스트가
            # 마크와 겹치지 않게 위치를 다시 잡은 뒤 해시를 갱신한다.
            if svg.is_file():
                original = svg.read_text(encoding="utf-8")
                width_px = float(package["size_mm"][0]) * MM
                updated = _inject_mark(original, package, width_px)
                updated = _relayout_origin(updated, package, width_px)
                if updated != original:
                    svg.write_text(updated, encoding="utf-8")
                    if manifest_path.is_file():
                        data = json.loads(manifest_path.read_text(encoding="utf-8"))
                        data["sha256"] = hashlib.sha256(updated.encode()).hexdigest()
                        if package.get("mark_path"):
                            data["mark"] = True
                        manifest_path.write_text(
                            json.dumps(
                                data, ensure_ascii=False, indent=2, sort_keys=True
                            )
                            + "\n",
                            encoding="utf-8",
                        )

        if not svg.is_file():
            raise base.ProfileError(
                f"print profile: 렌더 결과 SVG가 없다 — {svg}. "
                "render_label.py 출력 경로와 --name을 확인하라"
            )
        return {
            **variant,
            "svg": str(svg),
            "manifest": str(manifest_path) if manifest_path.is_file() else None,
            "spec": str(spec_path),
        }

    def gates(self) -> List[base.Gate]:
        """두 단계가 요구할 수 있는 게이트 전체(설명용).

        실행되는 게이트는 `pack`이 `spec["print_ready"]`로 고른다 — entry는
        앞 4종(순수 파이썬), handoff는 뒤 4종(PDF/X·CMYK·폰트). 이 목록은
        "이 profile은 자체 게이트를 갖는다"는 판정(critic/gate 노드)에 쓰인다.
        """
        return list(GATES)

    def _export_pdf(
        self,
        ctx: base.ProfileContext,
        svg: Path,
        size_mm: Sequence[float],
        bleed: float,
        out_dir: Path,
    ) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx.run(
            [
                sys.executable,
                ctx.assets_dir / "scripts/export_print.py",
                "--src",
                svg,
                "--trim",
                f"{_number(size_mm[0])}x{_number(size_mm[1])}",
                "--bleed",
                _number(bleed),
                "--out",
                out_dir,
            ]
        )
        return out_dir / f"{Path(svg).stem}.pdf"

    def pack(
        self,
        items: Sequence[Mapping[str, Any]],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> Dict[str, Any]:
        ctx = _context(state)
        package = package_spec(spec)
        if not items:
            raise base.ProfileError(
                "print profile: 선택된 항목이 없다 — selection이 비었거나 렌더가 사라졌다"
            )
        selection = list(state.get("selection") or [])
        if selection and len(items) < len(selection):
            raise base.ProfileError(
                f"print profile: selection {len(selection)}건 중 {len(items)}건만 렌더가 있다 — "
                "렌더를 다시 만든 뒤 pack을 실행하라"
            )
        run_dir = Path(ctx.run_dir or ctx.out_root)
        pack_dir = run_dir / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)

        # 프리셋 해석: spec 값 → platform 프리셋 → generic. spec이 항상 이긴다.
        preset, preset_warnings = profile_presets.resolve_for_spec(spec)
        width = int(spec.get("image_width") or preset.image_width)
        max_height = int(spec.get("max_height") or preset.max_height)
        quality = int(spec.get("jpg_quality") or preset.jpg_quality)
        naming = str(spec.get("naming") or preset.naming)
        bleed = float(spec.get("bleed_mm", BLEED_MM))
        # 테스트 우회는 명시적 bool true만 — 문자열 "false"가 게이트를 끄지 않게 한다.
        allow = spec.get("allow_print_checks") is True
        # handoff는 명시적 bool true만 — 기본은 엔트리(표현물) 단계다.
        handoff = spec.get("print_ready") is True
        # 표현물 PNG·JPG에는 전개도·접힘선을 숨긴다. 명시적 bool true만 가이드를 켠다.
        guides = spec.get("guides") is True

        packed: List[Dict[str, Any]] = []
        gates: List[Dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            variant_id = item["id"]
            stem = profile_presets.filename(naming, variant_id, index)
            svg = Path(item["svg"])
            size_mm = item.get("size_mm") or package["size_mm"]
            pdf: Optional[Path] = None
            item_gates: List[Dict[str, Any]] = []
            if handoff:
                # 인쇄소 핸드오프: PDF/X 변환 + 도련·폰트·잉크 게이트.
                print_dir = pack_dir / f"{stem}-print"
                try:
                    pdf = self._export_pdf(ctx, svg, size_mm, bleed, print_dir)
                except Exception as exc:  # noqa: BLE001
                    failure = _fail(
                        "print.export",
                        "export_print 실행 실패 — chromium·ghostscript 설치, SVG 비율, "
                        f"출력 경로 접근성(snap chromium은 /tmp PrivateTmp를 못 읽는다)을 확인하라: "
                        f"{_tail(exc)}",
                    )
                    if not allow:
                        raise PrintGateError([failure]) from exc
                    failure["skipped"] = True
                    gates.append(failure)
                    pdf = print_dir / f"{stem}.pdf"

                if pdf is not None and pdf.is_file() and pdf.name != f"{stem}.pdf":
                    renamed = pdf.with_name(f"{stem}.pdf")
                    pdf.replace(renamed)
                    pdf = renamed

                item_gates = run_gates(
                    ctx, svg=svg, pdf=pdf, size_mm=size_mm, bleed=bleed, allow=allow
                )

            png = pack_dir / f"{stem}-front.png"
            jpg = pack_dir / f"{stem}.jpg"
            # 원본 SVG는 dieline·fold를 유지한다. 변환용 임시 사본에서만 숨긴다.
            presentation = svg
            presentation_copy: Optional[Path] = None
            if not guides:
                original = svg.read_text(encoding="utf-8")
                hidden = _hide_guide_layers(original)
                if hidden != original:
                    presentation_copy = pack_dir / f".{stem}.presentation.svg"
                    presentation_copy.write_text(hidden, encoding="utf-8")
                    presentation = presentation_copy
            try:
                ctx.convert(presentation, png, width, max_height)
                ctx.jpeg(png, jpg, quality)
            finally:
                if presentation_copy is not None:
                    presentation_copy.unlink(missing_ok=True)

            if not handoff:
                # 엔트리: PNG·JPG가 나온 뒤 구조 검사만 한다(외부 프로세스 없음).
                item_gates = run_entry_gates(
                    svg=svg,
                    png=png,
                    jpg=jpg,
                    package=package,
                    width=width,
                    max_height=max_height,
                    allow=allow,
                )
            gates.extend(item_gates)

            row: Dict[str, Any] = {
                "id": variant_id,
                "kind": item.get("kind", package["type"]),
                "brand": package["brand"],
                "product": package["product"],
                "size_mm": list(size_mm),
                "svg": str(svg),
                "spec": item.get("spec"),
                "png": str(png),
                "jpg": str(jpg),
                # allow 우회(테스트)에서 파일이 없어도 meta를 쓸 수 있게 0으로 둔다.
                "png_size": png.stat().st_size if png.is_file() else 0,
                "jpg_size": jpg.stat().st_size if jpg.is_file() else 0,
                "sha256": core_artifacts.sha256_file(png) if png.is_file() else None,
                # W8(preflight) 보고 — 렌더가 넘긴 값이 없으면 빈 목록/사전이다.
                "skipped_assets": list(item.get("skipped_assets") or []),
                "preflight": dict(item.get("preflight") or {}),
                "gates": item_gates,
            }
            if pdf is not None:
                row["pdf"] = str(pdf)
                row["pdf_size"] = pdf.stat().st_size if pdf.is_file() else 0
            packed.append(row)

        skipped_assets: List[Any] = []
        preflight: Dict[str, Any] = {}
        for row in packed:
            skipped_assets.extend(row.get("skipped_assets") or [])
            if row.get("preflight"):
                preflight[row["id"]] = row["preflight"]

        meta = {
            "profile": "print",
            "mode": "handoff" if handoff else "entry",
            "print_ready": handoff,
            "platform": spec.get("platform"),
            "preset": preset.id,
            "preset_warnings": preset_warnings,
            "naming": naming,
            "guides": guides,
            "package_type": package["type"],
            "inputs_sha256": core_artifacts.sha256_json(package),
            "brand": package["brand"],
            "product": package["product"],
            "size_mm": package["size_mm"],
            "bleed_mm": bleed if handoff else None,
            "image_width": width,
            "max_height": max_height,
            "jpg_quality": quality,
            "allow_print_checks": allow,
            "skipped_assets": skipped_assets,
            "preflight": preflight,
            "gates_ok": all(item["ok"] for item in gates),
            "gates": gates,
            "items": packed,
        }
        (pack_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        lines = [f"# 인쇄 팩 — {package['brand']} {package['product']}", ""]
        lines += [
            f"- 종류: {package['type']}",
            f"- 재단: {package['size_mm'][0]}×{package['size_mm'][1]}mm"
            + (f" · 도련 {bleed}mm" if handoff else " (도련 미적용 — 제출용 표현물)"),
            f"- 단계: {meta['mode']}"
            + (
                " (인쇄소 핸드오프: PDF/X·CMYK·폰트 임베드)"
                if handoff
                else " (콘테스트 제출: 표현물만)"
            ),
            f"- 입력 지문: `{meta['inputs_sha256'][:16]}`",
        ]
        if skipped_assets:
            labels = []
            for entry in skipped_assets:
                if isinstance(entry, Mapping):
                    labels.append(
                        str(entry.get("path") or entry.get("reason") or entry)
                    )
                else:
                    labels.append(str(entry))
            head = ", ".join(labels[:5])
            more = f" 외 {len(labels) - 5}건" if len(labels) > 5 else ""
            lines.append(f"- 경고: 건너뛴 자산 {len(labels)}건 — {head}{more}")
        lines += ["", "## 게이트"]
        for gate in gates:
            mark = "✓" if gate["ok"] else ("−" if gate.get("skipped") else "✗")
            lines.append(f"- {mark} {gate['id']}: {gate['detail']}")
        lines += ["", "## 산출물"]
        for item in packed:
            lines += [
                f"### {item['id']}",
                f"- SVG: `{Path(item['svg']).name}`",
            ]
            if item.get("pdf"):
                lines.append(
                    f"- PDF: `{Path(item['pdf']).name}` ({item['pdf_size']:,}B)"
                )
            lines += [
                f"- PNG: `{Path(item['png']).name}` ({item['png_size']:,}B)",
                f"- JPG: `{Path(item['jpg']).name}` ({item['jpg_size']:,}B)",
                "",
            ]
        (pack_dir / "entry.md").write_text("\n".join(lines), encoding="utf-8")

        artifact_paths: List[Any] = []
        for item in packed:
            artifact_paths += [item["svg"], item["png"], item["jpg"]]
            if item.get("pdf"):
                artifact_paths.append(item["pdf"])
            if item.get("spec"):
                artifact_paths.append(item["spec"])
        artifact_paths += [pack_dir / "meta.json", pack_dir / "entry.md"]
        gates_ok = all(gate["ok"] for gate in gates)
        manifest = base.run_manifest(
            {**state, "spec": spec},
            run_dir,
            base.collect_artifacts(artifact_paths, run_dir),
            kind="print",
            status="ok" if gates_ok else "warn",
            gates=gates,
        )
        # base.run_manifest는 platform만 남긴다. 프리셋·W8 보고를 해시 기록에도 남긴다.
        manifest.setdefault("meta", {})
        manifest["meta"].update(
            {
                "preset": preset.id,
                "preset_warnings": preset_warnings,
                "naming": naming,
                "guides": guides,
                "skipped_assets": skipped_assets,
                "preflight": preflight,
            }
        )
        manifest_path = core_artifacts.write_manifest(run_dir, manifest)

        return {
            "dir": str(pack_dir),
            "items": packed,
            "meta": str(pack_dir / "meta.json"),
            "manifest": str(manifest_path),
            "manifest_digest": core_artifacts.manifest_digest(manifest),
            "gates": gates,
        }

    def mockups(
        self, items: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        if not items:
            return []
        package = package_spec(spec)
        mock_dir = Path(
            spec.get("mockup_dir") or Path(items[0]["png"]).parent / "mockups"
        )
        mock_dir.mkdir(parents=True, exist_ok=True)

        rows: List[Dict[str, Any]] = []
        for item in items:
            flat = mock_dir / f"{item['id']}-flat.png"
            _flat_label_mock(Path(item["png"]), flat, package)
            pouch = mock_dir / f"{item['id']}-pouch.png"
            _pouch_mock(Path(item["png"]), pouch, package)
            rows.append(
                {
                    "id": item["id"],
                    "flat": str(flat),
                    "pouch": str(pouch),
                    "flat_sha256": core_artifacts.sha256_file(flat),
                    "pouch_sha256": core_artifacts.sha256_file(pouch),
                }
            )
        return rows
