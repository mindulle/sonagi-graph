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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import craft
from langchain_core.prompts import ChatPromptTemplate

from graphs.adapters import remote_assets
from graphs.core import artifacts as core_artifacts
from graphs.llm import chat_model
from graphs.profiles import base
from graphs.profiles import board as board_module
from graphs.profiles import contact_sheet as contact_sheet_module
from graphs.profiles import diversity as diversity_module
from graphs.profiles import presets

#: 변주 상한. 24가 기본이고, 대량 탐색(GRAPH-32)은 120까지 허용한다.
DEFAULT_VARIANT_CAP = 24
MAX_VARIANT_CAP = 120

#: 콘택트 시트 썸네일 변환 크기. 긴 페이지는 높이 상한에 맞춰 더 줄어든다.
CONTACT_THUMB_WIDTH = 320
CONTACT_THUMB_HEIGHT = 420

DEFAULT_SPEC: Dict[str, Any] = {
    "platform": "generic",
    "styles_per_direction": None,
    "variant_cap": DEFAULT_VARIANT_CAP,
    "top_n": 9,
    "mockups": 3,
    "image_width": 2000,
    "max_height": 3000,
    "jpg_quality": 90,
}

#: entry.md 경고 줄에 적는 스킵 경로 개수 상한.
SKIP_WARNING_PATHS = 5


def clamp_variant_cap(value: Any) -> Tuple[int, Optional[str]]:
    """`spec.variant_cap`을 1~120으로 조정하고, 조정 사유를 함께 돌려준다.

    잘못된 값은 실행을 실패시키지 않는다. 120을 넘으면 120으로 자르고,
    정수가 아니면 기본값 24를 쓴다. 반환한 경고는 `validate`가 stderr와
    `spec['warnings']`에 남긴다.
    """
    if value is None:
        return DEFAULT_VARIANT_CAP, None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return (
            DEFAULT_VARIANT_CAP,
            f"variant_cap={value!r}은 정수가 아니다 — 기본값 "
            f"{DEFAULT_VARIANT_CAP}을 쓴다",
        )
    if number < 1:
        return 1, f"variant_cap={number}은 1보다 작다 — 1로 조정한다"
    if number > MAX_VARIANT_CAP:
        return (
            MAX_VARIANT_CAP,
            f"variant_cap={number}은 상한 {MAX_VARIANT_CAP}을 넘는다 — "
            f"{MAX_VARIANT_CAP}으로 조정한다",
        )
    return number, None


def aggregate_preflight(
    renders: Sequence[Mapping[str, Any]],
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """변주별 `skipped_assets`/`preflight`를 하나로 모은다(GRAPH-30).

    반환값은 `(skipped_assets, preflight)`이며 meta.json·manifest·entry.md가
    같은 값을 쓴다. 스킵은 경로로 중복을 없애고 경로순으로 정렬한다. 같은
    경로가 다른 이유로 여러 번 나오면 사전순으로 앞선 이유를 남긴다(결정론).

    `checked`는 변주별 리포트의 **최대값**이다. web profile은 한 실행에 레시피
    하나를 공유하므로(변주마다 같은 참조 집합) 합산하면 변주 수만큼 부풀려진다.
    """
    reasons: Dict[str, set] = {}
    checked = 0
    modes = set()
    for render in renders:
        for entry in render.get("skipped_assets") or []:
            if isinstance(entry, Mapping):
                path = str(entry.get("path") or "").strip()
                reason = str(entry.get("reason") or "").strip()
            else:
                path, reason = str(entry or "").strip(), ""
            if not path:
                continue
            reasons.setdefault(path, set()).add(reason)
        report = render.get("preflight")
        if isinstance(report, Mapping):
            try:
                checked = max(checked, int(report.get("checked") or 0))
            except (TypeError, ValueError):
                pass
            mode = str(report.get("mode") or "").strip()
            if mode:
                modes.add(mode)

    skipped = [
        {"path": path, "reason": sorted(reasons[path])[0]} for path in sorted(reasons)
    ]
    # 원격 렌더가 하나라도 있으면 그 실행의 확인은 원격이다(혼합은 없어야 한다).
    mode = "remote" if "remote" in modes else "local"
    return skipped, {"checked": checked, "skipped": len(skipped), "mode": mode}


def skip_warning_line(skipped: Sequence[Mapping[str, Any]]) -> str:
    """entry.md에 넣는 한 줄 경고. 앞에서부터 최대 5개 경로만 적는다."""
    paths = [str(row.get("path") or "") for row in skipped]
    shown = ", ".join(paths[:SKIP_WARNING_PATHS])
    line = f"⚠ 스킵된 자산 {len(paths)}개 — {shown}"
    if len(paths) > SKIP_WARNING_PATHS:
        line += f" 외 {len(paths) - SKIP_WARNING_PATHS}개"
    return line


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

    def validate(self, spec: Dict[str, Any]) -> None:
        """변주 상한을 검증·조정한다(경고는 실행을 막지 않는다)."""
        cap, warning = clamp_variant_cap(spec.get("variant_cap"))
        spec["variant_cap"] = cap
        if not warning:
            return
        warnings = spec.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
            spec["warnings"] = warnings
        warnings.append(warning)
        print(f"[web] 경고: {warning}", file=sys.stderr)

    def plan(
        self,
        ir: Mapping[str, Any],
        spec: Mapping[str, Any],
        styles: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        directions = ir.get("directions") or [fallback_direction(ir)]
        styles = list(styles)
        cap, _ = clamp_variant_cap(spec.get("variant_cap"))
        breakpoints = tuple(spec.get("breakpoints") or ("desktop",))
        themes = tuple(spec.get("themes") or ("light",))
        per_dir_limit = diversity_module.resolve_styles_per_direction(
            cap,
            len(directions),
            combos_per_style=max(1, len(breakpoints) * len(themes)),
            explicit=spec.get("styles_per_direction"),
        )
        # 중복·자산 반복 교체 시 대체 후보를 고를 수 있도록 여유 풀을 함께 계획한다.
        pool_limit = min(len(styles), max(per_dir_limit, per_dir_limit * 2)) if styles else per_dir_limit

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
                limit=pool_limit,
                breakpoints=breakpoints,
                themes=themes,
                seed=f"{spec.get('seed')}-{index}",
            )
            used_names = {v["style"] for v in plan[:per_dir_limit]}
            used_ids = {v["style_id"] for v in plan[:per_dir_limit]}
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

        style_lookup: Dict[str, Mapping[str, Any]] = {}
        for style in styles:
            if style.get("id"):
                style_lookup[str(style["id"])] = style
            if style.get("name"):
                style_lookup[str(style["name"])] = style

        return diversity_module.select_diverse_variants(
            per_direction,
            cap=cap,
            style_lookup=style_lookup,
            min_distance=float(
                spec.get(
                    "min_feature_distance", diversity_module.DEFAULT_MIN_FEATURE_DISTANCE
                )
            ),
            max_asset_reuse=int(
                spec.get("max_asset_reuse", diversity_module.DEFAULT_MAX_ASSET_REUSE)
            ),
            asset_pool=spec.get("asset_pool"),
        )

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

    def contact_sheet(
        self,
        renders: Sequence[Mapping[str, Any]],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
        ctx: base.ProfileContext,
    ) -> Dict[str, Any]:
        """모든 렌더를 한 장으로 모은다(HITL 전에 사람이 눈으로 비교한다).

        썸네일은 렌더 SVG를 셀 크기로 줄여 `thumbs/`에 쓰고, 시트는 실행 루트의
        `contact-sheet.png`다. 변환은 결정론 렌더 파이프라인(`ctx.convert`)을
        그대로 쓰므로 원격·로컬 어느 쪽이든 같은 규약이다.
        """
        run_dir = Path(ctx.run_dir or ctx.out_root)
        thumb_dir = run_dir / "thumbs"
        thumb_dir.mkdir(parents=True, exist_ok=True)

        items: List[Dict[str, Any]] = []
        for render in renders:
            variant_id = str(render.get("id") or "variant")
            png = thumb_dir / f"{variant_id}.png"
            svg = render.get("svg")
            if svg and Path(str(svg)).is_file():
                ctx.convert(
                    Path(str(svg)), png, CONTACT_THUMB_WIDTH, CONTACT_THUMB_HEIGHT
                )
            items.append(
                {
                    "id": variant_id,
                    "png": str(png) if png.is_file() else None,
                    "style": render.get("style"),
                    "theme": render.get("theme"),
                    "breakpoint": render.get("breakpoint"),
                    "direction": render.get("direction"),
                    "axes": render.get("axes"),
                    "asset": render.get("asset"),
                    "motifs": render.get("motifs"),
                }
            )

        return contact_sheet_module.compose(
            items,
            out=run_dir / "contact-sheet.png",
            run_id=str(state.get("run_id") or run_dir.name),
            seed=str(spec.get("seed") or ""),
            brief=str(state.get("brief") or state.get("content") or ""),
        )

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
        skipped_assets, preflight = aggregate_preflight(items)
        contact_sheet_path = state.get("contact_sheet")

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
            "contact_sheet": contact_sheet_path,
            "skipped_assets": skipped_assets,
            "preflight": preflight,
            "items": packed,
        }
        (pack_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        lines = [f"# 엔트리 팩 — {spec.get('platform')}", ""]
        if skipped_assets:
            lines += [skip_warning_line(skipped_assets), ""]
        if contact_sheet_path and Path(str(contact_sheet_path)).is_file():
            lines += [f"- 콘택트 시트: `{Path(str(contact_sheet_path)).name}`", ""]
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
        if contact_sheet_path:
            artifact_paths.append(contact_sheet_path)
        artifact_paths += [pack_dir / "meta.json", pack_dir / "entry.md"]
        manifest = base.run_manifest(
            state,
            run_dir,
            base.collect_artifacts(artifact_paths, run_dir),
            kind="entry_pack",
        )
        # 스키마는 그대로 두고 meta에만 덧붙인다(기존 소비처는 무시해도 된다).
        manifest["meta"] = {
            **dict(manifest.get("meta") or {}),
            "skipped_assets": skipped_assets,
            "preflight": preflight,
        }
        manifest_path = core_artifacts.write_manifest(run_dir, manifest)

        return {
            "dir": str(pack_dir),
            "items": packed,
            "meta": str(pack_dir / "meta.json"),
            "manifest": str(manifest_path),
            "manifest_digest": core_artifacts.manifest_digest(manifest),
            "contact_sheet": contact_sheet_path,
            "skipped_assets": skipped_assets,
            "preflight": preflight,
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
