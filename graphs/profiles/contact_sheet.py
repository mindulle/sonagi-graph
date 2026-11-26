"""콘택트 시트 — 한 실행의 렌더를 한 장에 모아 비교한다 (GRAPH-32).

렌더가 수십~백 개로 늘어나면 옵션 id를 읽는 것만으로는 차이를 볼 수 없다.
이 모듈은 PNG 썸네일을 6열 격자로 붙여 **눈으로 비교하는 한 장**을 만든다.

- 셀 300×420, 6열, 자동 행. 24개는 2032px, 61개는 5140px 높이.
- 각 셀 위에 1px 괘선, 아래에 id와 style/theme 라벨(넘치면 말줄임표).
- 상단 제목 블록에 run_id·seed·브리프 첫 줄·개수를 적는다.
- 시각·난수·타임스탬프를 쓰지 않는다. 같은 입력이면 같은 바이트가 나온다.
- 글꼴을 하나도 읽지 못하면 Pillow 기본 글꼴로 떨어지고(`font_fallback`)
  그 사실을 결과에 남긴다. board와 같은 글꼴 후보·헬퍼를 재사용한다.

사용::

    from graphs.profiles import contact_sheet

    row = contact_sheet.compose(
        [{"id": "style-1-desktop-light", "png": "/run/thumbs/a.png",
          "style": "style-1", "theme": "light"}],
        out=Path("/run/contact-sheet.png"),
        run_id="entry-abcd1234",
        seed="abcd1234",
        brief="비 오는 날의 차",
    )
    row["path"], row["width"], row["height"], row["rows"]
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from graphs.profiles import board as board_module
from graphs.profiles import diversity as diversity_module

#: 고정 격자. 6열이면 디스코드 미리보기에서도 한눈에 비교하기 좋다.
COLUMNS = 6
CELL_WIDTH = 300
CELL_HEIGHT = 420
MARGIN = 48
GAP = 24
#: 제목 블록(제목·메타·브리프·괘선)이 끝나고 격자가 시작하는 y.
GRID_TOP = 232
THUMB_PADDING = 14
LABEL_HEIGHT = 88

#: 라벨 글자 수 상한(글꼴이 달라도 넘치지 않게 먼저 자른다).
ID_LABEL_LIMIT = 46
META_LABEL_LIMIT = 34
BRIEF_LABEL_LIMIT = 120

ELLIPSIS = "…"

#: board와 같은 색을 쓴다. 새 팔레트를 만들지 않는다.
PAPER = board_module.PAPER
INK = board_module.INK
MUTED = board_module.MUTED
ACCENT = board_module.ACCENT
RULE = board_module.RULE
PANEL = board_module.PANEL


def truncate_text(text: Any, limit: int) -> str:
    """한 줄 문자열을 `limit`자 이하로 자른다. 넘치면 말줄임표를 붙인다.

    줄바꿈·연속 공백은 한 칸으로 접는다(라벨은 한 줄이다). 자른 결과의
    길이는 항상 `limit` 이하다.
    """
    collapsed = " ".join(str(text or "").split())
    limit = max(0, int(limit))
    if limit == 0:
        return ""
    if len(collapsed) <= limit:
        return collapsed
    if limit == 1:
        return ELLIPSIS
    return collapsed[: limit - 1].rstrip() + ELLIPSIS


def sheet_size(count: int, columns: int = COLUMNS) -> Tuple[int, int]:
    """항목 수 → (폭, 높이). 항목이 없으면 제목 블록만 남는다."""
    columns = max(1, int(columns))
    count = max(0, int(count))
    rows = math.ceil(count / columns) if count else 0
    width = MARGIN * 2 + columns * CELL_WIDTH + max(0, columns - 1) * GAP
    height = GRID_TOP + rows * CELL_HEIGHT + max(0, rows - 1) * GAP + MARGIN
    return width, height


def _ellipsize_width(draw, text: str, font, max_width: float) -> str:
    """글꼴 폭 기준으로 한 번 더 자른다(한글·영문 혼용 대비)."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    trimmed = text
    while trimmed and draw.textlength(f"{trimmed}{ELLIPSIS}", font=font) > max_width:
        trimmed = trimmed[:-1]
    return f"{trimmed.rstrip()}{ELLIPSIS}" if trimmed else ELLIPSIS


def _draw_label(
    draw,
    *,
    x: int,
    y: int,
    width: int,
    item: Mapping[str, Any],
    id_font,
    meta_font,
) -> Dict[str, str]:
    """셀 아래 두 줄 라벨을 그리고 실제로 그린 문자열을 돌려준다."""
    entry_id = truncate_text(item.get("id") or "—", ID_LABEL_LIMIT)
    style = truncate_text(item.get("style") or "—", 24)
    theme = truncate_text(item.get("theme") or "", 12)
    meta = f"{style} ({theme})" if theme else style
    meta = truncate_text(meta, META_LABEL_LIMIT)

    id_text = _ellipsize_width(draw, entry_id, id_font, width)
    meta_text = _ellipsize_width(draw, meta, meta_font, width)

    draw.text((x, y), id_text, font=id_font, fill=INK)
    id_height = board_module._line_height(id_font)
    draw.text((x, y + id_height + 6), meta_text, font=meta_font, fill=MUTED)
    return {"id": id_text, "meta": meta_text}


def _draw_thumb(
    canvas,
    draw,
    *,
    item: Mapping[str, Any],
    box: Tuple[int, int, int, int],
    empty_font,
) -> bool:
    """셀 안쪽에 PNG를 비율 유지로 맞춘다. 파일이 없으면 빈 패널을 남긴다."""
    from PIL import Image

    left, top, right, bottom = box
    draw.rectangle(
        (left, top, right - 1, bottom - 1), fill=PANEL, outline=RULE, width=1
    )

    raw = item.get("png")
    path = Path(str(raw)) if raw else None
    if path is None or not path.is_file():
        label_font = empty_font
        text = "렌더 없음"
        text_width = draw.textlength(text, font=label_font)
        draw.text(
            (
                left + ((right - left) - text_width) / 2,
                top + ((bottom - top) - board_module._line_height(label_font)) / 2,
            ),
            text,
            font=label_font,
            fill=MUTED,
        )
        return False

    try:
        photo = board_module._flatten(path)
    except OSError:
        return False

    inset = 6
    avail_w = max(1, (right - left) - inset * 2)
    avail_h = max(1, (bottom - top) - inset * 2)
    ratio = min(avail_w / max(1, photo.width), avail_h / max(1, photo.height))
    size = (
        max(1, int(photo.width * ratio)),
        max(1, int(photo.height * ratio)),
    )
    fitted = photo.resize(size, Image.LANCZOS)
    x = left + ((right - left) - size[0]) // 2
    y = top + ((bottom - top) - size[1]) // 2
    canvas.paste(fitted, (x, y))
    return True


def compose(
    items: Sequence[Mapping[str, Any]],
    *,
    out: Path,
    run_id: str = "",
    seed: str = "",
    brief: str = "",
    columns: int = COLUMNS,
    title: str = "CONTACT SHEET",
    diversity: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """렌더 목록 → 콘택트 시트 PNG. 고정 레이아웃이라 바이트가 재현된다."""
    from PIL import Image, ImageDraw

    columns = max(1, int(columns))
    rows = math.ceil(len(items) / columns) if items else 0
    width, height = sheet_size(len(items), columns)

    canvas = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(canvas)

    title_font, title_path = board_module._load_font(44, bold=True)
    meta_font, meta_path = board_module._load_font(20)
    brief_font, brief_path = board_module._load_font(24)
    label_id_font, label_id_path = board_module._load_font(17, bold=True)
    label_meta_font, label_meta_path = board_module._load_font(15)
    empty_font, _ = board_module._load_font(15)

    # --- 제목 블록 -------------------------------------------------------
    draw.text((MARGIN, MARGIN), title, font=title_font, fill=ACCENT)
    diversity_info = (
        dict(diversity)
        if diversity is not None
        else diversity_module.diversity_summary(items)
    )
    diversity_label = (
        f"diversity {diversity_info.get('diversity_score', 100)}% "
        f"(dup {diversity_info.get('duplicate_groups', 0)})"
        if items
        else ""
    )
    meta_line = " · ".join(
        part
        for part in (
            f"run {truncate_text(run_id, 40)}" if run_id else "",
            f"seed {truncate_text(seed, 24)}" if seed else "",
            f"{len(items)} items",
            diversity_label,
        )
        if part
    )
    draw.text((MARGIN, MARGIN + 68), meta_line, font=meta_font, fill=MUTED)

    first_line = truncate_text(
        (str(brief or "").splitlines() or [""])[0], BRIEF_LABEL_LIMIT
    )
    if first_line:
        draw.text(
            (MARGIN, MARGIN + 110),
            _ellipsize_width(draw, first_line, brief_font, width - MARGIN * 2),
            font=brief_font,
            fill=INK,
        )

    rule_y = GRID_TOP - GAP
    draw.rectangle((MARGIN, rule_y, width - MARGIN, rule_y + 1), fill=RULE)

    # --- 격자 ------------------------------------------------------------
    labels: List[Dict[str, str]] = []
    rendered = 0
    thumb_top = GRID_TOP + THUMB_PADDING
    thumb_bottom = GRID_TOP + CELL_HEIGHT - LABEL_HEIGHT
    label_pad = 16

    for index, item in enumerate(items):
        row, column = divmod(index, columns)
        cell_x = MARGIN + column * (CELL_WIDTH + GAP)
        cell_y = GRID_TOP + row * (CELL_HEIGHT + GAP)

        draw.rectangle(
            (cell_x, cell_y, cell_x + CELL_WIDTH - 1, cell_y + CELL_HEIGHT - 1),
            fill=PANEL,
            outline=RULE,
            width=1,
        )
        drew = _draw_thumb(
            canvas,
            draw,
            item=item,
            box=(
                cell_x + THUMB_PADDING,
                cell_y + (thumb_top - GRID_TOP),
                cell_x + CELL_WIDTH - THUMB_PADDING,
                cell_y + (thumb_bottom - GRID_TOP),
            ),
            empty_font=empty_font,
        )
        rendered += 1 if drew else 0
        labels.append(
            _draw_label(
                draw,
                x=cell_x + label_pad,
                y=cell_y + (thumb_bottom - GRID_TOP) + 8,
                width=CELL_WIDTH - label_pad * 2,
                item=item,
                id_font=label_id_font,
                meta_font=label_meta_font,
            )
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG", optimize=True)

    paths = (
        title_path,
        meta_path,
        brief_path,
        label_id_path,
        label_meta_path,
    )
    fallback = any(path is None for path in paths)
    primary = next((Path(path).name for path in paths if path), "pillow-default")

    return {
        "path": str(out),
        "width": width,
        "height": height,
        "columns": columns,
        "rows": rows,
        "count": len(items),
        "rendered": rendered,
        "missing": len(items) - rendered,
        "bytes": out.stat().st_size,
        "labels": labels,
        "run_id": run_id,
        "seed": seed,
        "font": primary,
        "font_fallback": fallback,
        "diversity": diversity_info,
    }


__all__ = [
    "CELL_HEIGHT",
    "CELL_WIDTH",
    "COLUMNS",
    "compose",
    "sheet_size",
    "truncate_text",
]
