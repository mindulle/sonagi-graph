"""web profile — 콘테스트 제출용 웹/포스터 엔트리 (기존 entry_pack 동작).

`graphs/entry_pack_graph.py`에서 옮겨 온 계획·렌더·포장·목업을 그대로 담는다.
동작 변경이 목적이 아니므로 변주 id·파일 이름·meta 구조는 이전과 같다.

- `plan`  : 방향 × 스타일 × 브레이크포인트 × 테마 → 후보 (시드 고정)
- `render`: `render_recipe.py` 호출 (결정론 렌더)
- `pack`  : PNG·JPG·meta.json·entry.md
- `mockups`: 카드·포스터 평면 목업 (Pillow, 결정론)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import craft
from langchain_core.prompts import ChatPromptTemplate

from graphs.adapters import remote_assets
from graphs.core import artifacts as core_artifacts
from graphs.llm import chat_model
from graphs.profiles import base
from graphs.profiles import board as board_module
from graphs.profiles import presets

DEFAULT_SPEC: Dict[str, Any] = {
    "platform": "generic",
    "styles_per_direction": 2,
    "variant_cap": 24,
    "top_n": 9,
    "mockups": 3,
    "image_width": 2000,
    "max_height": 3000,
    "jpg_quality": 90,
}


def fallback_direction(ir: Mapping[str, Any]) -> Dict[str, Any]:
    tones = " · ".join(ir.get("tones") or ["편집"])
    return {
        "title": "기본 방향",
        "description": f"{tones} 톤의 기본 구성",
        "motifs": [],
        "visual_language": "",
    }


def direction_text(direction: Mapping[str, Any]) -> str:
    parts = [direction.get("title", ""), direction.get("description", "")]
    parts += [str(m) for m in direction.get("motifs") or []]
    parts.append(direction.get("visual_language", ""))
    return " ".join(p for p in parts if p)


def _ensure_recipe(ctx: base.ProfileContext, run_dir: Path) -> Path:
    """브리프 → recipe.json. 한 실행 안에서 한 번만 만든다."""
    cached = ctx.extra.get("recipe")
    if cached:
        return Path(cached)

    content = str(ctx.extra.get("content") or "")
    content_path = Path(content)
    if not content_path.is_file():
        content_path = run_dir / "brief.md"
        content_path.write_text(content, encoding="utf-8")

    recipe = run_dir / "recipe.json"
    ctx.run(
        [
            sys.executable,
            ctx.assets_dir / "scripts/import_content.py",
            content_path,
            "--catalog",
            ctx.catalog,
            "--out",
            recipe,
        ]
    )
    ctx.extra["recipe"] = str(recipe)
    return recipe


def _describe(state: Mapping[str, Any], option: Mapping[str, Any]) -> str:
    spec = state.get("spec") or {}
    direction = next(
        (
            d
            for d in state.get("directions") or []
            if d.get("title") == option.get("direction")
        ),
        {},
    )
    if spec.get("skip_llm"):
        motifs = ", ".join(direction.get("motifs") or []) or "형태 중심"
        return (
            f"{option.get('direction') or '기본 방향'} — "
            f"{direction.get('description') or '브리프 기반 구성'}. "
            f"모티프: {motifs}. 스타일 {option.get('style')} ({option.get('theme')})."
        )
    try:
        llm = chat_model(
            "ENTRY_COPY_LLM_MODEL", default_model="gemini-3-flash", temperature=0.6
        )
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "콘테스트 제출용 설명을 2~3문장으로 쓰세요. 미사여구 없이 무엇을 왜 그렇게 했는지.",
                ),
                (
                    "user",
                    "브리프: {brief}\n방향: {direction}\n모티프: {motifs}\n스타일: {style} ({theme})",
                ),
            ]
        )
        resp = (prompt | llm).invoke(
            {
                "brief": state.get("brief", ""),
                "direction": option.get("direction", ""),
                "motifs": ", ".join(direction.get("motifs") or []),
                "style": option.get("style", ""),
                "theme": option.get("theme", ""),
            }
        )
        text = str(getattr(resp, "content", resp)).strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    return f"{option.get('direction') or '기본 방향'} — 스타일 {option.get('style')} ({option.get('theme')})."


def _card_mockup(page: Path, out: Path, label: str) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    canvas_size = (1400, 900)
    canvas = Image.new("RGB", canvas_size, (239, 237, 232))
    draw = ImageDraw.Draw(canvas)
    card = (150, 110, 1250, 750)
    shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (card[0] + 6, card[1] + 18, card[2] + 6, card[3] + 18),
        radius=18,
        fill=(0, 0, 0, 60),
    )
    canvas.paste(
        Image.alpha_composite(
            canvas.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(14))
        ).convert("RGB"),
        (0, 0),
    )
    draw.rounded_rectangle(
        card, radius=14, fill=(255, 255, 255), outline=(220, 216, 208), width=2
    )

    with Image.open(page) as image:
        inner = (card[0] + 28, card[1] + 28, card[2] - 28, card[3] - 28)
        fitted = image.convert("RGB")
        ratio = min(inner[2] - inner[0], inner[3] - inner[1]) / max(
            fitted.width, fitted.height
        )
        size = (max(1, int(fitted.width * ratio)), max(1, int(fitted.height * ratio)))
        fitted = fitted.resize(size, Image.LANCZOS)
        x = inner[0] + ((inner[2] - inner[0]) - size[0]) // 2
        y = inner[1] + ((inner[3] - inner[1]) - size[1]) // 2
        canvas.paste(fitted, (x, y))

    if label:
        draw.text((150, 800), label[:80], fill=(60, 58, 54))
    canvas.save(out, "PNG", optimize=True)


def _poster_mockup(page: Path, out: Path) -> None:
    from PIL import Image

    canvas = Image.new("RGB", (1400, 900), (214, 209, 200))
    frame = (300, 70, 1100, 830)
    with Image.open(page) as image:
        fitted = image.convert("RGB")
        ratio = min(frame[2] - frame[0], frame[3] - frame[1]) / max(
            fitted.width, fitted.height
        )
        size = (max(1, int(fitted.width * ratio)), max(1, int(fitted.height * ratio)))
        fitted = fitted.resize(size, Image.LANCZOS)
        x = frame[0] + ((frame[2] - frame[0]) - size[0]) // 2
        y = frame[1] + ((frame[3] - frame[1]) - size[1]) // 2
        canvas.paste(fitted, (x, y))
    canvas.save(out, "PNG", optimize=True)


class WebProfile:
    """콘테스트 엔트리(웹·포스터). 기존 entry_pack 공개 동작을 유지한다."""

    id = "web"

    def defaults(self) -> Dict[str, Any]:
        return dict(DEFAULT_SPEC)

    def plan(
        self,
        ir: Mapping[str, Any],
        spec: Mapping[str, Any],
        styles: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        directions = ir.get("directions") or [fallback_direction(ir)]
        styles = list(styles)

        per_direction: List[Dict[str, Any]] = []
        remaining = list(styles)
        for index, direction in enumerate(directions):
            brief = dict(ir)
            brief["purpose"] = direction_text(direction)
            brief["tones"] = list(ir.get("tones") or ["편집"])
            # 이미 쓴 스타일은 다음 방향에서 빼서 방향마다 다른 조합이 나오게 한다.
            pool = remaining or styles
            plan = craft.plan_variants(
                brief,
                pool,
                limit=int(spec.get("styles_per_direction", 2)),
                breakpoints=tuple(spec.get("breakpoints") or ("desktop",)),
                themes=tuple(spec.get("themes") or ("light",)),
                seed=f"{spec.get('seed')}-{index}",
            )
            used_names = {v["style"] for v in plan}
            used_ids = {v["style_id"] for v in plan}
            remaining = [
                s
                for s in remaining
                if s.get("name") not in used_names and s.get("id") not in used_ids
            ]
            for variant in plan:
                variant["direction"] = direction.get("title", f"방향 {index + 1}")
                variant["direction_index"] = index
                variant["motifs"] = list(direction.get("motifs") or [])
                per_direction.append(variant)

        seen = set()
        variants = []
        for variant in per_direction:
            key = (variant["style"], variant["breakpoint"], variant["theme"])
            if key in seen:
                continue
            seen.add(key)
            variants.append(variant)
            if len(variants) >= int(spec.get("variant_cap", 24)):
                break
        return variants

    def render(
        self, variant: Mapping[str, Any], ctx: base.ProfileContext
    ) -> Optional[Dict[str, Any]]:
        run_dir = Path(ctx.run_dir or ctx.out_root)
        run_dir.mkdir(parents=True, exist_ok=True)
        recipe = _ensure_recipe(ctx, run_dir)

        out = run_dir / variant["id"]
        if remote_assets.enabled():
            rendered = remote_assets.render(
                recipe,
                variant["style"],
                variant["breakpoint"],
                dark=variant.get("theme") == "dark",
                out_dir=out,
                paths=remote_assets.RemotePaths.from_env(assets_dir=ctx.assets_dir),
                run=ctx.run,
            )
            return {**variant, **rendered, "renderer": "devops"}

        cmd = [
            sys.executable,
            ctx.assets_dir / "scripts/render_recipe.py",
            "--recipe",
            recipe,
            "--style",
            ctx.assets_dir / "styles" / f"{variant['style']}.json",
            "--catalog",
            ctx.catalog,
            "--tokens",
            ctx.tokens,
            "--out",
            out,
            "--breakpoint",
            variant["breakpoint"],
        ]
        if variant.get("theme") == "dark":
            cmd.append("--dark")
        ctx.run(cmd)

        svg = next(out.glob("*.svg"), None)
        manifest = next(out.glob("*.manifest.json"), None)
        if not svg:
            return None
        return {
            **variant,
            "svg": str(svg),
            "manifest": str(manifest) if manifest else None,
        }

    def gates(self) -> List[base.Gate]:
        """web은 그래프의 critic/gate가 판정한다(profile 전용 하드 게이트 없음)."""
        return []

    def pack(
        self,
        items: Sequence[Mapping[str, Any]],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> Dict[str, Any]:
        ctx = base.context_from_state(state)
        run_dir = Path(ctx.run_dir or ctx.out_root)
        pack_dir = run_dir / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)

        # 프리셋 해석: spec 값 → platform 프리셋 → generic. spec이 항상 이긴다.
        preset, preset_warnings = presets.resolve_for_spec(spec)
        width = int(spec.get("image_width") or preset.image_width)
        max_height = int(spec.get("max_height") or preset.max_height)
        quality = int(spec.get("jpg_quality") or preset.jpg_quality)
        formats = presets.formats_for_spec(spec, preset)
        naming = str(spec.get("naming") or preset.naming)
        board_spec = spec.get("board")
        board_enabled = preset.board if board_spec is None else bool(board_spec)
        options = {o["id"]: o for o in state.get("options") or []}

        packed: List[Dict[str, Any]] = []
        for index, render in enumerate(items, start=1):
            variant_id = render["id"]
            svg = Path(render["svg"])
            stem = presets.filename(naming, variant_id, index)
            png = pack_dir / f"{stem}.png"
            jpg = pack_dir / f"{stem}.jpg"
            ctx.convert(svg, png, width, max_height)
            if "jpg" in formats:
                ctx.jpeg(png, jpg, quality)
            option = options.get(variant_id) or render
            packed.append(
                {
                    "id": variant_id,
                    "direction": option.get("direction"),
                    "style": render.get("style"),
                    "theme": render.get("theme"),
                    "svg": str(svg),
                    "png": str(png),
                    "jpg": str(jpg) if jpg.is_file() else None,
                    "png_size": png.stat().st_size,
                    "jpg_size": jpg.stat().st_size if jpg.is_file() else 0,
                    "sha256": core_artifacts.sha256_file(png),
                    "description": _describe(state, option),
                }
            )

        # 엔트리 보드 — 프리셋이 요구할 때만. 선택 1순위 엔트리를 표지로 쓴다.
        board_row: Optional[Dict[str, Any]] = None
        if board_enabled and packed:
            first = packed[0]
            board_path = pack_dir / "board.png"
            board_row = board_module.compose(
                image=Path(first["png"]),
                out=board_path,
                platform=spec.get("platform") or preset.id,
                direction=str(first.get("direction") or ""),
                style=str(first.get("style") or ""),
                theme=str(first.get("theme") or ""),
                description=str(first.get("description") or ""),
                entry_id=str(first["id"]),
            )

        meta = {
            "platform": spec.get("platform"),
            "preset": preset.id,
            "preset_warnings": preset_warnings,
            "brief": state.get("brief", ""),
            "seed": spec.get("seed"),
            "image_width": width,
            "max_height": max_height,
            "jpg_quality": quality,
            "formats": list(formats),
            "naming": naming,
            "variants_total": len(state.get("variants") or []),
            "rendered_total": len(state.get("renders") or []),
            "selected_total": len(packed),
            "board": board_row,
            "items": packed,
        }
        (pack_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        lines = [f"# 엔트리 팩 — {spec.get('platform')}", ""]
        if board_row:
            lines += [
                f"- 보드: `board.png` ({board_row['width']}×{board_row['height']}, "
                f"{board_row['bytes']:,}B)",
                "",
            ]
        for item in packed:
            lines += [
                f"## {item['id']}",
                f"- 방향: {item['direction']}",
                f"- 스타일: {item['style']} ({item['theme']})",
                f"- PNG: `{Path(item['png']).name}` ({item['png_size']:,}B)",
                f"- JPG: `{Path(item['jpg']).name}` ({item['jpg_size']:,}B)"
                if item.get("jpg")
                else "- JPG: 없음(프리셋 형식에 포함되지 않음)",
                "",
                item["description"],
                "",
            ]
        (pack_dir / "entry.md").write_text("\n".join(lines), encoding="utf-8")

        artifact_paths: List[Any] = []
        for item in packed:
            artifact_paths += [item["svg"], item["png"]]
            if item.get("jpg"):
                artifact_paths.append(item["jpg"])
        if board_row:
            artifact_paths.append(board_row["path"])
        artifact_paths += [pack_dir / "meta.json", pack_dir / "entry.md"]
        manifest = base.run_manifest(
            state,
            run_dir,
            base.collect_artifacts(artifact_paths, run_dir),
            kind="entry_pack",
        )
        manifest_path = core_artifacts.write_manifest(run_dir, manifest)

        return {
            "dir": str(pack_dir),
            "items": packed,
            "meta": str(pack_dir / "meta.json"),
            "manifest": str(manifest_path),
            "manifest_digest": core_artifacts.manifest_digest(manifest),
        }

    def mockups(
        self, items: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        if not items:
            return []
        mock_dir = Path(
            spec.get("mockup_dir") or Path(items[0]["png"]).parent / "mockups"
        )
        mock_dir.mkdir(parents=True, exist_ok=True)

        rows: List[Dict[str, Any]] = []
        for item in items:
            card = mock_dir / f"{item['id']}-card.png"
            _card_mockup(Path(item["png"]), card, item.get("direction") or item["id"])
            poster = mock_dir / f"{item['id']}-poster.png"
            _poster_mockup(Path(item["png"]), poster)
            rows.append(
                {
                    "id": item["id"],
                    "card": str(card),
                    "poster": str(poster),
                    "card_sha256": core_artifacts.sha256_file(card),
                }
            )
        return rows
