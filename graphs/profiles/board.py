"""엔트리 보드 — 1920×1080 제출용 요약 이미지 (GRAPH-25).

선택된 엔트리 하나를 크게 보여 주고 방향·스타일·설명을 함께 적는다. 심사자가
파일을 열기 전에 맥락을 잡을 수 있게 하는 표지다.

Pillow만 쓴다. 위치·색·글꼴 크기는 상수이고 시각·난수·타임스탬프를 쓰지 않으므로
같은 입력이면 같은 보드가 나온다. 글꼴을 하나도 읽지 못하면 Pillow 기본 글꼴로
떨어지고 그 사실(`font_fallback`)을 결과에 남긴다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

BOARD_SIZE: Tuple[int, int] = (1920, 1080)

PAPER = (247, 245, 240)
INK = (26, 25, 23)
MUTED = (112, 108, 101)
ACCENT = (180, 85, 31)
RULE = (214, 209, 200)
PANEL = (255, 255, 255)

#: 로컬 마운트 → 배포 이미지 순. 앞에서부터 읽히는 첫 글꼴을 쓴다.
REGULAR_FONTS: Tuple[str, ...] = (
    "/usr/share/fonts/local/NanumSquareR.ttf",
    "/usr/share/fonts/truetype/nanum/NanumSquareR.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
BOLD_FONTS: Tuple[str, ...] = (
    "/usr/share/fonts/local/NanumSquareB.ttf",
    "/usr/share/fonts/truetype/nanum/NanumSquareB.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def _load_font(size: int, *, bold: bool = False):
    """글꼴 하나를 읽는다. 전부 실패하면 (기본 글꼴, None)을 돌려준다."""
    from PIL import ImageFont

    for path in BOLD_FONTS if bold else REGULAR_FONTS:
        if not Path(path).is_file():
            continue
        try:
            return ImageFont.truetype(path, size=size), str(path)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size), None
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default(), None


def _line_height(font) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def _wrap(draw, text: str, font, max_width: float) -> List[str]:
    """단어 우선, 한 단어가 폭을 넘으면 글자 단위로 쪼갠다."""
    lines: List[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        words = paragraph.split(" ")
        current = ""
        for word in words:
            trial = word if not current else f"{current} {word}"
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
                continue
            if current:
                lines.append(current)
                current = ""
            # 한 단어가 통째로 넘치면 글자 단위로 자른다.
            if draw.textlength(word, font=font) <= max_width:
                current = word
                continue
            for char in word:
                trial = current + char
                if current and draw.textlength(trial, font=font) > max_width:
                    lines.append(current)
                    current = char
                else:
                    current = trial
        lines.append(current)
    return lines


def _fit_lines(draw, text: str, font, max_width: float, max_lines: int) -> List[str]:
    """줄 수 상한을 넘으면 마지막 줄을 말줄임표로 자른다."""
    lines = _wrap(draw, text, font, max_width)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    while last and draw.textlength(f"{last}…", font=font) > max_width:
        last = last[:-1]
    kept[-1] = f"{last.rstrip()}…" if last else "…"
    return kept


def _draw_lines(
    draw,
    xy: Tuple[int, int],
    lines: Sequence[str],
    font,
    fill: Tuple[int, int, int],
    *,
    leading: float = 1.18,
) -> int:
    """줄 목록을 그리고 다음 y를 돌려준다."""
    x, y = xy
    step = int(round(_line_height(font) * leading))
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += step
    return y


def _flatten(image_path: Path):
    """알파가 있으면 흰 배경에 합성한다(JPG·보드에서 검게 나오지 않게)."""
    from PIL import Image

    with Image.open(image_path) as source:
        image = (
            source.convert("RGBA")
            if source.mode in ("RGBA", "LA", "P")
            else source.convert("RGB")
        )
        if image.mode == "RGBA":
            base = Image.new("RGB", image.size, (255, 255, 255))
            base.paste(image, mask=image.split()[-1])
            image = base
        return image.copy()


def compose(
    *,
    image: Path,
    out: Path,
    platform: str = "generic",
    direction: str = "",
    style: str = "",
    theme: str = "",
    description: str = "",
    entry_id: str = "",
) -> Dict[str, Any]:
    """보드 하나를 `out`에 쓰고 메타를 돌려준다(고정 레이아웃)."""
    from PIL import Image, ImageDraw

    width, height = BOARD_SIZE
    canvas = Image.new("RGB", BOARD_SIZE, PAPER)
    draw = ImageDraw.Draw(canvas)

    title_font, title_path = _load_font(62, bold=True)
    label_font, label_path = _load_font(22, bold=True)
    style_font, style_path = _load_font(30)
    body_font, body_path = _load_font(29)
    meta_font, meta_path = _load_font(22)

    # 헤더 — 플랫폼 라벨 + 방향 제목.
    draw.text(
        (96, 70), str(platform or "generic").upper(), font=label_font, fill=ACCENT
    )
    title = str(direction or "기본 방향")
    title_lines = _fit_lines(draw, title, title_font, 1728, 2)
    _draw_lines(draw, (96, 112), title_lines, title_font, INK)
    draw.rectangle((96, 276, 1824, 278), fill=RULE)

    # 왼쪽 — 선택된 엔트리 이미지를 패널 안에 맞춘다.
    panel = (96, 316, 1196, 984)
    draw.rectangle(panel, fill=PANEL, outline=RULE, width=2)
    inner = (panel[0] + 28, panel[1] + 28, panel[2] - 28, panel[3] - 28)
    photo = _flatten(Path(image))
    ratio = min(
        (inner[2] - inner[0]) / max(1, photo.width),
        (inner[3] - inner[1]) / max(1, photo.height),
    )
    size = (
        max(1, int(photo.width * ratio)),
        max(1, int(photo.height * ratio)),
    )
    fitted = photo.resize(size, Image.LANCZOS)
    x = inner[0] + ((inner[2] - inner[0]) - size[0]) // 2
    y = inner[1] + ((inner[3] - inner[1]) - size[1]) // 2
    canvas.paste(fitted, (x, y))

    # 오른쪽 — 스타일·테마, 엔트리 id, 설명.
    column_x = 1250
    draw.text((column_x, 316), "STYLE / THEME", font=label_font, fill=MUTED)
    style_text = str(style or "—")
    if theme:
        style_text = f"{style_text} ({theme})"
    _draw_lines(
        draw,
        (column_x, 352),
        _fit_lines(draw, style_text, style_font, 574, 2),
        style_font,
        INK,
    )

    draw.text((column_x, 452), "ENTRY", font=label_font, fill=MUTED)
    _draw_lines(
        draw,
        (column_x, 488),
        _fit_lines(draw, str(entry_id or "—"), meta_font, 574, 3),
        meta_font,
        INK,
    )

    draw.text((column_x, 596), "DESCRIPTION", font=label_font, fill=MUTED)
    _draw_lines(
        draw,
        (column_x, 632),
        _fit_lines(draw, str(description or "설명 없음"), body_font, 574, 9),
        body_font,
        INK,
        leading=1.42,
    )

    # 푸터 — 보드임을 밝히는 고정 문구. 시각·난수를 쓰지 않는다.
    draw.rectangle((96, 1020, 1824, 1022), fill=RULE)
    draw.text(
        (96, 1036),
        f"{str(platform or 'generic').upper()} · ENTRY BOARD",
        font=meta_font,
        fill=MUTED,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG", optimize=True)

    paths = (title_path, label_path, style_path, body_path, meta_path)
    fallback = any(path is None for path in paths)
    primary = next((Path(path).name for path in paths if path), "pillow-default")
    return {
        "path": str(out),
        "width": width,
        "height": height,
        "bytes": out.stat().st_size,
        "image": str(image),
        "platform": str(platform or "generic"),
        "direction": direction,
        "style": style,
        "theme": theme,
        "font": primary,
        "font_fallback": fallback,
    }


__all__ = ["BOARD_SIZE", "compose"]
