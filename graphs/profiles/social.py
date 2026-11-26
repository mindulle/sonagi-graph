"""social profile — 소셜 채널 비율·안전영역(Safe Zone) 규격·게이트·팩 (GRAPH-17 / P4).

특정 사이트·계정용 완성 에셋 키트가 아니라, `1:1`·`4:5`·`9:16`·`16:9` 등
소셜 포맷 공통 비율과 상·하단 UI 안전영역 준수 여부를 검증하는 프로필이다.
"""

from __future__ import annotations

import html
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from graphs.profiles import base

SOCIAL_FORMATS: Dict[str, Dict[str, Any]] = {
    "1:1": {
        "slug": "1x1",
        "label": "Square Feed (1:1)",
        "width": 1080,
        "height": 1080,
        "safe_zone": {"top": 90, "bottom": 90, "left": 90, "right": 90},
    },
    "4:5": {
        "slug": "4x5",
        "label": "Portrait Feed (4:5)",
        "width": 1080,
        "height": 1350,
        "safe_zone": {"top": 110, "bottom": 130, "left": 90, "right": 90},
    },
    "9:16": {
        "slug": "9x16",
        "label": "Story / Reels (9:16)",
        "width": 1080,
        "height": 1920,
        "safe_zone": {"top": 250, "bottom": 320, "left": 80, "right": 80},
    },
    "16:9": {
        "slug": "16x9",
        "label": "Landscape (16:9)",
        "width": 1920,
        "height": 1080,
        "safe_zone": {"top": 80, "bottom": 100, "left": 120, "right": 120},
    },
}

DEFAULT_SOCIAL_SPEC: Dict[str, Any] = {
    "profile": "social",
    "formats": ["1:1", "4:5", "9:16", "16:9"],
    "themes": ["light", "dark"],
    "variant_cap": 16,
    "top_n": 6,
    "mockups": 2,
    "jpg_quality": 90,
}


class SocialGateError(base.ProfileError):
    """소셜 비율·안전영역 게이트 실패."""


def _resolve_formats(spec: Mapping[str, Any]) -> List[str]:
    raw = spec.get("formats")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        picked = [str(f) for f in raw if str(f) in SOCIAL_FORMATS]
        if picked:
            return picked
    return list(SOCIAL_FORMATS.keys())


def _safe_bounds(fmt_spec: Mapping[str, Any]) -> Tuple[float, float, float, float]:
    w = float(fmt_spec["width"])
    h = float(fmt_spec["height"])
    sz = fmt_spec["safe_zone"]
    return (
        float(sz["left"]),
        float(sz["top"]),
        w - float(sz["right"]),
        h - float(sz["bottom"]),
    )


def check_aspect_ratio(svg_path: Path, fmt: str) -> Dict[str, Any]:
    """SVG 크기가 요청한 소셜 포맷 비율·해상도와 일치하는지 검사한다."""
    if fmt not in SOCIAL_FORMATS:
        return {"gate": "social.aspect_ratio", "ok": False, "detail": f"알 수 없는 포맷: {fmt}"}
    expected = SOCIAL_FORMATS[fmt]
    w, h = base.svg_size(svg_path)
    ew, eh = float(expected["width"]), float(expected["height"])
    ok = abs(w - ew) <= 1.0 and abs(h - eh) <= 1.0
    return {
        "gate": "social.aspect_ratio",
        "ok": ok,
        "detail": f"{int(w)}x{int(h)} (기대 {int(ew)}x{int(eh)})",
    }


def _element_box(elem: ET.Element) -> Optional[Tuple[float, float, float, float]]:
    tag = elem.tag.split("}")[-1]
    try:
        if tag == "rect":
            x = float(elem.attrib.get("x", 0))
            y = float(elem.attrib.get("y", 0))
            w = float(elem.attrib.get("width", 0))
            h = float(elem.attrib.get("height", 0))
            return (x, y, x + w, y + h)
        if tag == "circle":
            cx = float(elem.attrib.get("cx", 0))
            cy = float(elem.attrib.get("cy", 0))
            r = float(elem.attrib.get("r", 0))
            return (cx - r, cy - r, cx + r, cy + r)
        if tag == "text":
            x = float(elem.attrib.get("x", 0))
            y = float(elem.attrib.get("y", 0))
            fs = float(elem.attrib.get("font-size", 24))
            return (x, y - fs, x + fs, y)
    except (TypeError, ValueError):
        return None
    return None


def check_safe_zone(svg_path: Path, fmt: str) -> Dict[str, Any]:
    """`<g id="content">` 안의 핵심 요소가 안전영역 안에 있는지 검사한다."""
    if fmt not in SOCIAL_FORMATS:
        return {"gate": "social.safe_zone", "ok": False, "detail": f"알 수 없는 포맷: {fmt}"}
    fmt_spec = SOCIAL_FORMATS[fmt]
    safe_l, safe_t, safe_r, safe_b = _safe_bounds(fmt_spec)
    try:
        root = ET.fromstring(svg_path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError) as exc:
        return {"gate": "social.safe_zone", "ok": False, "detail": f"SVG 파싱 실패: {exc}"}

    content_group = None
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "g" and elem.attrib.get("id") == "content":
            content_group = elem
            break
    if content_group is None:
        return {"gate": "social.safe_zone", "ok": False, "detail": '<g id="content"> 없음'}

    violations: List[str] = []
    for child in content_group.iter():
        box = _element_box(child)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        if x1 < safe_l or y1 < safe_t or x2 > safe_r or y2 > safe_b:
            violations.append(
                f"{child.tag.split('}')[-1]}({x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f})"
            )

    return {
        "gate": "social.safe_zone",
        "ok": not violations,
        "detail": "안전영역 준수" if not violations else f"안전영역 침범: {', '.join(violations[:3])}",
    }


class SocialProfile:
    """소셜 포맷(1:1·4:5·9:16·16:9) 비율 및 안전영역 프로필."""

    id = "social"

    def defaults(self) -> Dict[str, Any]:
        return dict(DEFAULT_SOCIAL_SPEC)

    def plan(
        self,
        ir: Mapping[str, Any],
        spec: Mapping[str, Any],
        styles: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        formats = _resolve_formats(spec)
        themes = tuple(spec.get("themes") or ("light",))
        directions = ir.get("directions") or [{"title": "기본", "motifs": []}]
        cap = max(1, int(spec.get("variant_cap") or DEFAULT_SOCIAL_SPEC["variant_cap"]))
        style_name = (
            str(styles[0].get("name") or "social-clean") if styles else "social-clean"
        )

        variants: List[Dict[str, Any]] = []
        for idx, direction in enumerate(directions):
            dir_title = str(direction.get("title") or f"방향 {idx + 1}")
            for fmt in formats:
                fmt_spec = SOCIAL_FORMATS[fmt]
                for theme in themes:
                    vid = f"social-{fmt_spec['slug']}-{theme}-d{idx + 1}"
                    variants.append(
                        {
                            "id": vid,
                            "style": style_name,
                            "format": fmt,
                            "width": fmt_spec["width"],
                            "height": fmt_spec["height"],
                            "safe_zone": dict(fmt_spec["safe_zone"]),
                            "theme": theme,
                            "direction": dir_title,
                            "direction_index": idx,
                            "motifs": list(direction.get("motifs") or ()),
                        }
                    )
                    if len(variants) >= cap:
                        return variants
        return variants

    def render(
        self, variant: Mapping[str, Any], ctx: base.ProfileContext
    ) -> Optional[Dict[str, Any]]:
        fmt = str(variant.get("format") or "1:1")
        fmt_spec = SOCIAL_FORMATS.get(fmt, SOCIAL_FORMATS["1:1"])
        w = int(fmt_spec["width"])
        h = int(fmt_spec["height"])
        safe_l, safe_t, safe_r, safe_b = _safe_bounds(fmt_spec)
        safe_w = int(safe_r - safe_l)
        safe_h = int(safe_b - safe_t)

        dark = variant.get("theme") == "dark"
        bg = "#161616" if dark else "#fbf9f5"
        fg = "#fbf9f5" if dark else "#161616"
        accent = "#d97706" if dark else "#b4551f"

        headline = html.escape(
            str(ctx.extra.get("content") or variant.get("direction") or "SONAGI").splitlines()[0][:40]
        )
        subline = html.escape(f"{fmt_spec['label']} · safe {safe_w}x{safe_h}")

        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}">\n'
            f'  <rect x="0" y="0" width="{w}" height="{h}" fill="{bg}" />\n'
            f'  <g id="safe-zone" data-format="{fmt}">\n'
            f'    <rect x="{int(safe_l)}" y="{int(safe_t)}" width="{safe_w}" height="{safe_h}" '
            f'fill="none" stroke="{accent}" stroke-width="2" stroke-dasharray="8 6" opacity="0.4" />\n'
            f"  </g>\n"
            f'  <g id="content">\n'
            f'    <rect x="{int(safe_l) + 24}" y="{int(safe_t) + 24}" '
            f'width="{max(40, safe_w - 48)}" height="{max(40, safe_h - 48)}" '
            f'fill="none" stroke="{fg}" stroke-width="2" opacity="0.18" />\n'
            f'    <text x="{int(safe_l) + 48}" y="{int(safe_t) + 120}" '
            f'font-size="48" font-weight="700" fill="{fg}">{headline}</text>\n'
            f'    <text x="{int(safe_l) + 48}" y="{int(safe_t) + 176}" '
            f'font-size="24" fill="{accent}">{subline}</text>\n'
            f"  </g>\n"
            f"</svg>\n"
        )

        run_dir = Path(ctx.run_dir or ctx.out_root)
        run_dir.mkdir(parents=True, exist_ok=True)
        svg_path = run_dir / f"{variant['id']}.svg"
        svg_path.write_text(svg, encoding="utf-8")

        return {**variant, "svg": str(svg_path)}

    def gates(self) -> List[base.Gate]:
        return [
            base.Gate(
                name="social.aspect_ratio",
                description="소셜 포맷 비율·해상도 일치 여부",
                blocking=True,
            ),
            base.Gate(
                name="social.safe_zone",
                description="상·하단 UI 안전영역(Safe Zone) 준수 여부",
                blocking=True,
            ),
        ]

    def pack(
        self,
        items: Sequence[Mapping[str, Any]],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> Dict[str, Any]:
        ctx = state.get('profile_context') if isinstance(state.get('profile_context'), base.ProfileContext) else base.context_from_state(state)
        run_dir = Path(ctx.run_dir or ctx.out_root)
        pack_dir = run_dir / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)

        quality = int(spec.get("jpg_quality") or DEFAULT_SOCIAL_SPEC["jpg_quality"])
        packed_items: List[Dict[str, Any]] = []
        written_paths: List[Path] = []

        for item in items:
            svg_raw = item.get("svg")
            if not svg_raw or not Path(str(svg_raw)).is_file():
                continue
            svg_path = Path(str(svg_raw))
            fmt = str(item.get("format") or "1:1")
            ratio_res = check_aspect_ratio(svg_path, fmt)
            safe_res = check_safe_zone(svg_path, fmt)
            if not (ratio_res["ok"] and safe_res["ok"]):
                raise SocialGateError(
                    f"{item.get('id')} 게이트 실패: {ratio_res['detail']} / {safe_res['detail']}"
                )

            fmt_spec = SOCIAL_FORMATS[fmt]
            png_path = pack_dir / f"{item['id']}.png"
            jpg_path = pack_dir / f"{item['id']}.jpg"
            ctx.convert(svg_path, png_path, int(fmt_spec["width"]), int(fmt_spec["height"]))
            ctx.jpeg(png_path, jpg_path, quality)
            written_paths.extend([png_path, jpg_path])

            packed_items.append(
                {
                    "id": item["id"],
                    "format": fmt,
                    "width": fmt_spec["width"],
                    "height": fmt_spec["height"],
                    "safe_zone": fmt_spec["safe_zone"],
                    "gates": [ratio_res, safe_res],
                    "png": str(png_path),
                    "jpg": str(jpg_path),
                }
            )

        meta_path = pack_dir / "meta.json"
        meta_path.write_text(
            json.dumps({"profile": "social", "items": packed_items}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        entry_md = pack_dir / "entry.md"
        lines = ["# Social Pack", ""]
        for row in packed_items:
            lines.append(
                f"- **{row['id']}** ({row['format']}, {row['width']}x{row['height']}): 안전영역 통과"
            )
        entry_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written_paths.extend([meta_path, entry_md])

        return {
            "profile": "social",
            "items": packed_items,
            "meta": str(meta_path),
            "entry": str(entry_md),
            "artifacts": base.collect_artifacts(written_paths, run_dir),
        }

    def mockups(
        self,
        pack_result: Mapping[str, Any],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        return []
