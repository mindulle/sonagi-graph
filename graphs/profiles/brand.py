"""brand profile — 브랜드/아이덴티티 엔트리 (GRAPH-18, P4b).

마크(로고·락업)는 **사용자가 만든 SVG**다. 그래프는 마크를 새로 그리지 않고,
납품 가능한 상태로 정규화·아웃라인·게이트·포장한다. 이 순서가 전부다.

```
mark.svg
  │
  ▼
normalize   <style>/var()/class → 프레젠테이션 속성 (순수 파이썬, 결정론)
  │
  ▼
outline     vectorcraft-cli convert --outline-text (텍스트 → 패스)
  │
  ▼
gates       text 0 · style/var 0 · 256px 불투명 픽셀 > 0 · PDF > 0B (fail closed)
  │
  ▼
pack        normalized.svg · outlined.svg · mark.pdf · PNG 2000 · JPG 90
            · 사이즈/배경 PNG · meta.json · entry.md
  │
  ▼
mockups     카드(web helper 재사용) · 아이콘 타일 (Pillow, 결정론)
```

spec 예::

    {
      "profile": "brand",
      "brand": {
        "name": "sonagi",
        "mark_path": "/path/mark.svg",        # VectorCraft/DesignCraft 산출물
        "palette": {"ink": "#161616", "paper": "#fbf9f5",
                    "accent": "#b4551f", "calm": "#1f4536"},
        "variants": {"sizes": [16, 32, 64, 256, 512],
                     "backgrounds": ["light", "dark"]}
      }
    }

게이트는 **실패 시 멈춘다**(fail closed). 테스트에서만
`spec["allow_brand_checks"] = True`로 건너뛴다(문자열 `"false"`는 우회로 인정하지
않는다 — print와 같은 규칙).

정규화 규칙(normalize):
- `:root`(또는 `html`)의 CSS 커스텀 프로퍼티를 값으로 치환한다. 다크 테마
  (`[data-theme="dark"]`, `@media (prefers-color-scheme: dark)`)는 **버린다** —
  납품 기본값은 라이트다.
- 클래스 규칙을 프레젠테이션 속성으로 펼친다. 우선순위는 실제 CSS 캐스케이드와
  같다: 인라인 `style` > 클래스 규칙 > 요소의 기존 속성.
- `<style>` 블록과 `class` 속성을 제거한다. 남은 `var()`는 게이트가 잡는다.

아웃라인 품질은 설치된 폰트에 달렸다. 폰트가 없으면 `vectorcraft-cli`가 대체
폰트로 그린 뒤 패스를 만들고 stdout에 경고를 남긴다. 납품 전 원본 폰트를
설치하거나 시스템 폰트만 쓴 마크를 넣는다.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from graphs.core import artifacts as core_artifacts
from graphs.core import env as core_env
from graphs.profiles import base

DEFAULT_SIZES: Tuple[int, ...] = (16, 32, 64, 256, 512)
DEFAULT_BACKGROUNDS: Tuple[str, ...] = ("light", "dark")
BACKGROUNDS = ("light", "dark")

DEFAULT_PALETTE: Dict[str, str] = {
    "ink": "#161616",
    "paper": "#fbf9f5",
    "accent": "#b4551f",
    "calm": "#1f4536",
}

#: 클래스 규칙 → 프레젠테이션 속성으로 펼치는 목록. 앞의 7개가 계약이고,
#: 나머지는 마크에서 흔한 짝(paint-order·투명도 등)이라 함께 옮긴다.
PROPERTIES: Tuple[str, ...] = (
    "fill",
    "stroke",
    "stroke-width",
    "font-family",
    "font-size",
    "font-weight",
    "letter-spacing",
    "fill-opacity",
    "stroke-opacity",
    "opacity",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-dasharray",
    "font-style",
    "text-anchor",
    "paint-order",
)

DEFAULT_SPEC: Dict[str, Any] = {
    "image_width": 2000,
    "max_height": 3000,
    "jpg_quality": 90,
    "allow_brand_checks": False,
    # 마크는 사용자가 준다 — 방향/레퍼런스가 산출물에 쓰이지 않는다.
    "skip_knowledge": True,
    "skip_llm": True,
}

GATES: Tuple[base.Gate, ...] = (
    base.Gate("brand.no_text", "outlined.svg에 <text>가 없다(아웃라인 완료)"),
    base.Gate("brand.no_style", "outlined.svg에 <style>·var()가 없다(정규화 완료)"),
    base.Gate(
        "brand.raster", "256px 래스터에 불투명 픽셀이 1개 이상 있다(빈 렌더 E015)"
    ),
    base.Gate("brand.pdf", "출력 PDF가 존재하고 0바이트보다 크다"),
)

#: 정규화가 인식하는 태그·속성 패턴. 속성값 안의 `>`는 인용부 안에서만 허용한다.
_TAG = re.compile(
    r"<([A-Za-z][A-Za-z0-9:_-]*)"
    r"((?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)"
    r"(\s*/?)>"
)
_ATTR = re.compile(r"([^\s=/>]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CSS_VAR = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^()]*))?\)")
_CLASS_SELECTOR = re.compile(r"^(?P<tag>[A-Za-z][\w:-]*)?\.(?P<cls>[-\w]+)$")
_TEXT_OPEN = re.compile(r"<\s*text\b", re.I)
_CLASS_ATTR = re.compile(r"\sclass\s*=", re.I)


class BrandGateError(RuntimeError):
    """브랜드 게이트 실패. 어떤 검사가 왜 막았는지 그대로 들고 있다."""

    def __init__(self, failures: Sequence[Mapping[str, Any]]):
        self.failures = [dict(item) for item in failures]
        lines = [f"브랜드 게이트 실패 {len(self.failures)}건 — 납품을 중단한다."]
        for item in self.failures:
            lines.append(f"- {item.get('id')}: {item.get('detail')}")
        lines.append("테스트에서만 spec['allow_brand_checks']=true로 건너뛸 수 있다.")
        super().__init__("\n".join(lines))


def _tail(exc: BaseException, limit: int = 400) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[-limit:] if len(text) > limit else text


def _context(state: Mapping[str, Any]) -> base.ProfileContext:
    ctx = state.get("profile_ctx")
    if isinstance(ctx, base.ProfileContext):
        return ctx
    return base.ProfileContext(out_root=Path(core_env.out_root("entry_pack")))


def _binary(ctx: Optional[base.ProfileContext]) -> str:
    """실행 파일 이름. 테스트/다른 설치본은 ctx.extra['vectorcraft']로 바꾼다."""
    if ctx is not None:
        hinted = ctx.extra.get("vectorcraft")
        if hinted:
            return str(hinted)
    return core_env.VECTORCRAFT


# ---------------------------------------------------------------------------
# 정규화 (순수 파이썬 — 외부 도구 없음)
# ---------------------------------------------------------------------------


def _strip_at_rules(css: str) -> str:
    """`@media`·`@import` 같은 at-rule 블록을 통째로 버린다.

    마크의 스타일시트에 다크 테마 블록이 섞여 있으면 라이트 값만 남겨야 한다.
    at-rule은 중괄호가 중첩되므로 정규식 대신 깊이를 세며 자른다.
    """
    out: List[str] = []
    index = 0
    while index < len(css):
        if css[index] != "@":
            out.append(css[index])
            index += 1
            continue
        depth = 0
        cursor = index
        while cursor < len(css):
            if css[cursor] == "{":
                depth += 1
            elif css[cursor] == "}":
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        index = cursor
    return "".join(out)


def _parse_declarations(block: str) -> Dict[str, str]:
    """`a: b; c: d` → 사전. `!important`는 떼고, 빈 값은 버린다."""
    declarations: Dict[str, str] = {}
    for chunk in block.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        name = name.strip().lower()
        value = re.sub(r"!\s*important\s*$", "", value.strip(), flags=re.I).strip()
        if name and value and not name.startswith("--"):
            declarations[name] = value
    return declarations


def _is_light_root(selector: str) -> bool:
    """`:root`·`html`의 라이트 값만 커스텀 프로퍼티 원본으로 인정한다."""
    low = selector.strip().lower()
    if not low or "dark" in low:
        return False
    return low in (":root", "html", ":host") or low.startswith(":root")


def _class_key(selector: str) -> Optional[Tuple[str, str]]:
    """`.wordmark` → (`*`, `wordmark`), `path.accent` → (`path`, `accent`)."""
    match = _CLASS_SELECTOR.match(selector.strip())
    if not match:
        return None
    return (match.group("tag") or "*").lower(), match.group("cls")


def _resolve_var(value: str, variables: Mapping[str, str], passes: int = 8) -> str:
    """`var(--x, fallback)`를 값으로 바꾼다. 사슬은 최대 `passes`번 따라간다.

    해석할 수 없는 변수(정의도 fallback도 없음)는 그대로 남긴다 — 조용히 지우면
    색이 사라지고, 게이트가 `var(`로 잡아 원인을 알려 준다.
    """

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        fallback = match.group(2)
        if name in variables:
            return variables[name]
        return fallback.strip() if fallback is not None else match.group(0)

    resolved = str(value)
    for _ in range(max(1, passes)):
        updated = _CSS_VAR.sub(replace, resolved)
        if updated == resolved:
            break
        resolved = updated
    return resolved.strip()


def parse_css(css: str) -> Tuple[Dict[str, str], Dict[Tuple[str, str], Dict[str, str]]]:
    """스타일시트 → (커스텀 프로퍼티, 클래스 규칙)."""
    cleaned = _CSS_COMMENT.sub("", str(css)).replace("<![CDATA[", "").replace("]]>", "")
    cleaned = _strip_at_rules(cleaned)
    variables: Dict[str, str] = {}
    rules: Dict[Tuple[str, str], Dict[str, str]] = {}
    for match in _CSS_RULE.finditer(cleaned):
        declarations = _parse_declarations(match.group(2))
        for selector in match.group(1).split(","):
            selector = selector.strip()
            if not selector:
                continue
            if _is_light_root(selector):
                # 변수 블록은 이름 그대로 모은다(값 해석은 아래에서 한 번에).
                for chunk in match.group(2).split(";"):
                    if ":" not in chunk:
                        continue
                    name, _, value = chunk.partition(":")
                    name = name.strip()
                    if name.startswith("--") and value.strip():
                        variables[name] = value.strip()
                continue
            key = _class_key(selector)
            if key is not None and declarations:
                rules.setdefault(key, {}).update(declarations)
    # 변수 사슬을 먼저 풀어 둔다(--a: var(--b)).
    resolved = {
        name: _resolve_var(value, variables) for name, value in variables.items()
    }
    for _ in range(8):
        again = {
            name: _resolve_var(value, resolved) for name, value in resolved.items()
        }
        if again == resolved:
            break
        resolved = again
    return resolved, rules


def _attr_text(value: str) -> str:
    """속성값에 쓸 수 있게 다듬는다(따옴표 충돌 방지)."""
    return str(value).replace('"', "'").strip()


def normalize(svg_text: str) -> str:
    """마크 SVG를 정규화한다 — `<style>`·`var()`·`class` 없이도 같게 보이도록.

    순수 함수다. 같은 입력이면 항상 같은 출력이고, 파일·프로세스·시각을 쓰지 않는다.
    """
    text = str(svg_text)
    styles = _STYLE_BLOCK.findall(text)
    if styles:
        text = _STYLE_BLOCK.sub("", text)
    variables, rules = parse_css("\n".join(styles))

    def rewrite(match: "re.Match[str]") -> str:
        name, blob, closing = match.group(1), match.group(2), match.group(3)
        attrs: List[List[str]] = []
        for attr in _ATTR.finditer(blob):
            value = attr.group(2) if attr.group(2) is not None else attr.group(3)
            quote = '"' if attr.group(2) is not None else "'"
            attrs.append([attr.group(1), value, quote])
        values = {row[0]: row[1] for row in attrs}

        declarations: Dict[str, str] = {}
        for cls in str(values.get("class") or "").split():
            declarations.update(rules.get(("*", cls), {}))
            declarations.update(rules.get((name.lower(), cls), {}))
        declarations.update(_parse_declarations(str(values.get("style") or "")))

        rows: List[List[str]] = []
        index: Dict[str, int] = {}
        for attr_name, attr_value, quote in attrs:
            if attr_name == "class":  # 규칙을 속성으로 펼쳤으니 class는 죽은 정보다
                continue
            index[attr_name] = len(rows)
            rows.append([attr_name, attr_value, quote])
        for prop in PROPERTIES:
            if prop not in declarations:
                continue
            value = _attr_text(_resolve_var(declarations[prop], variables))
            if prop in index:
                rows[index[prop]][1] = value
            else:
                index[prop] = len(rows)
                rows.append([prop, value, '"'])

        for row in rows:
            row[1] = _attr_text(_resolve_var(row[1], variables))
        rendered = "".join(f" {n}={q}{v}{q}" for n, v, q in rows)
        return f"<{name}{rendered}{closing}>"

    return _TAG.sub(rewrite, text)


def _without_comments(svg_text: str) -> str:
    """주석 안의 `<text>`·`<style>`·`var(`는 렌더에 영향이 없다 — 세지 않는다."""
    return re.sub(r"<!--.*?-->", "", str(svg_text), flags=re.S)


def svg_stats(svg_text: str) -> Dict[str, int]:
    """정규화 전/후를 비교하는 지표 — 게이트와 meta에 그대로 남긴다."""
    text = _without_comments(svg_text)
    return {
        "text_elements": len(_TEXT_OPEN.findall(text)),
        "style_elements": len(_STYLE_BLOCK.findall(text)),
        "css_var_refs": text.count("var("),
        "class_attributes": len(_CLASS_ATTR.findall(text)),
    }


def normalize_report(before: str, after: str) -> Dict[str, Any]:
    return {"before": svg_stats(before), "after": svg_stats(after)}


# ---------------------------------------------------------------------------
# spec 정규화
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    if slug:
        return slug
    # 한글 등 비ASCII 이름은 해시로 안정적인 슬러그를 만든다(충돌 방지).
    return "brand-" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:6]


def _color(value: Any, key: str) -> str:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", text)
    if not match:
        raise base.ProfileError(
            f"brand profile: palette['{key}']는 hex 색이어야 한다 — {value!r}"
        )
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(char * 2 for char in digits)
    return f"#{digits}"


def _sizes(value: Any) -> List[int]:
    if not isinstance(value, (list, tuple)):
        raise base.ProfileError(
            f"brand profile: sizes는 정수 목록이어야 한다 — {value!r}"
        )
    sizes: List[int] = []
    for item in value:
        try:
            size = int(item)
        except (TypeError, ValueError):
            raise base.ProfileError(
                f"brand profile: sizes에 숫자가 아닌 값이 있다 — {item!r}"
            ) from None
        if size <= 0 or size > 4096:
            raise base.ProfileError(
                f"brand profile: sizes는 1~4096px여야 한다 — {size}"
            )
        if size not in sizes:
            sizes.append(size)
    if not sizes:
        raise base.ProfileError(
            "brand profile: sizes가 비었다 — [16, 32, 64, 256, 512]"
        )
    return sorted(sizes)


def _backgrounds(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        raise base.ProfileError(
            f"brand profile: backgrounds는 목록이어야 한다 — {value!r}"
        )
    rows: List[str] = []
    for item in value:
        name = str(item).strip().lower()
        if name not in BACKGROUNDS:
            raise base.ProfileError(
                f"brand profile: backgrounds는 {'|'.join(BACKGROUNDS)} 중 하나다 — {item!r}"
            )
        if name not in rows:
            rows.append(name)
    if not rows:
        raise base.ProfileError("brand profile: backgrounds가 비었다 — light·dark")
    return rows


def brand_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """`spec['brand']`를 검증·정규화한다. 누락은 실행 전에 멈춘다."""
    brand = spec.get("brand")
    if not isinstance(brand, Mapping):
        raise base.ProfileError(
            "brand profile: spec['brand']가 필요하다 — "
            "mark_path(SVG)·palette·variants·name을 담은 사전을 넣어라"
        )
    raw_mark = brand.get("mark_path")
    if not raw_mark:
        raise base.ProfileError(
            "brand profile: spec['brand']['mark_path']가 필요하다 — "
            "VectorCraft/DesignCraft에서 내보낸 SVG 경로를 넣어라"
        )
    mark = Path(str(raw_mark))
    if not mark.is_file():
        raise base.ProfileError(f"brand profile: mark_path 파일이 없다 — {mark}")
    if mark.suffix.lower() != ".svg":
        raise base.ProfileError(
            f"brand profile: mark_path는 SVG여야 한다 — {mark.suffix or '확장자 없음'}: {mark}"
        )

    palette_in = brand.get("palette") or {}
    if not isinstance(palette_in, Mapping):
        raise base.ProfileError(
            f"brand profile: palette는 사전이어야 한다 — {palette_in!r}"
        )
    unknown = sorted(set(palette_in) - set(DEFAULT_PALETTE))
    if unknown:
        raise base.ProfileError(
            f"brand profile: palette 키는 ink·paper·accent·calm만 쓴다 — 알 수 없음: "
            f"{', '.join(unknown)}"
        )
    palette = {
        key: _color(palette_in.get(key, default), key)
        for key, default in DEFAULT_PALETTE.items()
    }

    variants = brand.get("variants")
    if variants is None:
        raw_sizes: Any = brand.get("sizes", DEFAULT_SIZES)
        raw_backgrounds: Any = brand.get("backgrounds", DEFAULT_BACKGROUNDS)
    elif isinstance(variants, Mapping):
        raw_sizes = variants.get("sizes", DEFAULT_SIZES)
        raw_backgrounds = variants.get("backgrounds", DEFAULT_BACKGROUNDS)
    elif isinstance(variants, (list, tuple)):
        raw_sizes = variants
        raw_backgrounds = brand.get("backgrounds", DEFAULT_BACKGROUNDS)
    else:
        raise base.ProfileError(
            "brand profile: variants는 {sizes, backgrounds} 사전이거나 사이즈 목록이다"
        )

    name = _slug(brand.get("name") or mark.stem)
    pdf = brand.get("pdf", spec.get("pdf", True)) is not False
    return {
        "name": name,
        "mark_path": str(mark),
        "palette": palette,
        "sizes": _sizes(raw_sizes),
        "backgrounds": _backgrounds(raw_backgrounds),
        "pdf": pdf,
        "sha256": core_artifacts.sha256_file(mark),
    }


# ---------------------------------------------------------------------------
# 아웃라인(text → path)
# ---------------------------------------------------------------------------


def outline(
    normalized: Path,
    out: Path,
    *,
    run: Optional[Any] = None,
    vectorcraft: Optional[str] = None,
) -> Path:
    """`vectorcraft-cli convert IN OUT --outline-text`. 실패는 실행 전에 멈춘다."""
    binary = vectorcraft or core_env.VECTORCRAFT
    runner = run or base.default_run
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        runner(
            [binary, "convert", str(normalized), str(out), "--outline-text"],
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        raise base.ProfileError(
            "brand profile: vectorcraft-cli로 텍스트를 아웃라인하지 못했다 — "
            "`vectorcraft-cli`가 PATH에 있는지, VECTORCRAFT_BIN이 올바른지 확인하라. "
            f"입력: {Path(normalized).name}: {_tail(exc)}"
        ) from exc
    if not Path(out).is_file():
        raise base.ProfileError(
            f"brand profile: 아웃라인 결과가 없다 — vectorcraft-cli convert 출력을 확인하라: {out}"
        )
    return Path(out)


# ---------------------------------------------------------------------------
# 게이트
# ---------------------------------------------------------------------------


def _ok(gate_id: str, detail: str) -> Dict[str, Any]:
    return {"id": gate_id, "ok": True, "detail": detail, "skipped": False}


def _fail(gate_id: str, detail: str) -> Dict[str, Any]:
    return {"id": gate_id, "ok": False, "detail": detail, "skipped": False}


def _gate_no_text(svg: Path) -> Dict[str, Any]:
    text = _without_comments(Path(svg).read_text(encoding="utf-8", errors="replace"))
    count = len(_TEXT_OPEN.findall(text))
    if count:
        return _fail(
            "brand.no_text",
            f"<text>가 {count}개 남아 있다 — 아웃라인 단계"
            "(vectorcraft-cli convert --outline-text)가 적용되지 않았다: "
            f"{Path(svg).name}",
        )
    return _ok("brand.no_text", "텍스트 0개(아웃라인 완료)")


def _gate_no_style(svg: Path) -> Dict[str, Any]:
    text = _without_comments(Path(svg).read_text(encoding="utf-8", errors="replace"))
    styles = len(_STYLE_BLOCK.findall(text))
    variables = text.count("var(")
    if styles or variables:
        return _fail(
            "brand.no_style",
            f"<style> {styles}개 · var() {variables}개가 남아 있다 — "
            "normalize()를 거치지 않은 SVG다(:root 변수·클래스 규칙을 펼쳐야 한다): "
            f"{Path(svg).name}",
        )
    return _ok("brand.no_style", "style 0개 · var() 0개(정규화 완료)")


def _opaque_ratio(png: Path) -> float:
    """불투명 픽셀 비율. 알파가 없으면 전부 불투명(1.0)."""
    from PIL import Image

    with Image.open(png) as image:
        rgba = image.convert("RGBA")
        total = rgba.width * rgba.height
        if total <= 0:
            return 0.0
        alpha = rgba.getchannel("A")
        # histogram()[0] = 완전 투명 픽셀 수 — getdata()는 Pillow 14에서 사라진다.
        opaque = total - alpha.histogram()[0]
        return opaque / total


def _svg_geometry(svg: Path) -> Tuple[float, float]:
    """width/height, 없으면 viewBox. 둘 다 없으면 1200×900."""
    head = Path(svg).read_text(encoding="utf-8", errors="replace")[:600]
    width = re.search(r"<svg[^>]*\bwidth\s*=\s*\"([0-9.]+)", head)
    height = re.search(r"<svg[^>]*\bheight\s*=\s*\"([0-9.]+)", head)
    if width and height:
        w, h = float(width.group(1)), float(height.group(1))
        if w > 0 and h > 0:
            return w, h
    box = re.search(
        r"<svg[^>]*\bviewBox\s*=\s*\"\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([0-9.]+)[\s,]+([0-9.]+)",
        head,
    )
    if box:
        w, h = float(box.group(1)), float(box.group(2))
        if w > 0 and h > 0:
            return w, h
    return 1200.0, 900.0


def _fit_scale(svg: Path, box: int) -> float:
    native_w, native_h = _svg_geometry(svg)
    scale = min(float(box) / native_w, float(box) / native_h)
    return max(0.02, min(16.0, scale))


def _gate_raster(ctx: base.ProfileContext, svg: Path, workdir: Path) -> Dict[str, Any]:
    out = Path(workdir) / "gate-256.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()
    try:
        ctx.run(
            [
                _binary(ctx),
                "convert",
                str(svg),
                str(out),
                "--scale",
                f"{_fit_scale(svg, 256):.4f}",
            ],
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "brand.raster",
            "256px 래스터화 실패 — vectorcraft-cli 설치와 SVG 무결성을 확인하라: "
            f"{_tail(exc)}",
        )
    if not out.is_file():
        return _fail(
            "brand.raster",
            f"256px 래스터 결과가 없다 — convert 출력 경로를 확인하라: {out}",
        )
    try:
        ratio = _opaque_ratio(out)
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "brand.raster",
            f"래스터 결과를 읽을 수 없다 — PNG 형식을 확인하라: {_tail(exc)}",
        )
    if ratio <= 0:
        return _fail(
            "brand.raster",
            "256px 렌더에 불투명 픽셀이 0개다 — 빈 렌더(E015). "
            "아트워크가 뷰박스 안에 있는지, fill/stroke가 투명이 아닌지 확인하라",
        )
    return _ok("brand.raster", f"불투명 픽셀 {ratio:.1%}")


def _gate_pdf(
    pdf: Path, *, required: bool, error: Optional[str] = None
) -> Dict[str, Any]:
    if not required:
        return {
            "id": "brand.pdf",
            "ok": True,
            "detail": "PDF를 요청하지 않았다(brand.pdf=false)",
            "skipped": True,
        }
    if not Path(pdf).is_file():
        detail = (
            f"출력 PDF가 없다 — vectorcraft-cli convert(PDF) 결과를 확인하라: {pdf}"
        )
        if error:
            detail += f" (실행 오류: {error})"
        return _fail("brand.pdf", detail)
    size = Path(pdf).stat().st_size
    if size <= 0:
        return _fail("brand.pdf", f"출력 PDF가 0바이트다 — 변환 로그를 확인하라: {pdf}")
    return _ok("brand.pdf", f"{Path(pdf).name} {size:,}B")


def run_gates(
    ctx: base.ProfileContext,
    *,
    svg: Path,
    pdf: Path,
    workdir: Path,
    pdf_required: bool = True,
    pdf_error: Optional[str] = None,
    allow: bool = False,
) -> List[Dict[str, Any]]:
    """게이트 4종. 실패가 있으면 `BrandGateError`(allow면 skipped로 남긴다)."""
    results = [
        _gate_no_text(svg),
        _gate_no_style(svg),
        _gate_raster(ctx, svg, workdir),
        _gate_pdf(pdf, required=pdf_required, error=pdf_error),
    ]
    failures = [item for item in results if not item["ok"]]
    if allow:
        for item in failures:
            item["skipped"] = True
    elif failures:
        raise BrandGateError(failures)
    return results


# ---------------------------------------------------------------------------
# 목업 (Pillow, 결정론 — 난수·시각 없음)
# ---------------------------------------------------------------------------


def _icon_tile_mockup(page: Path, out: Path, palette: Mapping[str, str]) -> None:
    """앱 아이콘 타일: 잉크 타일 위에 마크를 앉힌다."""
    from PIL import Image, ImageDraw

    canvas = Image.new("RGB", (1400, 900), (239, 237, 232))
    draw = ImageDraw.Draw(canvas)
    tile = (470, 120, 930, 580)
    draw.rounded_rectangle(tile, radius=96, fill=palette["ink"])
    draw.rounded_rectangle(tile, radius=96, outline=palette["calm"], width=3)

    with Image.open(page) as image:
        fitted = image.convert("RGBA")
        inner = 320
        ratio = inner / max(fitted.width, fitted.height)
        size = (
            max(1, round(fitted.width * ratio)),
            max(1, round(fitted.height * ratio)),
        )
        fitted = fitted.resize(size, Image.LANCZOS)
        x = tile[0] + ((tile[2] - tile[0]) - size[0]) // 2
        y = tile[1] + ((tile[3] - tile[1]) - size[1]) // 2
        canvas.paste(fitted, (x, y), fitted)

    draw.text((470, 620), str(palette["accent"]), fill=(60, 58, 54))
    canvas.save(out, "PNG", optimize=True)


def _card_mockup(page: Path, out: Path, label: str) -> None:
    """web profile의 카드 목업을 재사용한다. 없으면 같은 규격으로 직접 그린다."""
    try:
        from graphs.profiles.web import _card_mockup as helper
    except Exception:  # noqa: BLE001 - helper가 바뀌어도 납품은 계속된다
        helper = None
    if helper is not None:
        return helper(page, out, label)
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
        canvas.paste(
            fitted,
            (
                frame[0] + ((frame[2] - frame[0]) - size[0]) // 2,
                frame[1] + ((frame[3] - frame[1]) - size[1]) // 2,
            ),
        )
    if label:
        draw.text((170, 770), label[:80], fill=(60, 58, 54))
    canvas.save(out, "PNG", optimize=True)


def _background_color(
    palette: Mapping[str, str], background: str
) -> Tuple[int, int, int]:
    hex_value = palette["paper"] if background == "light" else palette["ink"]
    digits = hex_value.lstrip("#")[:6]
    return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def build_icon_variants(
    page: Path,
    out_dir: Path,
    *,
    name: str,
    sizes: Sequence[int],
    backgrounds: Sequence[str],
    palette: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """2000px 아트를 사이즈·배경 조합으로 줄인다(외부 프로세스 없이 결정론)."""
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    with Image.open(page) as source:
        art = source.convert("RGBA")
        for background in backgrounds:
            color = _background_color(palette, background)
            for size in sizes:
                ratio = min(float(size) / art.width, float(size) / art.height)
                box = (
                    max(1, min(size, round(art.width * ratio))),
                    max(1, min(size, round(art.height * ratio))),
                )
                fitted = art.resize(box, Image.LANCZOS)
                tile = Image.new("RGB", (size, size), color)
                tile.paste(
                    fitted,
                    ((size - box[0]) // 2, (size - box[1]) // 2),
                    fitted,
                )
                path = out_dir / f"{name}-{size}-{background}.png"
                tile.save(path, "PNG", optimize=True)
                rows.append(
                    {
                        "size": size,
                        "background": background,
                        "path": str(path),
                        "sha256": core_artifacts.sha256_file(path),
                    }
                )
    return rows


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class BrandProfile:
    """브랜드 마크 납품. 게이트를 통과한 아웃라인 SVG만 팩에 넣는다."""

    id = "brand"

    def defaults(self) -> Dict[str, Any]:
        return dict(DEFAULT_SPEC)

    def validate(self, spec: Mapping[str, Any]) -> None:
        """spec_node에서 knowledge·LLM 전에 brand 계약을 확인한다."""
        brand_spec(spec)

    def plan(
        self,
        ir: Mapping[str, Any],
        spec: Mapping[str, Any],
        styles: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        brand = brand_spec(spec)
        return [
            {
                "id": f"brand-{brand['name']}",
                "kind": "brand",
                "direction": brand["name"],
                "style": "brand",
                "theme": "brand",
                "brand": brand,
            }
        ]

    def render(
        self, variant: Mapping[str, Any], ctx: base.ProfileContext
    ) -> Optional[Dict[str, Any]]:
        brand = dict(variant["brand"])
        out = Path(ctx.run_dir or ctx.out_root) / variant["id"]
        out.mkdir(parents=True, exist_ok=True)

        raw = Path(brand["mark_path"]).read_text(encoding="utf-8")
        normalized_text = normalize(raw)
        normalized = out / "normalized.svg"
        normalized.write_text(normalized_text, encoding="utf-8")

        outlined = outline(
            normalized,
            out / "outlined.svg",
            run=ctx.run,
            vectorcraft=_binary(ctx),
        )
        outlined_text = outlined.read_text(encoding="utf-8", errors="replace")
        stats = normalize_report(raw, normalized_text)
        stats["outlined"] = svg_stats(outlined_text)

        return {
            **variant,
            "svg": str(outlined),
            "normalized": str(normalized),
            "outlined": str(outlined),
            "stats": stats,
        }

    def gates(self) -> List[base.Gate]:
        return list(GATES)

    def pack(
        self,
        items: Sequence[Mapping[str, Any]],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> Dict[str, Any]:
        ctx = _context(state)
        brand = brand_spec(spec)
        if not items:
            raise base.ProfileError(
                "brand profile: 선택된 항목이 없다 — selection이 비었거나 렌더가 사라졌다"
            )
        selection = list(state.get("selection") or [])
        if selection and len(items) < len(selection):
            raise base.ProfileError(
                f"brand profile: selection {len(selection)}건 중 {len(items)}건만 렌더가 있다 — "
                "렌더를 다시 만든 뒤 pack을 실행하라"
            )

        run_dir = Path(ctx.run_dir or ctx.out_root)
        pack_dir = run_dir / "pack"
        pack_dir.mkdir(parents=True, exist_ok=True)
        icon_dir = pack_dir / "icons"

        item = dict(items[0])
        source = Path(item.get("normalized") or item["svg"])
        normalized = pack_dir / "normalized.svg"
        if source.resolve() != normalized.resolve():
            shutil.copyfile(source, normalized)
        outlined_path = Path(item["outlined"]) if item.get("outlined") else None
        outlined = pack_dir / "outlined.svg"
        if outlined_path is not None and outlined_path.is_file():
            shutil.copyfile(outlined_path, outlined)
        else:
            outline(
                normalized,
                outlined,
                run=ctx.run,
                vectorcraft=_binary(ctx),
            )

        width = int(spec.get("image_width", 2000))
        max_height = int(spec.get("max_height", 3000))
        quality = int(spec.get("jpg_quality", 90))
        # 테스트 우회는 명시적 bool true만 — 문자열 "false"가 게이트를 끄지 않게 한다.
        allow = spec.get("allow_brand_checks") is True

        pdf = pack_dir / "mark.pdf"
        pdf_error: Optional[str] = None
        if brand["pdf"]:
            try:
                ctx.run(
                    [
                        _binary(ctx),
                        "convert",
                        str(outlined),
                        str(pdf),
                    ],
                    timeout=180,
                )
            except Exception as exc:  # noqa: BLE001
                pdf_error = _tail(exc)

        gates = run_gates(
            ctx,
            svg=outlined,
            pdf=pdf,
            # 게이트 래스터는 팩 밖에 둔다(납품물이 아니다).
            workdir=pack_dir.parent / "gate",
            pdf_required=brand["pdf"],
            pdf_error=pdf_error,
            allow=allow,
        )

        png = pack_dir / f"mark-{width}.png"
        ctx.convert(outlined, png, width, max_height)
        jpg = pack_dir / "mark.jpg"
        ctx.jpeg(png, jpg, quality)

        icons = build_icon_variants(
            png,
            icon_dir,
            name=brand["name"],
            sizes=brand["sizes"],
            backgrounds=brand["backgrounds"],
            palette=brand["palette"],
        )

        artifacts: Dict[str, Any] = {}
        paths: List[Path] = [normalized, outlined, png, jpg]
        if pdf.is_file():
            paths.append(pdf)
        paths += [Path(row["path"]) for row in icons]
        for path in paths:
            if not path.is_file():
                continue
            artifacts[str(path.relative_to(pack_dir))] = {
                "sha256": core_artifacts.sha256_file(path),
                "size": path.stat().st_size,
            }

        gates_ok = all(gate["ok"] for gate in gates)
        meta = {
            "profile": "brand",
            "name": brand["name"],
            "mark_path": brand["mark_path"],
            "mark_sha256": brand["sha256"],
            "inputs_sha256": core_artifacts.sha256_json(
                {
                    "mark_sha256": brand["sha256"],
                    "palette": brand["palette"],
                    "sizes": brand["sizes"],
                    "backgrounds": brand["backgrounds"],
                    "image_width": width,
                    "max_height": max_height,
                    "jpg_quality": quality,
                    "pdf": brand["pdf"],
                }
            ),
            "palette": brand["palette"],
            "sizes": brand["sizes"],
            "backgrounds": brand["backgrounds"],
            "image_width": width,
            "max_height": max_height,
            "jpg_quality": quality,
            "pdf_requested": brand["pdf"],
            "allow_brand_checks": allow,
            "gates_ok": gates_ok,
            "gates": gates,
            "normalization": dict(item.get("stats") or {}),
            "artifacts": artifacts,
            "icons": icons,
        }
        meta_path = pack_dir / "meta.json"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        lines = [f"# 브랜드 팩 — {brand['name']}", ""]
        lines += [
            f"- 마크: `{Path(brand['mark_path']).name}` (`{brand['sha256'][:16]}`)",
            f"- 팔레트: "
            + " · ".join(f"{key} {value}" for key, value in brand["palette"].items()),
            f"- 아이콘: {brand['sizes']} × {'/'.join(brand['backgrounds'])}",
            f"- 입력 지문: `{meta['inputs_sha256'][:16]}`",
            "",
            "## 게이트",
        ]
        for gate in gates:
            mark = "✓" if gate["ok"] else ("−" if gate.get("skipped") else "✗")
            lines.append(f"- {mark} {gate['id']}: {gate['detail']}")
        lines += ["", "## 산출물"]
        for name, row in artifacts.items():
            lines.append(f"- `{name}` ({row['size']:,}B)")
        entry_path = pack_dir / "entry.md"
        entry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        packed = [
            {
                "id": item["id"],
                "name": brand["name"],
                "normalized": str(normalized),
                "outlined": str(outlined),
                "pdf": str(pdf) if pdf.is_file() else None,
                "png": str(png),
                "jpg": str(jpg),
                "png_size": png.stat().st_size,
                "jpg_size": jpg.stat().st_size,
                "pdf_size": pdf.stat().st_size if pdf.is_file() else 0,
                "sha256": core_artifacts.sha256_file(png),
                "gates": gates,
                "icons": icons,
            }
        ]

        artifact_paths: List[Any] = list(paths) + [meta_path, entry_path]
        manifest = base.run_manifest(
            {**state, "spec": spec},
            run_dir,
            base.collect_artifacts(artifact_paths, run_dir),
            kind="brand",
            status="ok" if gates_ok else "warn",
            gates=gates,
        )
        manifest_path = core_artifacts.write_manifest(run_dir, manifest)

        return {
            "dir": str(pack_dir),
            "items": packed,
            "meta": str(meta_path),
            "manifest": str(manifest_path),
            "manifest_digest": core_artifacts.manifest_digest(manifest),
            "gates": gates,
        }

    def mockups(
        self, items: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
    ) -> List[Dict[str, Any]]:
        if not items:
            return []
        brand = brand_spec(spec)
        mock_dir = Path(
            spec.get("mockup_dir") or Path(items[0]["png"]).parent / "mockups"
        )
        mock_dir.mkdir(parents=True, exist_ok=True)

        rows: List[Dict[str, Any]] = []
        for item in items:
            card = mock_dir / f"{item['id']}-card.png"
            _card_mockup(Path(item["png"]), card, f"{brand['name']} · brand mark")
            tile = mock_dir / f"{item['id']}-tile.png"
            _icon_tile_mockup(Path(item["png"]), tile, brand["palette"])
            rows.append(
                {
                    "id": item["id"],
                    "card": str(card),
                    "tile": str(tile),
                    "card_sha256": core_artifacts.sha256_file(card),
                    "tile_sha256": core_artifacts.sha256_file(tile),
                }
            )
        return rows
