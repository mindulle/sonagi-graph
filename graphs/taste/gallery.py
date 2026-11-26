"""taste/gallery — 월간 성장 갤러리 HTML/PNG 생성 (T4 / GRAPH-38).

결정 원장에서 선택(Chosen) vs 거절(Rejected) 후보를 나란히 배치하고,
선택 이유·태그·자산 선호 상태를 함께 HTML과 PNG로 남긴다.
"""

from __future__ import annotations

import html
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from graphs.profiles import board as board_module

PAPER = board_module.PAPER
INK = board_module.INK
MUTED = board_module.MUTED
ACCENT = board_module.ACCENT
RULE = board_module.RULE
PANEL = board_module.PANEL


def _now_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _record_month(record: Mapping[str, Any]) -> Optional[str]:
    for key in ("ts", "timestamp", "created_at", "updated_at"):
        val = record.get(key)
        if isinstance(val, str) and len(val) >= 7 and val[4] == "-":
            return val[:7]
    return None


def _option_image(option: Mapping[str, Any]) -> str:
    for key in ("png", "image", "thumbnail", "svg"):
        val = option.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def collect_monthly_pairs(
    records: Sequence[Mapping[str, Any]],
    *,
    month: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """선택 vs 거절 대비 쌍을 추린다. `month`가 있으면 해당 월 기록을 우선 필터링한다."""
    pairs: List[Dict[str, Any]] = []
    for rec in records:
        rec_month = _record_month(rec)
        if month and rec_month and rec_month != month:
            continue
        selected_ids = set(
            str(x) for x in (rec.get("selected") or rec.get("selection") or ()) if x
        )
        if not selected_ids:
            continue
        chosen = []
        rejected = []
        for opt in rec.get("options") or ():
            if not isinstance(opt, Mapping):
                continue
            item = {
                "id": str(opt.get("id") or ""),
                "style": str(opt.get("style") or ""),
                "image": _option_image(opt),
            }
            if item["id"] in selected_ids:
                chosen.append(item)
            else:
                rejected.append(item)
        if not chosen and not rejected:
            continue
        pairs.append(
            {
                "thread_id": str(rec.get("thread_id") or ""),
                "brief": str(rec.get("brief") or ""),
                "reason": str(rec.get("reason") or ""),
                "reason_tags": [
                    str(t)
                    for t in (rec.get("reason_tags") or rec.get("tags") or ())
                    if isinstance(t, str) and t.strip()
                ],
                "chosen": chosen,
                "rejected": rejected,
            }
        )
    return pairs


def render_gallery_html(
    pairs: Sequence[Mapping[str, Any]],
    *,
    month: str,
    prefs: Optional[Mapping[str, Any]] = None,
) -> str:
    """선택 vs 거절을 나란히 보여주는 정적 HTML 성장 갤러리를 만든다."""
    assets_map = (prefs or {}).get("assets") or {}
    preferred = [
        k for k, v in assets_map.items() if isinstance(v, Mapping) and v.get("status") == "preferred"
    ]
    demoted = [
        k for k, v in assets_map.items() if isinstance(v, Mapping) and v.get("status") == "demoted"
    ]

    cards_html: List[str] = []
    for idx, pair in enumerate(pairs, start=1):
        tags_html = "".join(
            f'<span class="tag">{html.escape(t)}</span>' for t in pair.get("reason_tags") or ()
        )
        reason_text = html.escape(pair.get("reason") or "이유 미기재")
        brief_text = html.escape(pair.get("brief") or "")

        def _render_side(items: Sequence[Mapping[str, Any]], label: str, cls: str) -> str:
            if not items:
                return f'<div class="col {cls}"><h4>{label}</h4><p class="empty">없음</p></div>'
            lis = []
            for it in items:
                img = it.get("image") or ""
                media = (
                    f'<img src="{html.escape(img)}" alt="{html.escape(it.get("id", ""))}" />'
                    if img.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg"))
                    or img.startswith("http")
                    else f'<div class="placeholder">{html.escape(img or "이미지 없음")}</div>'
                )
                lis.append(
                    f'<div class="opt">{media}'
                    f'<div class="meta"><strong>{html.escape(it.get("id", ""))}</strong> '
                    f'<span>{html.escape(it.get("style", ""))}</span></div></div>'
                )
            return f'<div class="col {cls}"><h4>{label}</h4>{"".join(lis)}</div>'

        chosen_html = _render_side(pair.get("chosen") or (), "선택 (Chosen)", "chosen")
        rejected_html = _render_side(pair.get("rejected") or (), "거절 (Rejected)", "rejected")
        cards_html.append(
            f'<section class="pair">'
            f'<div class="pair-head"><span>#{idx}</span> {brief_text} '
            f'<div class="tags">{tags_html}</div></div>'
            f'<p class="reason">이유: {reason_text}</p>'
            f'<div class="grid">{chosen_html}{rejected_html}</div>'
            f"</section>"
        )

    body = "\n".join(cards_html) if cards_html else '<p class="empty">기록된 비교 쌍이 없습니다.</p>'
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<title>성장 갤러리 ({html.escape(month)})</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #fbf9f5; color: #161616; margin: 40px auto; max-width: 1040px; padding: 0 20px; }}
  header {{ border-bottom: 1px solid #d6d1c8; padding-bottom: 16px; margin-bottom: 24px; }}
  .stats {{ color: #666; font-size: 14px; margin-top: 6px; }}
  .pair {{ background: #fff; border: 1px solid #e2ddd4; border-radius: 10px; padding: 16px; margin-bottom: 20px; }}
  .pair-head {{ font-weight: 600; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  .tag {{ background: #f1ede4; border-radius: 999px; padding: 2px 10px; font-size: 12px; font-weight: 500; }}
  .reason {{ color: #333; margin: 8px 0 14px; font-size: 14px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .col h4 {{ margin: 0 0 10px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; }}
  .chosen h4 {{ color: #1f4536; }}
  .rejected h4 {{ color: #9a3412; }}
  .opt {{ border: 1px solid #ebe6dc; border-radius: 8px; padding: 8px; margin-bottom: 8px; background: #faf8f4; }}
  .opt img {{ max-width: 100%; max-height: 220px; display: block; margin-bottom: 6px; }}
  .placeholder {{ padding: 24px 12px; text-align: center; color: #777; font-size: 12px; background: #f2efe8; border-radius: 6px; margin-bottom: 6px; }}
  .meta {{ font-size: 12px; color: #444; display: flex; justify-content: space-between; }}
  .empty {{ color: #888; font-size: 13px; }}
</style>
</head>
<body>
<header>
  <h1>성장 갤러리 — {html.escape(month)}</h1>
  <div class="stats">비교 {len(pairs)}건 · preferred {len(preferred)}개 · demoted {len(demoted)}개</div>
</header>
{body}
</body>
</html>
"""


def render_gallery_png(
    pairs: Sequence[Mapping[str, Any]],
    *,
    out: Path,
    month: str,
    prefs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """선택 vs 거절 대비를 한눈에 보는 PNG 성장 갤러리를 생성한다."""
    from PIL import Image, ImageDraw

    rows = max(1, len(pairs))
    width = 1200
    header_h = 120
    row_h = 260
    height = header_h + rows * row_h + 40

    canvas = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(canvas)

    title_font, _ = board_module._load_font(36, bold=True)
    meta_font, _ = board_module._load_font(18)
    label_font, _ = board_module._load_font(16, bold=True)
    small_font, _ = board_module._load_font(14)

    assets_map = (prefs or {}).get("assets") or {}
    preferred = sum(
        1 for v in assets_map.values() if isinstance(v, Mapping) and v.get("status") == "preferred"
    )
    demoted = sum(
        1 for v in assets_map.values() if isinstance(v, Mapping) and v.get("status") == "demoted"
    )

    draw.text((40, 32), f"GROWTH GALLERY · {month}", font=title_font, fill=ACCENT)
    draw.text(
        (40, 80),
        f"{len(pairs)} pairs · preferred {preferred} · demoted {demoted}",
        font=meta_font,
        fill=MUTED,
    )
    draw.rectangle((40, 112, width - 40, 113), fill=RULE)

    if not pairs:
        draw.text((40, 150), "비교 기록이 없습니다.", font=meta_font, fill=MUTED)
    else:
        for idx, pair in enumerate(pairs):
            y = header_h + idx * row_h + 16
            draw.rectangle((40, y, width - 40, y + row_h - 24), fill=PANEL, outline=RULE, width=1)
            tags = ", ".join(pair.get("reason_tags") or ())
            reason = str(pair.get("reason") or "이유 미기재")
            head_txt = f"#{idx + 1}  {reason}" + (f"  [{tags}]" if tags else "")
            draw.text((56, y + 12), head_txt[:90], font=label_font, fill=INK)

            chosen = (pair.get("chosen") or [{}])[0]
            rejected = (pair.get("rejected") or [{}])[0]
            for col_idx, (title_lbl, item) in enumerate(
                (("CHOSEN", chosen), ("REJECTED", rejected))
            ):
                bx = 56 + col_idx * 556
                by = y + 46
                bw, bh = 520, 168
                draw.rectangle((bx, by, bx + bw, by + bh), fill=PAPER, outline=RULE, width=1)
                draw.text((bx + 12, by + 10), f"{title_lbl}: {item.get('id', '-')}", font=small_font, fill=INK)
                draw.text((bx + 12, by + 34), f"style: {item.get('style', '-')}", font=small_font, fill=MUTED)
                img_raw = item.get("image") or ""
                img_path = Path(img_raw) if img_raw else None
                if img_path and img_path.is_file() and img_path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    try:
                        thumb = board_module._flatten(img_path)
                        ratio = min(140 / max(1, thumb.width), 100 / max(1, thumb.height))
                        tw = max(1, int(thumb.width * ratio))
                        th = max(1, int(thumb.height * ratio))
                        canvas.paste(thumb.resize((tw, th), Image.LANCZOS), (bx + bw - tw - 16, by + 56))
                    except OSError:
                        pass

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG", optimize=True)
    return {
        "path": str(out),
        "width": width,
        "height": height,
        "pairs_count": len(pairs),
    }


def build_monthly_gallery(
    records: Sequence[Mapping[str, Any]],
    *,
    out_dir: Path,
    month: Optional[str] = None,
    prefs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """월간 성장 갤러리 HTML과 PNG를 생성한다."""
    target_month = month or _now_month()
    pairs = collect_monthly_pairs(records, month=target_month)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    html_path = out_dir / f"gallery-{target_month}.html"
    tmp_html = html_path.with_name(html_path.name + ".tmp")
    tmp_html.write_text(
        render_gallery_html(pairs, month=target_month, prefs=prefs), encoding="utf-8"
    )
    os.replace(tmp_html, html_path)

    png_path = out_dir / f"gallery-{target_month}.png"
    png_meta = render_gallery_png(pairs, out=png_path, month=target_month, prefs=prefs)

    return {
        "month": target_month,
        "html_path": str(html_path),
        "png_path": png_meta["path"],
        "pairs_count": len(pairs),
    }
