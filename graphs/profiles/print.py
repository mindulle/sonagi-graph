"""print profile — 라벨·파우치 전개도 → 인쇄 PDF 게이트 → 납품 팩 (GRAPH-17).

`spec["package"]` 하나로 인쇄물 한 종을 만든다. 라벨은 sonagi-assets의
`scripts/render_label.py`를 그대로 쓰고, 파우치는 같은 레이어 규약
(`dieline`·`fold`·`art`·`barcode`)의 최소 전면 템플릿을 직접 만든다.

게이트는 **실패 시 멈춘다**(fail closed). 통과하지 못한 PDF는 팩에 들어가지 않는다.
테스트에서만 `spec["allow_print_checks"] = True`로 건너뛸 수 있다.

spec 예::

    {
      "profile": "print",
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

게이트:
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
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from graphs.core import artifacts as core_artifacts
from graphs.core import env as core_env
from graphs.core import exec as core_exec
from graphs.profiles import base

MM = 96 / 25.4  # 1mm = 3.7795px
PACKAGE_TYPES = ("label", "pouch")
BLEED_MM = 3.0

DEFAULT_SPEC: Dict[str, Any] = {
    "image_width": 2000,
    "max_height": 3000,
    "jpg_quality": 90,
    "bleed_mm": BLEED_MM,
    "allow_print_checks": False,
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

GATES: Tuple[base.Gate, ...] = (
    base.Gate(
        "print.export_check",
        "export_print.py --check: 재단/도련 3mm·안전여백·레이어 분리",
    ),
    base.Gate("print.pdf", "출력 PDF가 존재하고 0바이트보다 크다"),
    base.Gate("print.fonts", "pdffonts: 임베드되지 않은 폰트가 없다"),
    base.Gate("print.inkcov", "gs -sDEVICE=inkcov: CMYK 색공간"),
)


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


def wrap_text(text: str, size_px: float, max_width: float) -> List[str]:
    """CJK 1em · 라틴 0.55em 근사로 줄바꿈."""
    lines: List[str] = []
    current = ""
    width = 0.0
    for char in text:
        char_w = size_px if ord(char) >= 0x2E80 else size_px * 0.55
        if width + char_w > max_width and current:
            lines.append(current)
            current, width = "", 0.0
        current += char
        width += char_w
    if current:
        lines.append(current)
    return lines


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


def _mark_tag(package: Mapping[str, Any], width_px: float, pad: float) -> str:
    """mark_path(SVG)를 base64 이미지 태그로 만든다(없으면 빈 문자열)."""
    mark = package.get("mark_path")
    if not mark:
        return ""
    data = base64.b64encode(Path(str(mark)).read_bytes()).decode("ascii")
    size = 14 * MM
    x = width_px - pad - size
    y = pad + 6
    return (
        f'<image id="mark" x="{x:.1f}" y="{y:.1f}" width="{size:.1f}" height="{size:.1f}" '
        f'href="data:image/svg+xml;base64,{data}"/>'
    )


def _inject_mark(svg_text: str, package: Mapping[str, Any], width_px: float) -> str:
    """스크립트가 만든 SVG의 art 그룹 끝에 mark를 넣는다(라벨 경로)."""
    tag = _mark_tag(package, width_px, 6 * MM)
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
        f'<text x="{width - pad:.1f}" y="{pad + 54:.1f}" font-family="monospace" font-size="8" '
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
    """게이트 4종. 실패가 있으면 `PrintGateError`(allow면 skipped로 남긴다)."""
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
            # render_label.py는 mark를 모른다 — art 그룹 끝에 직접 넣고 해시를 갱신한다.
            if package.get("mark_path") and svg.is_file():
                original = svg.read_text(encoding="utf-8")
                updated = _inject_mark(
                    original, package, float(package["size_mm"][0]) * MM
                )
                if updated != original:
                    svg.write_text(updated, encoding="utf-8")
                    if manifest_path.is_file():
                        data = json.loads(manifest_path.read_text(encoding="utf-8"))
                        data["sha256"] = hashlib.sha256(updated.encode()).hexdigest()
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

        width = int(spec.get("image_width", 2000))
        max_height = int(spec.get("max_height", 3000))
        quality = int(spec.get("jpg_quality", 90))
        bleed = float(spec.get("bleed_mm", BLEED_MM))
        # 테스트 우회는 명시적 bool true만 — 문자열 "false"가 게이트를 끄지 않게 한다.
        allow = spec.get("allow_print_checks") is True

        packed: List[Dict[str, Any]] = []
        gates: List[Dict[str, Any]] = []
        for item in items:
            variant_id = item["id"]
            svg = Path(item["svg"])
            size_mm = item.get("size_mm") or package["size_mm"]
            print_dir = pack_dir / f"{variant_id}-print"
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
                pdf = print_dir / f"{Path(svg).stem}.pdf"

            item_gates = run_gates(
                ctx, svg=svg, pdf=pdf, size_mm=size_mm, bleed=bleed, allow=allow
            )
            gates.extend(item_gates)

            png = pack_dir / f"{variant_id}-front.png"
            jpg = pack_dir / f"{variant_id}.jpg"
            ctx.convert(svg, png, width, max_height)
            ctx.jpeg(png, jpg, quality)

            packed.append(
                {
                    "id": variant_id,
                    "kind": item.get("kind", package["type"]),
                    "brand": package["brand"],
                    "product": package["product"],
                    "size_mm": list(size_mm),
                    "svg": str(svg),
                    "spec": item.get("spec"),
                    "pdf": str(pdf),
                    "png": str(png),
                    "jpg": str(jpg),
                    "png_size": png.stat().st_size,
                    "jpg_size": jpg.stat().st_size,
                    "pdf_size": pdf.stat().st_size if pdf.is_file() else 0,
                    "sha256": core_artifacts.sha256_file(png),
                    "gates": item_gates,
                }
            )

        meta = {
            "profile": "print",
            "package_type": package["type"],
            "inputs_sha256": core_artifacts.sha256_json(package),
            "brand": package["brand"],
            "product": package["product"],
            "size_mm": package["size_mm"],
            "bleed_mm": bleed,
            "image_width": width,
            "allow_print_checks": allow,
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
            f"- 재단: {package['size_mm'][0]}×{package['size_mm'][1]}mm · 도련 {bleed}mm",
            f"- 입력 지문: `{meta['inputs_sha256'][:16]}`",
            "",
            "## 게이트",
        ]
        for gate in gates:
            mark = "✓" if gate["ok"] else ("−" if gate.get("skipped") else "✗")
            lines.append(f"- {mark} {gate['id']}: {gate['detail']}")
        lines += ["", "## 산출물"]
        for item in packed:
            lines += [
                f"### {item['id']}",
                f"- SVG: `{Path(item['svg']).name}`",
                f"- PDF: `{Path(item['pdf']).name}` ({item['pdf_size']:,}B)",
                f"- PNG: `{Path(item['png']).name}` ({item['png_size']:,}B)",
                f"- JPG: `{Path(item['jpg']).name}` ({item['jpg_size']:,}B)",
                "",
            ]
        (pack_dir / "entry.md").write_text("\n".join(lines), encoding="utf-8")

        artifact_paths: List[Any] = []
        for item in packed:
            artifact_paths += [item["svg"], item["pdf"], item["png"], item["jpg"]]
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
