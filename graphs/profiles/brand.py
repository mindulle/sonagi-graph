"""brand profile — 브랜드/아이덴티티 엔트리 (GRAPH-18, P4b · GRAPH-33 explore).

두 모드가 있다.

- `mode="existing"`(기본): 마크는 **사용자가 만든 SVG**다. 그래프는 마크를 새로
  그리지 않고, 납품 가능한 상태로 정규화·아웃라인·게이트·포장한다.
- `mode="explore"`: 브리프에서 **새 마크 방향**을 만든다. `graphs/brand_marks.py`가
  시드 결정론으로 monogram·seal·radial 후보를 VectorCraft headless로 그리고,
  후보마다 같은 게이트를 건 뒤 최소 2개 통과를 요구한다. 선택된 하나는 기존
  파이프라인으로 아이콘·락업·PDF 킷이 된다.
- `mode="refine"`: **기존 마스터 인장을 정련**한다. `source_mark`(SVG)의 링 비율·
  방사 각도·코어 비율을 읽고(`graphs/brand_marks_v2.py`), 링 수·획 굵기·씨앗
  밀도·컷 깊이만 바꾼 후보 8~12개를 만든다. 자동 선택은 하지 않는다 — HITL에서
  사람이 고른다.

```
mark.svg (existing)
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

explore/refine은 앞단만 다르다: `brief → 후보 계획 → VectorCraft → 위 normalize/outline`.
후보 게이트는 기존 3종에 `palette`·`safe_area`·`single_color`·`legible_16`을 더하고,
refine은 제작 규칙 5종(획 배율·오버슈트·색 역할·그라디언트·카운터)을 더한다.

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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from graphs import brand_marks
from graphs import brand_marks_v2
from graphs.core import artifacts as core_artifacts
from graphs.core import env as core_env
from graphs.profiles import base

DEFAULT_SIZES: Tuple[int, ...] = (16, 32, 64, 256, 512)
DEFAULT_BACKGROUNDS: Tuple[str, ...] = ("light", "dark")
BACKGROUNDS = ("light", "dark")

#: 브랜드 모드. `existing`은 지금까지의 동작(사용자 SVG), `explore`는 후보 생성,
#: `refine`은 기존 마스터 인장의 정련이다.
MODES: Tuple[str, ...] = ("existing", "explore", "refine")

#: context_test가 렌더하는 크기 — 16px 파비콘부터 앱 아이콘까지.
CONTEXT_SIZES: Tuple[int, ...] = (16, 32, 64, 256)

#: 렌더가 실패했을 때의 감점(삭제된 logo_design 그래프와 같은 값).
CRAFT_FAILURE_PENALTY = 20

DEFAULT_PALETTE: Dict[str, str] = dict(brand_marks.DEFAULT_PALETTE)

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
    # explore/refine 모드도 후보 생성이 결정론이라 지식·LLM이 필요 없다.
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

#: explore 후보 하나에 추가로 거는 검사. 앞 3개는 기존 게이트를 그대로 재사용한다.
CANDIDATE_GATES: Tuple[base.Gate, ...] = (
    base.Gate("candidate.palette", "색이 모두 넘겨받은 팔레트 안에 있다"),
    base.Gate("candidate.safe_area", "256px 렌더의 사방 여백이 4% 이상이다"),
    base.Gate("candidate.single_color", "단색 변형이 색 1개로 렌더된다"),
    base.Gate("candidate.legible_16", "16px 단색 래스터에 형태가 남는다"),
    base.Gate("brand.lockup", "락업 SVG·PDF가 있고 SVG에 텍스트가 없다"),
)

#: 후보 렌더가 반드시 들고 있어야 하는 게이트 id. 부분 상태를 통과로 오인하지 않는다.
REQUIRED_CANDIDATE_GATE_IDS = frozenset(
    {
        "brand.no_text",
        "brand.no_style",
        "brand.raster",
        "candidate.palette",
        "candidate.safe_area",
        "candidate.single_color",
        "candidate.legible_16",
    }
)

#: refine 후보는 여기에 제작 규칙 5종(획 배율·오버슈트·색 역할·그라디언트·카운터)을
#: 더 요구한다. 하나라도 빠지면 통과로 세지 않는다.
REQUIRED_REFINE_GATE_IDS = REQUIRED_CANDIDATE_GATE_IDS | set(
    brand_marks_v2.REFINE_GATE_IDS
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

#: context_test — 구조·가독성 검사용 패턴.
_SHAPE = re.compile(r"<(circle|rect|path|polygon|ellipse|line|g|use)\b")
_VIEWBOX = re.compile(r'viewBox="([^"]+)"')
_STROKE_WIDTH = re.compile(r'stroke-width="([0-9.]+)"')
_PAINT = re.compile(r'\b(fill|stroke)="([^"]+)"')
#: 색이 아닌 채우기 값 — 개수에서 뺀다.
_NOT_A_COLOR = {"none", "transparent", "currentcolor", "inherit", "unset"}


class BrandGateError(RuntimeError):
    """브랜드 게이트 실패. 어떤 검사가 왜 막았는지 그대로 들고 있다."""

    def __init__(self, failures: Sequence[Mapping[str, Any]]):
        self.failures = [dict(item) for item in failures]
        lines = [f"브랜드 게이트 실패 {len(self.failures)}건 — 납품을 중단한다."]
        for item in self.failures:
            lines.append(f"- {item.get('id')}: {item.get('detail')}")
        lines.append("테스트에서만 spec['allow_brand_checks']=true로 건너뛸 수 있다.")
        super().__init__("\n".join(lines))


class CandidateGateError(RuntimeError):
    """explore 후보가 최소 통과 수를 못 채웠다. 후보별 사유를 전부 들고 있다."""

    def __init__(
        self,
        passing: int,
        total: int,
        failures: Sequence[Mapping[str, Any]],
        *,
        minimum: int = brand_marks.MIN_PASSING,
    ):
        self.passing = int(passing)
        self.total = int(total)
        self.failures = [dict(item) for item in failures]
        lines = [
            f"brand explore: 게이트를 통과한 후보가 {passing}개다 — "
            f"최소 {minimum}개가 필요하다(후보 {total}개).",
            "브리프를 더 단순한 형태로 바꾸거나 candidates를 늘린 뒤 다시 실행하라.",
        ]
        for item in self.failures:
            lines.append(
                f"- {item.get('candidate')}: "
                + ", ".join(item.get("reasons") or [item.get("detail", "")])
            )
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


def _mode(brand: Mapping[str, Any]) -> str:
    mode = str(brand.get("mode") or "existing").strip().lower()
    if mode not in MODES:
        raise base.ProfileError(
            f"brand profile: mode는 {'|'.join(MODES)} 중 하나다 — {brand.get('mode')!r}"
        )
    return mode


def _brief_text(brand: Mapping[str, Any], spec: Mapping[str, Any]) -> Tuple[str, str]:
    """explore 브리프와 개념 텍스트. 브리프가 없으면 concept을 브리프로 쓴다."""
    concept = brand.get("concept")
    if isinstance(concept, Mapping):
        concept_text = " ".join(
            str(concept.get(key))
            for key in ("title", "description", "visual_language")
            if concept.get(key)
        ).strip()
    else:
        concept_text = str(concept or "").strip()
    brief = str(brand.get("brief") or spec.get("brief") or concept_text).strip()
    return brief, concept_text


def _candidate_count(value: Any) -> int:
    if isinstance(value, bool):
        raise base.ProfileError(
            f"brand profile: candidates는 2~{brand_marks.MAX_CANDIDATES} 정수여야 한다 — {value!r}"
        )
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        count = int(value)
        if isinstance(value, str) and value.strip() != str(count):
            raise ValueError
    except (TypeError, ValueError):
        raise base.ProfileError(
            f"brand profile: candidates는 2~{brand_marks.MAX_CANDIDATES} 정수여야 한다 — {value!r}"
        ) from None
    if count < brand_marks.MIN_PASSING or count > brand_marks.MAX_CANDIDATES:
        raise base.ProfileError(
            f"brand profile: candidates는 2~{brand_marks.MAX_CANDIDATES}개여야 한다 — "
            f"{count}(게이트 통과 최소 {brand_marks.MIN_PASSING}개)"
        )
    return count


def _refine_count(value: Any) -> int:
    """refine 후보 수 — 8~12만 허용한다(정련은 비교할 폭이 필요하다)."""
    if isinstance(value, bool):
        raise base.ProfileError(
            f"brand profile: refine candidates는 "
            f"{brand_marks_v2.MIN_CANDIDATES}~{brand_marks_v2.MAX_CANDIDATES} "
            f"정수여야 한다 — {value!r}"
        )
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        count = int(value)
        if isinstance(value, str) and value.strip() != str(count):
            raise ValueError
    except (TypeError, ValueError):
        raise base.ProfileError(
            f"brand profile: refine candidates는 "
            f"{brand_marks_v2.MIN_CANDIDATES}~{brand_marks_v2.MAX_CANDIDATES} "
            f"정수여야 한다 — {value!r}"
        ) from None
    if count < brand_marks_v2.MIN_CANDIDATES or count > brand_marks_v2.MAX_CANDIDATES:
        raise base.ProfileError(
            f"brand profile: refine candidates는 "
            f"{brand_marks_v2.MIN_CANDIDATES}~{brand_marks_v2.MAX_CANDIDATES}개여야 한다 — "
            f"{count}"
        )
    return count


def _refine_families(value: Any) -> List[str]:
    """refine 가족 목록. 기본은 전체, 모르는 이름은 실행 전에 막는다."""
    if value is None:
        return list(brand_marks_v2.FAMILIES)
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise base.ProfileError(
            f"brand profile: refine families는 가족 이름 목록이어야 한다 — {value!r}"
        )
    rows: List[str] = []
    for item in value:
        name = str(item).strip().lower()
        if name not in brand_marks_v2.FAMILIES:
            raise base.ProfileError(
                f"brand profile: 알 수 없는 refine 가족 {item!r} — "
                f"{', '.join(brand_marks_v2.FAMILIES)} 중에서 골라라"
            )
        if name not in rows:
            rows.append(name)
    if not rows:
        raise base.ProfileError(
            "brand profile: refine families가 비었다 — "
            f"{', '.join(brand_marks_v2.FAMILIES)} 중에서 골라라"
        )
    return rows


def _display_name(brand: Mapping[str, Any], brief: str) -> str:
    """락업에 쓸 표시 이름. 없으면 브리프의 첫 구절을 쓰고 길이를 제한한다."""
    raw = str(brand.get("name") or "").strip()
    if not raw:
        raw = re.split(r"[—–·:|,.;]", str(brief))[0].strip() or str(brief).strip()
    return raw[:40].strip()


def brand_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """`spec['brand']`를 검증·정규화한다. 누락은 실행 전에 멈춘다.

    `mode="existing"`(기본)은 지금까지처럼 `mark_path`가 필수다.
    `mode="explore"`는 브리프(`brief` 또는 `concept`)에서 후보를 만들므로
    `mark_path`가 선택이고 `candidates`(기본 6, 최대 12)를 받는다.
    `mode="refine"`은 `source_mark`(마스터 인장 SVG)가 필수이고 후보 8~12개를
    만들며, `families`로 가족을 고를 수 있다.
    """
    brand = spec.get("brand")
    if not isinstance(brand, Mapping):
        raise base.ProfileError(
            "brand profile: spec['brand']가 필요하다 — "
            "mark_path(SVG)·palette·variants·name을 담은 사전을 넣어라"
        )
    mode = _mode(brand)

    raw_mark = brand.get("mark_path")
    mark: Optional[Path] = None
    if mode == "existing":
        if not raw_mark:
            raise base.ProfileError(
                "brand profile: spec['brand']['mark_path']가 필요하다 — "
                "VectorCraft/DesignCraft에서 내보낸 SVG 경로를 넣어라. "
                "새 마크 방향을 만들려면 spec['brand']['mode']='explore'와 brief를 넣어라"
            )
        mark = Path(str(raw_mark))
        if not mark.is_file():
            raise base.ProfileError(f"brand profile: mark_path 파일이 없다 — {mark}")
        if mark.suffix.lower() != ".svg":
            raise base.ProfileError(
                f"brand profile: mark_path는 SVG여야 한다 — "
                f"{mark.suffix or '확장자 없음'}: {mark}"
            )
    elif raw_mark:
        # explore/refine에서도 참고 마크를 줄 수 있다. 있으면 형식만 검사한다.
        mark = Path(str(raw_mark))
        if not mark.is_file():
            raise base.ProfileError(f"brand profile: mark_path 파일이 없다 — {mark}")
        if mark.suffix.lower() != ".svg":
            raise base.ProfileError(
                f"brand profile: mark_path는 SVG여야 한다 — "
                f"{mark.suffix or '확장자 없음'}: {mark}"
            )

    source: Optional[Path] = None
    if mode == "refine":
        raw_source = brand.get("source_mark")
        if not raw_source:
            raise base.ProfileError(
                "brand profile: refine 모드는 source_mark가 필요하다 — "
                "정련할 마스터 인장 SVG 경로를 spec['brand']['source_mark']에 넣어라"
            )
        source = Path(str(raw_source))
        if not source.is_file():
            raise base.ProfileError(
                f"brand profile: source_mark 파일이 없다 — {source}"
            )
        if source.suffix.lower() != ".svg":
            raise base.ProfileError(
                f"brand profile: source_mark는 SVG여야 한다 — "
                f"{source.suffix or '확장자 없음'}: {source}"
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

    pdf = brand.get("pdf", spec.get("pdf", True)) is not False
    sizes = _sizes(raw_sizes)
    backgrounds = _backgrounds(raw_backgrounds)

    if mode == "refine":
        brief, concept_text = _brief_text(brand, spec)
        if not brief:
            raise base.ProfileError(
                "brand profile: refine 모드는 brief가 필요하다 — "
                "spec['brand']['brief']에 정련 방향을 한 문장 이상 적어라"
            )
        count = _refine_count(
            brand.get("candidates", brand_marks_v2.DEFAULT_CANDIDATES)
        )
        families = _refine_families(brand.get("families"))
        display = _display_name(brand, brief)
        seed_hint = str(brand.get("seed") or spec.get("seed") or "").strip()
        assert source is not None  # 위에서 보장한다
        return {
            "mode": "refine",
            "name": _slug(display)[:48],
            "display_name": display,
            "brief": brief,
            "brief_sha256": brand_marks.brief_digest(brief, concept_text, seed_hint),
            "concept": concept_text,
            "candidates": count,
            "families": families,
            "seed": seed_hint,
            "source_mark": str(source),
            "source_sha256": core_artifacts.sha256_file(source),
            "mark_path": None,
            "palette": palette,
            "sizes": sizes,
            "backgrounds": backgrounds,
            "pdf": pdf,
        }

    if mode == "explore":
        brief, concept_text = _brief_text(brand, spec)
        if not brief:
            raise base.ProfileError(
                "brand profile: explore 모드는 brief가 필요하다 — "
                "spec['brand']['brief'] 또는 spec['brand']['concept'](또는 spec['brief'])에 "
                "브랜드 방향을 한 문장 이상 적어라"
            )
        count = _candidate_count(
            brand.get("candidates", brand_marks.DEFAULT_CANDIDATES)
        )
        display = _display_name(brand, brief)
        seed_hint = str(brand.get("seed") or spec.get("seed") or "").strip()
        return {
            "mode": "explore",
            "name": _slug(display)[:48],
            "display_name": display,
            "brief": brief,
            "brief_sha256": brand_marks.brief_digest(brief, concept_text, seed_hint),
            "concept": concept_text,
            "candidates": count,
            "seed": seed_hint,
            "mark_path": str(mark) if mark is not None else None,
            "palette": palette,
            "sizes": sizes,
            "backgrounds": backgrounds,
            "pdf": pdf,
        }

    assert mark is not None  # existing 모드는 위에서 보장한다
    name = _slug(brand.get("name") or mark.stem)
    return {
        "mode": "existing",
        "name": name,
        "mark_path": str(mark),
        "palette": palette,
        "sizes": sizes,
        "backgrounds": backgrounds,
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
# explore 후보 게이트 — 기존 게이트 + 팔레트·여백·단색·16px
# ---------------------------------------------------------------------------


def _gate_palette(svg: Path, palette: Mapping[str, str]) -> Dict[str, Any]:
    text = Path(svg).read_text(encoding="utf-8", errors="replace")
    colors = brand_marks.svg_colors(text)
    if not colors:
        return _fail(
            "candidate.palette",
            f"fill/stroke 색이 하나도 없다 — 도형에 팔레트 색을 지정하라: {Path(svg).name}",
        )
    allowed = {brand_marks.hex6(str(value)) for value in palette.values()}
    outside = sorted(set(colors) - allowed)
    if outside:
        return _fail(
            "candidate.palette",
            f"팔레트 밖 색 {len(outside)}개 — {', '.join(outside)}. "
            f"허용: {', '.join(sorted(allowed))}",
        )
    return _ok("candidate.palette", f"색 {len(colors)}개 모두 팔레트 안")


def _gate_safe_area(png: Path) -> Dict[str, Any]:
    if not Path(png).is_file():
        return _fail(
            "candidate.safe_area",
            f"256px 래스터가 없어 여백을 잴 수 없다 — raster 게이트를 먼저 확인하라: {png}",
        )
    try:
        stats = brand_marks.image_stats(png)
        margins = brand_marks.bbox_margins(stats)
    except Exception as exc:  # noqa: BLE001
        return _fail("candidate.safe_area", f"래스터를 읽을 수 없다 — {_tail(exc)}")
    if stats["opaque"] <= 0:
        return _fail(
            "candidate.safe_area",
            "불투명 픽셀이 없어 여백을 잴 수 없다(빈 렌더)",
        )
    minimum = 0.04
    tight = sorted(
        f"{name} {value:.1%}" for name, value in margins.items() if value < minimum
    )
    if tight:
        return _fail(
            "candidate.safe_area",
            f"사방 여백 4% 미만 — {', '.join(tight)}. 마크가 아트보드 가장자리에 닿았다",
        )
    return _ok(
        "candidate.safe_area",
        "사방 여백 "
        + " · ".join(f"{name} {value:.1%}" for name, value in margins.items()),
    )


def _render_probe(
    ctx: base.ProfileContext,
    svg: Path,
    out: Path,
    box: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """단색 SVG를 `box`px로 래스터화하고 픽셀 통계를 돌려준다."""
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
                f"{_fit_scale(svg, box):.6f}",
            ],
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"래스터화 실패 — {_tail(exc)}"
    if not out.is_file():
        return None, f"래스터 결과가 없다 — {out}"
    try:
        return brand_marks.image_stats(out), None
    except Exception as exc:  # noqa: BLE001
        return None, f"래스터를 읽을 수 없다 — {_tail(exc)}"


def _gate_single_color(
    ctx: base.ProfileContext, svg: Path, workdir: Path
) -> Dict[str, Any]:
    if not Path(svg).is_file():
        return _fail("candidate.single_color", f"단색 SVG가 없다 — {svg}")
    text = Path(svg).read_text(encoding="utf-8", errors="replace")
    colors = brand_marks.svg_colors(text)
    if len(colors) != 1:
        return _fail(
            "candidate.single_color",
            f"단색이 아니다 — 색 {len(colors)}개({', '.join(colors) or '없음'})",
        )
    stats, error = _render_probe(
        ctx, Path(svg), Path(workdir) / "gate-64-single.png", 64
    )
    if error:
        return _fail("candidate.single_color", error)
    if not stats or stats["opaque"] <= 0:
        return _fail("candidate.single_color", "단색 렌더에 불투명 픽셀이 0개다")
    return _ok(
        "candidate.single_color",
        f"색 1개({colors[0]}) · 64px 불투명 {stats['ratio']:.1%}",
    )


def _gate_legible_16(
    ctx: base.ProfileContext, svg: Path, workdir: Path
) -> Dict[str, Any]:
    stats, error = _render_probe(ctx, Path(svg), Path(workdir) / "gate-16.png", 16)
    if error:
        return _fail("candidate.legible_16", error)
    if not stats or stats["opaque"] <= 0:
        return _fail(
            "candidate.legible_16",
            "16px 렌더에 불투명 픽셀이 0개다 — 16px에서 형태가 사라진다",
        )
    bbox = stats.get("bbox")
    width = (bbox[2] - bbox[0]) if bbox else 0
    height = (bbox[3] - bbox[1]) if bbox else 0
    ratio = float(stats["ratio"])
    if ratio < 0.05:
        return _fail(
            "candidate.legible_16",
            f"16px에서 잉크가 {ratio:.1%}뿐이다(< 5%) — 획이 너무 얇다",
        )
    if ratio > 0.90:
        return _fail(
            "candidate.legible_16",
            f"16px에서 잉크가 {ratio:.1%}다(> 90%) — 형태가 뭉개진다",
        )
    if width < 6 or height < 6:
        return _fail(
            "candidate.legible_16",
            f"16px 바운딩박스가 {width}×{height}px다 — 형태가 너무 작다(< 6px)",
        )
    return _ok(
        "candidate.legible_16",
        f"16px 불투명 {ratio:.1%} · bbox {width}×{height}px",
    )


def candidate_gates(
    ctx: base.ProfileContext,
    *,
    outlined: Path,
    single_color: Path,
    workdir: Path,
    palette: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """explore 후보 하나의 게이트. 실패해도 멈추지 않고 결과 전부를 돌려준다."""
    results = [
        _gate_no_text(outlined),
        _gate_no_style(outlined),
        _gate_raster(ctx, outlined, workdir),
        _gate_palette(outlined, palette),
        _gate_safe_area(Path(workdir) / "gate-256.png"),
        _gate_single_color(ctx, single_color, workdir),
        _gate_legible_16(ctx, single_color, workdir),
    ]
    return results


def candidate_gate_summary(gates: Sequence[Mapping[str, Any]]) -> str:
    rows = list(gates or [])
    passed = sum(1 for gate in rows if gate.get("ok"))
    failed = [str(gate.get("id")) for gate in rows if not gate.get("ok")]
    text = f"{passed}/{len(rows)} 통과"
    if failed:
        text += " — 실패: " + ", ".join(failed)
    return text


def _gate_lockup(
    svg: Path, pdf: Optional[Path], *, pdf_required: bool = True
) -> Dict[str, Any]:
    if not Path(svg).is_file():
        return _fail("brand.lockup", f"락업 SVG가 없다 — {svg}")
    text = Path(svg).read_text(encoding="utf-8", errors="replace")
    count = len(_TEXT_OPEN.findall(_without_comments(text)))
    if count:
        return _fail(
            "brand.lockup",
            f"락업 SVG에 <text>가 {count}개 남아 있다 — 아웃라인 단계를 확인하라",
        )
    if pdf_required:
        if pdf is None or not Path(pdf).is_file():
            return _fail("brand.lockup", f"락업 PDF가 없다 — {pdf}")
        if Path(pdf).stat().st_size <= 0:
            return _fail("brand.lockup", f"락업 PDF가 0바이트다 — {pdf}")
    return _ok(
        "brand.lockup",
        f"{Path(svg).name} · PDF {'있음' if pdf and Path(pdf).is_file() else '생략'}",
    )


def check_candidates(
    renders: Sequence[Mapping[str, Any]],
    *,
    minimum: int = brand_marks.MIN_PASSING,
    required: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """후보 렌더를 검사해 통과 수·실패 사유를 돌려준다. 최소 미달이면 예외.

    `required`를 주면 그 id 집합을 요구한다(refine은 제작 규칙 게이트까지 포함).
    """
    required_ids = frozenset(required or REQUIRED_CANDIDATE_GATE_IDS)
    total = len(renders)
    passing = 0
    failures: List[Dict[str, Any]] = []
    for render in renders:
        gates = list(render.get("gates") or [])
        present = {str(gate.get("id")) for gate in gates}
        missing = sorted(required_ids - present)
        failed = [gate for gate in gates if not gate.get("ok")]
        if gates and not failed and not missing:
            passing += 1
            continue
        reasons = [f"{gate.get('id')}: {gate.get('detail')}" for gate in failed]
        if missing:
            reasons.append("게이트 누락: " + ", ".join(missing))
        if not reasons:
            reasons = ["게이트 결과가 없다 — 렌더가 끝나지 않았다"]
        failures.append({"candidate": render.get("id"), "reasons": reasons})
    if passing < minimum:
        raise CandidateGateError(passing, total, failures, minimum=minimum)
    return {"passing": passing, "total": total, "failures": failures}


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
    dark_page: Optional[Path] = None,
    crop: bool = False,
) -> List[Dict[str, Any]]:
    """2000px 아트를 사이즈·배경 조합으로 줄인다(외부 프로세스 없이 결정론).

    `dark_page`가 있으면 dark 배경에는 그 아트를 쓴다(explore: ink → paper 반전).
    `crop`은 아트보드 여백을 잘라 아이콘 타일을 마크에 꽉 채운다(explore 전용).
    """
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for background in backgrounds:
        source_page = (
            Path(dark_page)
            if background == "dark" and dark_page is not None
            else Path(page)
        )
        color = _background_color(palette, background)
        with Image.open(source_page) as source:
            art = source.convert("RGBA")
            if crop:
                box = art.getchannel("A").getbbox()
                if box:
                    pad = round(max(box[2] - box[0], box[3] - box[1]) * 0.06)
                    art = art.crop(
                        (
                            max(0, box[0] - pad),
                            max(0, box[1] - pad),
                            min(art.width, box[2] + pad),
                            min(art.height, box[3] + pad),
                        )
                    )
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
# context_test — 여러 크기 렌더 + 구조·가독성 검사 (GRAPH-21 흡수)
# ---------------------------------------------------------------------------


def _context_workdir(workdir: Optional[Path]) -> Path:
    if workdir is not None:
        path = Path(workdir)
        path.mkdir(parents=True, exist_ok=True)
        return path
    import tempfile

    return Path(tempfile.mkdtemp(prefix="ctx-test-"))


def _color_values(mark_svg: str) -> set:
    """fill/stroke에 실제로 지정된 색값(hex·이름 모두). none·url()은 빼다."""
    values = set()
    for _, value in _PAINT.findall(mark_svg):
        text = str(value).strip().lower()
        if not text or text in _NOT_A_COLOR or text.startswith("url("):
            continue
        values.add(text)
    return values


def _check_structure(mark_svg: str, report: Dict[str, Any]) -> int:
    """SVG 구조 검사 — 요소 수·viewBox·stroke·색상 수. 감점 합계를 돌려준다."""
    penalties = 0
    element_count = len(_SHAPE.findall(mark_svg))
    if element_count > 20:
        report["issues"].append(
            f"SVG 요소 {element_count}개 — 너무 복잡 (16px에서 뭉개짐)"
        )
        penalties += 15
    if element_count == 0:
        report["issues"].append("SVG 요소가 없음 — 형태가 없음")
        penalties += 20
    report["checks"]["element_count"] = element_count

    viewbox = _VIEWBOX.search(mark_svg)
    if viewbox:
        parts = [part for part in re.split(r"[\s,]+", viewbox.group(1).strip()) if part]
        try:
            width, height = float(parts[2]), float(parts[3])
        except (IndexError, ValueError):
            report["issues"].append("viewBox 형식을 읽을 수 없음")
            penalties += 10
        else:
            if abs(width - height) > 0.1:
                report["issues"].append(
                    f"viewBox 비정사각 ({width}x{height}) — 스케일링 왜곡 가능"
                )
                penalties += 5
            if width < 50 or height < 50:
                report["issues"].append(f"viewBox 너무 작음 ({width}x{height})")
                penalties += 10
    else:
        report["issues"].append("viewBox 없음 — 스케일링 불가")
        penalties += 10

    strokes = [float(match.group(1)) for match in _STROKE_WIDTH.finditer(mark_svg)]
    if strokes:
        minimum = min(strokes)
        if minimum < 1.5:
            report["issues"].append(f"stroke-width {minimum} < 1.5 — 16px에서 사라짐")
            penalties += 10
        if len(set(strokes)) > 1:
            report["issues"].append(
                f"stroke-width 불일치: {sorted(set(strokes))} — 일관성 부족"
            )
            penalties += 5

    colors = _color_values(mark_svg)
    report["checks"]["color_count"] = len(colors)
    if len(colors) > 3:
        report["issues"].append(f"색상 {len(colors)}개 — 로고는 1~2색이 이상적")
        penalties += 10
    if not colors:
        report["issues"].append("색상 없음 — fill/stroke 미지정")
        penalties += 15

    if element_count <= 3:
        report["checks"]["complexity"] = "simple"
    elif element_count <= 8:
        report["checks"]["complexity"] = "moderate"
    else:
        report["checks"]["complexity"] = "complex"
    return penalties


def _rgba_pixels(image: Any) -> Tuple[int, int, List[Tuple[int, int, int, int]]]:
    """Pillow 이미지 → (폭, 높이, RGBA 픽셀). `getdata()` 없이 바이트로 읽는다."""
    rgba = image.convert("RGBA")
    raw = rgba.tobytes()
    rows = [(raw[i], raw[i + 1], raw[i + 2], raw[i + 3]) for i in range(0, len(raw), 4)]
    return rgba.width, rgba.height, rows


def _check_shape_preservation(
    workdir: Path, report: Dict[str, Any], *, large: int, small: int
) -> int:
    """작은 크기로 줄여도 형태가 남는가 — 큰 크기 마스크와의 IoU."""
    penalties = 0
    big_path = Path(workdir) / f"mark_{large}.png"
    small_path = Path(workdir) / f"mark_{small}.png"
    try:
        from PIL import Image

        if not (big_path.is_file() and small_path.is_file()):
            return 0
        with Image.open(big_path) as big_raw, Image.open(small_path) as small_raw:
            width, height, big = _rgba_pixels(big_raw)
            small = small_raw.convert("RGBA").resize((width, height), Image.NEAREST)
            small_pixels = _rgba_pixels(small)[2]

        big_mask = [1 if pixel[3] > 128 else 0 for pixel in big]
        small_mask = [1 if pixel[3] > 128 else 0 for pixel in small_pixels]
        intersection = sum(1 for a, b in zip(big_mask, small_mask) if a and b)
        union = sum(1 for a, b in zip(big_mask, small_mask) if a or b)
        iou = intersection / union if union else 0.0
        report["checks"]["shape_preservation_iou"] = round(iou, 3)
        if iou < 0.5:
            report["issues"].append(
                f"{small}px 가독성 낮음 (IoU {iou:.2f} < 0.5) — 형태가 무너짐"
            )
            penalties += 25
        elif iou < 0.7:
            report["issues"].append(
                f"{small}px 가독성 보통 (IoU {iou:.2f}) — 세부가 사라짐"
            )
            penalties += 10
    except Exception as exc:  # noqa: BLE001 - 이미지가 없거나 깨졌으면 점수만 보류
        report["checks"]["shape_preservation_error"] = _tail(exc, 80)
    return penalties


def _check_contrast(workdir: Path, report: Dict[str, Any], *, large: int) -> int:
    """큰 크기 렌더에서 마크가 보이는가 — 픽셀 수·평균 휘도·대비비."""
    penalties = 0
    path = Path(workdir) / f"mark_{large}.png"
    try:
        from PIL import Image

        if not path.is_file():
            return 0
        with Image.open(path) as raw:
            _, _, pixels = _rgba_pixels(raw)
        opaque = [pixel for pixel in pixels if pixel[3] > 128]
        if len(opaque) < 100:
            report["issues"].append(f"{large}px: 마크가 거의 보이지 않음 (픽셀 부족)")
            return 30

        luminance = [
            0.2126 * pixel[0] + 0.7152 * pixel[1] + 0.0722 * pixel[2]
            for pixel in opaque
        ]
        average = sum(luminance) / len(luminance)
        if average > 180:
            report["issues"].append(
                f"마크가 밝음 (avg lum {average:.0f}) — 라이트 배경에서 안 보일 수 있음"
            )
            penalties += 10
        if average < 70:
            report["issues"].append(
                f"마크가 어두움 (avg lum {average:.0f}) — 다크 배경에서 안 보일 수 있음"
            )
            penalties += 10
        ratio = (
            average / max(255 - average, 1)
            if average > 127
            else max(255 - average, 1) / max(average, 1)
        )
        report["checks"]["contrast_ratio"] = round(ratio, 2)
        if ratio < 3.0:
            report["issues"].append(f"대비 부족 (ratio {ratio:.1f} < 3.0)")
            penalties += 15
    except Exception as exc:  # noqa: BLE001
        report["checks"]["contrast_error"] = _tail(exc, 80)
    return penalties


def _check_balance(workdir: Path, report: Dict[str, Any], *, large: int) -> int:
    """광학 균형 — 캔버스 사용률·중심 이탈."""
    penalties = 0
    path = Path(workdir) / f"mark_{large}.png"
    try:
        from PIL import Image

        if not path.is_file():
            return 0
        with Image.open(path) as raw:
            width, height, pixels = _rgba_pixels(raw)
        points = [
            (index % width, index // width)
            for index, pixel in enumerate(pixels)
            if pixel[3] > 128
        ]
        if not points:
            return 0
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        box_w = max(xs) - min(xs) + 1
        box_h = max(ys) - min(ys) + 1
        usage = (box_w * box_h) / (width * height)
        report["checks"]["canvas_usage"] = round(usage, 3)
        report["checks"]["bbox"] = [min(xs), min(ys), box_w, box_h]
        if usage < 0.15:
            report["issues"].append(f"마크가 너무 작음 (캔버스 사용 {usage:.0%} < 15%)")
            penalties += 15
        if usage > 0.95:
            report["issues"].append(f"마크가 캔버스를 넘침 ({usage:.0%}) — 여백 없음")
            penalties += 10
        center_x = (min(xs) + max(xs)) / 2
        center_y = (min(ys) + max(ys)) / 2
        offset_x = abs(center_x - width / 2) / (width / 2)
        offset_y = abs(center_y - height / 2) / (height / 2)
        if offset_x > 0.15 or offset_y > 0.15:
            report["issues"].append(
                f"광학 중심 이탈 (x={offset_x:.2f}, y={offset_y:.2f})"
            )
            penalties += 10
    except Exception as exc:  # noqa: BLE001
        report["checks"]["balance_error"] = _tail(exc, 80)
    return penalties


def context_test(
    mark_svg: Optional[str],
    *,
    ctx: Optional[base.ProfileContext] = None,
    workdir: Optional[Path] = None,
    sizes: Sequence[int] = CONTEXT_SIZES,
) -> Dict[str, Any]:
    """마크를 16/32/64/256px에서 렌더하고 가독성·구조·대비·균형을 채점한다.

    마크가 없으면 100점으로 건너뛴다(레이아웃만 검증하는 경로). 렌더·검사는
    전부 결정론이고 LLM을 쓰지 않는다. `ctx.run` 훅이 vectorcraft를 대신한다.
    """
    if not mark_svg:
        return {
            "sizes": {},
            "score": 100,
            "issues": [],
            "checks": {},
            "skipped": True,
            "note": "마크 없음 — 건너뜀",
        }

    context = ctx or base.ProfileContext(out_root=Path(core_env.out_root("entry_pack")))
    work = _context_workdir(workdir)
    svg_path = work / "mark.svg"
    svg_path.write_text(mark_svg, encoding="utf-8")

    requested = tuple(int(size) for size in sizes) or CONTEXT_SIZES
    large, small = max(requested), min(requested)
    native_w, native_h = _svg_geometry(svg_path)
    base_size = max(native_w, native_h, 1.0)

    report: Dict[str, Any] = {"sizes": {}, "score": 100, "issues": [], "checks": {}}
    penalties = 0
    for size in requested:
        out = work / f"mark_{size}.png"
        if out.exists():  # 같은 workdir 재사용 시 이전 렌더를 점수에 쓰지 않는다
            out.unlink()
        try:
            context.run(
                [
                    _binary(context),
                    "convert",
                    str(svg_path),
                    str(out),
                    "--scale",
                    f"{size / base_size:.6f}",
                ],
                timeout=60,
            )
            if out.is_file() and out.stat().st_size > 100:
                report["sizes"][size] = "ok"
            else:
                report["sizes"][size] = "blank"
                report["issues"].append(f"{size}px: 렌더 실패 또는 빈 이미지")
                penalties += 15
        except Exception as exc:  # noqa: BLE001
            report["sizes"][size] = f"error: {_tail(exc, 80)}"
            report["issues"].append(f"{size}px: {_tail(exc, 80)}")
            penalties += 20

    penalties += _check_structure(mark_svg, report)
    penalties += _check_shape_preservation(work, report, large=large, small=small)
    penalties += _check_contrast(work, report, large=large)
    penalties += _check_balance(work, report, large=large)
    report["score"] = max(0, min(100, 100 - penalties))
    return report


def critic_score(
    context_score: int,
    *,
    craft_failed: bool = False,
    penalty: int = CRAFT_FAILURE_PENALTY,
) -> int:
    """context_test 점수에 렌더 실패 감점을 적용한 최종 점수(0~100)."""
    applied = max(0, int(penalty)) if craft_failed else 0
    return max(0, min(100, int(context_score) - applied))


# ---------------------------------------------------------------------------
# print_prep — 인쇄 사양 검사 (GRAPH-21 흡수)
# ---------------------------------------------------------------------------


def print_prep(
    print_spec: Optional[Mapping[str, Any]],
    *,
    variants: Sequence[Any] = (),
    craft_status: str = "",
    mark_source: Optional[str] = None,
    mark_locked: bool = False,
) -> Dict[str, Any]:
    """인쇄 사양(도련·CMYK 프로파일)과 렌더 상태를 검사한다.

    사양이 없으면 건너뛴다(`ok=True`, `skipped=True`). 사양이 있으면 문제를
    모두 모아 `issues`에 남긴다 — 첫 번째에서 멈추지 않는다.
    """
    if not print_spec:
        return {
            "ok": True,
            "issues": [],
            "report": "인쇄 사양 없음 — 건너뜀",
            "skipped": True,
        }

    issues: List[str] = []
    status = str(craft_status or "")
    if status.startswith("error"):
        issues.append(f"asset 렌더 실패: {status[:100]}")
    if not list(variants) and not status.startswith("error"):
        issues.append("변주가 생성되지 않았습니다")
    # 문자열 "false"를 우회로 인정하지 않는다 — brand 게이트와 같은 엄격 규칙.
    if mark_source not in (None, "none") and mark_locked is not True:
        issues.append("마크가 잠기지 않았습니다")

    raw_bleed = print_spec.get("bleed_mm", 0) or 0
    try:
        bleed = float(raw_bleed)
    except (TypeError, ValueError):
        issues.append(f"bleed_mm이 숫자가 아니다 — {raw_bleed!r}")
    else:
        if bleed < 3:
            issues.append(f"bleed {bleed:g}mm < 3mm")
    if print_spec.get("cmyk") and not print_spec.get("cmyk_profile"):
        issues.append("CMYK 프로파일 미지정 (ISO Coated v2 권장)")

    return {
        "ok": not issues,
        "issues": issues,
        "report": "✅ 인쇄 준비 통과" if not issues else "⚠️ " + "; ".join(issues),
        "skipped": False,
    }


# ---------------------------------------------------------------------------
# series — N건 배치 모드 (GRAPH-21 흡수)
# ---------------------------------------------------------------------------

#: 배치가 더 진행하지 않을 때의 라우팅 이름(그래프의 END와 같은 뜻).
SERIES_STOP = "__end__"


def series_lock_mark(state: Mapping[str, Any]) -> Dict[str, Any]:
    """마크를 한 번만 확정한다. 마크 생성은 하지 않는다(`mark_path`만 읽는다)."""
    if state.get("mark_svg"):
        return {"status": "mark_already_locked"}
    if state.get("mark_source", "none") != "existing":
        return {"status": "mark_skipped"}
    path = state.get("mark_path")
    if not path or not Path(str(path)).is_file():
        raise ValueError(f"mark_path가 유효하지 않습니다: {path}")
    return {
        "mark_svg": Path(str(path)).read_text(encoding="utf-8"),
        "status": "mark_locked",
    }


def series_plan(deliverables: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """납품 목록을 정규화한다(이름·인덱스·인쇄 사양 보존). 빈 목록은 실패."""
    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(deliverables or []):
        if not isinstance(item, Mapping):
            raise base.ProfileError(
                f"brand series: deliverables[{index}]는 사전이어야 한다 — {item!r}"
            )
        rows.append(
            {
                "name": str(item.get("name") or f"deliverable-{index}"),
                "index": index,
                "print_spec": item.get("print_spec"),
            }
        )
    if not rows:
        raise base.ProfileError(
            "brand series: deliverables가 비었다 — [{name, print_spec}] 1건 이상"
        )
    return rows


def series_step(
    state: Mapping[str, Any],
    deliver: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> Dict[str, Any]:
    """현재 항목 하나를 `deliver` 콜러블로 처리하고 결과를 누적한다.

    `deliver`는 항목 payload를 받아 결과 사전을 돌려주는 함수다(보통
    `entry_pack` 그래프 실행). payload에는 `brief`·`spec`(state의 템플릿 +
    항목 `print_spec`)·`mark_path`·`mark_svg`가 실린다. 배치 순서·인덱스·HITL만
    profile이 소유한다.
    """
    index = int(state.get("current_index") or 0)
    plan = list(state.get("plan") or series_plan(state.get("deliverables") or []))
    if index >= len(plan):
        return {"status": "all_delivered"}

    item = plan[index]
    brief = str(state.get("brief") or "").strip()
    spec = dict(state.get("spec") or {})
    if item.get("print_spec"):
        spec.setdefault("print_spec", item["print_spec"])
    payload = {
        "brief": f"{brief} — {item['name']}".strip(" —"),
        "name": item["name"],
        "index": index,
        "deliverable": item,
        "spec": spec,
        "print_spec": item.get("print_spec"),
        "mark_source": "existing" if state.get("mark_svg") else "none",
        "mark_path": state.get("mark_path"),
        "mark_svg": state.get("mark_svg"),
        "content": state.get("content"),
        "max_iterations": 1,
        "approval_status": "approved",
    }
    result = dict(deliver(payload) or {})
    result.setdefault("name", item["name"])
    result.setdefault("index", index)

    results = list(state.get("results") or [])
    results.append(result)
    return {
        "results": results,
        "current_index": index + 1,
        "status": f"delivered_{item['name']}",
    }


def series_hitl(state: Mapping[str, Any]) -> Dict[str, Any]:
    """항목 하나가 끝난 뒤의 승인 게이트. 승인되면 다음 항목을 위해 초기화한다."""
    if state.get("approval_status") == "approved":
        return {"status": "approved", "approval_status": "pending"}
    index = int(state.get("current_index") or 0) - 1
    plan = list(state.get("plan") or series_plan(state.get("deliverables") or []))
    name = plan[index]["name"] if 0 <= index < len(plan) else "unknown"
    return {"approval_status": "pending", "status": f"awaiting_approval_{name}"}


def series_next(state: Mapping[str, Any]) -> str:
    """승인 후 라우팅 — 다음 항목(`deliver_one`) 또는 일괄 납품(`publish_all`)."""
    if state.get("status") != "approved":
        return SERIES_STOP
    index = int(state.get("current_index") or 0)
    plan = list(state.get("plan") or series_plan(state.get("deliverables") or []))
    if index < len(plan):
        return "deliver_one"
    return "publish_all"


def series_publish(results: Sequence[Mapping[str, Any]]) -> List[str]:
    """누적 결과 → 사람이 읽는 납품 요약."""
    return [
        f"{row.get('name', 'unknown')}: {row.get('status', 'unknown')}"
        for row in results
    ]


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

    def enrich_spec(self, spec: Mapping[str, Any], brief: str = "") -> Dict[str, Any]:
        """그래프 state의 brief를 explore/refine spec에 주입한다(다른 profile엔 없는 훅).

        `spec['brand']['brief']`가 이미 있으면 건드리지 않는다. 이 훅이 없으면
        `entry_pack`의 top-level `brief`가 profile까지 닿지 않는다.
        """
        raw_brand = spec.get("brand")
        if not isinstance(raw_brand, Mapping):
            return dict(spec)  # validate()가 친절한 ProfileError를 낸다
        brand = dict(raw_brand)
        mode = str(brand.get("mode") or "").strip().lower()
        if mode not in ("explore", "refine"):
            return dict(spec)  # existing 모드의 spec 지문을 바꾸지 않는다
        if not brand.get("brief") and not spec.get("brief"):
            brand["brief"] = str(brief or "")
        updated = {**dict(spec), "brand": brand}
        # 후보는 전부 HITL 선택지로 올린다(최대 12개). existing 기본값은
        # 건드리지 않는다 — 기존 manifest의 spec 지문이 바뀌면 안 된다.
        updated.setdefault("top_n", brand_marks.MAX_CANDIDATES)
        return updated

    def auto_select_allowed(self, spec: Mapping[str, Any]) -> bool:
        """refine은 자동 선택 금지 — HITL에서 사람이 고른다.

        `entry_pack.hitl_node`가 `approval_status=approved`만 받고 selection이
        비었을 때 자동으로 고르는 v1 동작을 refine에서만 끈다.
        """
        brand = brand_spec(spec)
        return brand["mode"] != "refine"

    def check_candidates(
        self, renders: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """explore/refine 모드에서 최소 2개 통과를 강제한다(existing은 무시)."""
        brand = brand_spec(spec)
        if brand["mode"] == "refine":
            return check_candidates(renders, required=REQUIRED_REFINE_GATE_IDS)
        if brand["mode"] != "explore":
            return None
        return check_candidates(renders)

    def plan(
        self,
        ir: Mapping[str, Any],
        spec: Mapping[str, Any],
        styles: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        brand = brand_spec(spec)
        if brand["mode"] == "refine":
            source = Path(str(brand["source_mark"]))
            model = brand_marks_v2.analyze_source(
                source.read_text(encoding="utf-8", errors="replace"),
                sha256=brand["source_sha256"],
            )
            candidates = brand_marks_v2.plan_candidates(
                model,
                brand["brief"],
                name=str(brand.get("display_name") or brand["name"]),
                families=brand["families"],
                count=brand["candidates"],
                seed_hint=brand["seed"],
            )
            return [
                {
                    "id": candidate["id"],
                    "kind": "brand",
                    "direction": candidate["family"],
                    "style": candidate["family"],
                    "theme": "brand",
                    "brand": brand,
                    "candidate": candidate,
                    "family": candidate["family"],
                    "seed": candidate["seed"],
                }
                for candidate in candidates
            ]
        if brand["mode"] == "explore":
            candidates = brand_marks.plan_candidates(
                brand["brief"],
                concept=brand["concept"],
                count=brand["candidates"],
                seed_hint=brand["seed"],
            )
            return [
                {
                    "id": candidate["id"],
                    "kind": "brand",
                    "direction": candidate["family"],
                    "style": candidate["family"],
                    "theme": "brand",
                    "brand": brand,
                    "candidate": candidate,
                    "family": candidate["family"],
                    "seed": candidate["seed"],
                }
                for candidate in candidates
            ]
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

        if brand.get("mode", "existing") == "explore":
            return self._render_explore(variant, brand, out, ctx)
        if brand.get("mode", "existing") == "refine":
            return self._render_refine(variant, brand, out, ctx)

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

    def _render_explore(
        self,
        variant: Mapping[str, Any],
        brand: Mapping[str, Any],
        out: Path,
        ctx: base.ProfileContext,
    ) -> Dict[str, Any]:
        """후보 하나를 VectorCraft headless로 그린 뒤 기존 정규화·아웃라인을 건다."""
        candidate = dict(variant.get("candidate") or {})
        rendered = brand_marks.render_candidate(
            ctx.run,
            _binary(ctx),
            candidate,
            out,
            brand["palette"],
        )
        raw = Path(rendered["svg"]).read_text(encoding="utf-8")
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

        single_text = brand_marks.single_color_svg(
            outlined_text, brand["palette"]["ink"]
        )
        single_color = out / "outlined-single-color.svg"
        single_color.write_text(single_text, encoding="utf-8")

        gates = candidate_gates(
            ctx,
            outlined=outlined,
            single_color=single_color,
            workdir=out / "gate",
            palette=brand["palette"],
        )
        stats = normalize_report(raw, normalized_text)
        stats["outlined"] = svg_stats(outlined_text)
        return {
            **variant,
            "svg": str(outlined),
            "png": rendered["png"],
            "candidate_svg": rendered["svg"],
            "normalized": str(normalized),
            "outlined": str(outlined),
            "single_color": str(single_color),
            "stats": stats,
            "gates": gates,
            "gate_summary": candidate_gate_summary(gates),
            "family": candidate.get("family"),
            "seed": candidate.get("seed"),
        }

    def _render_refine(
        self,
        variant: Mapping[str, Any],
        brand: Mapping[str, Any],
        out: Path,
        ctx: base.ProfileContext,
    ) -> Dict[str, Any]:
        """인장 정련 후보 하나 — 원본 구조에서 만든 SVG를 굽고 같은 게이트를 건다."""
        candidate = dict(variant.get("candidate") or {})
        rendered = brand_marks_v2.render_candidate(
            ctx.run,
            _binary(ctx),
            candidate,
            out,
            brand["palette"],
        )
        raw = Path(rendered["svg"]).read_text(encoding="utf-8")
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

        single_text = brand_marks.single_color_svg(
            outlined_text, brand["palette"]["ink"]
        )
        single_color = out / "outlined-single-color.svg"
        single_color.write_text(single_text, encoding="utf-8")

        # 렌더 게이트는 아웃라인 결과에, 제작 규칙 게이트는 후보 원본 SVG에 건다.
        # (convert가 요소 순서를 바꿔도 획 배율·오버슈트 검사가 흔들리지 않게)
        gates = candidate_gates(
            ctx,
            outlined=outlined,
            single_color=single_color,
            workdir=out / "gate",
            palette=brand["palette"],
        )
        gates += brand_marks_v2.craft_gates(candidate, raw, brand["palette"])
        presentation = brand_marks_v2.presentation_images(
            Path(rendered["png"]), out, brand["palette"]
        )
        stats = normalize_report(raw, normalized_text)
        stats["outlined"] = svg_stats(outlined_text)
        return {
            **variant,
            "svg": str(outlined),
            "png": rendered["png"],
            "candidate_svg": rendered["svg"],
            "normalized": str(normalized),
            "outlined": str(outlined),
            "single_color": str(single_color),
            "stats": stats,
            "gates": gates,
            "gate_summary": candidate_gate_summary(gates),
            "family": candidate.get("family"),
            "seed": candidate.get("seed"),
            "note": candidate.get("note"),
            "light": presentation["light"],
            "dark": presentation["dark"],
            "small": presentation["small"],
        }

    def contact_sheet(
        self,
        renders: Sequence[Mapping[str, Any]],
        spec: Mapping[str, Any],
        state: Mapping[str, Any],
        ctx: base.ProfileContext,
    ) -> Optional[Dict[str, Any]]:
        """refine 후보를 종이 512 격자·16px 줄·다크 줄로 묶고 게이트 표를 쓴다.

        explore/existing은 v1 동작을 바꾸지 않도록 시트를 만들지 않는다(None).
        """
        brand = brand_spec(spec)
        if brand["mode"] != "refine":
            return None
        run_dir = Path(ctx.run_dir or ctx.out_root)
        rows = [
            {
                "id": render.get("id"),
                "family": render.get("family"),
                "note": render.get("note"),
                "light": render.get("light"),
                "dark": render.get("dark"),
                "small": render.get("small"),
                "gates": render.get("gates") or [],
            }
            for render in renders
        ]
        sheet = brand_marks_v2.compose_candidate_sheet(
            rows,
            out=run_dir / "candidate-contact.png",
            palette=brand["palette"],
            brief=brand["brief"],
            run_id=str(state.get("run_id") or run_dir.name),
        )
        table = brand_marks_v2.gate_table_markdown(
            rows, title=f"마크 정련 후보 게이트 — {brand['name']}"
        )
        table_path = run_dir / "gate-table.md"
        table_path.write_text(table, encoding="utf-8")
        sheet["gate_table"] = str(table_path)
        return sheet

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
        generated = brand.get("mode", "existing") in ("explore", "refine")
        mode_name = brand.get("mode", "existing")
        if generated and len(items) > 1:
            raise base.ProfileError(
                f"brand {mode_name}: 킷은 후보 1개로만 만든다 — selection {len(items)}건. "
                "고른 후보 하나만 selection에 넣어라"
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
        candidate = dict(item.get("candidate") or {})
        candidate_gate_rows = list(item.get("gates") or [])
        if generated:
            # 부분 상태(게이트·후보 메타 없음)를 통과로 오인하지 않는다.
            if not (candidate.get("params") or {}).get("shapes"):
                raise base.ProfileError(
                    f"brand {mode_name}: 선택 후보에 도형 정보가 없다 — 다시 렌더한 뒤 pack하라"
                )
            present = {str(gate.get("id")) for gate in candidate_gate_rows}
            required = (
                REQUIRED_REFINE_GATE_IDS
                if mode_name == "refine"
                else REQUIRED_CANDIDATE_GATE_IDS
            )
            missing = sorted(required - present)
            if missing:
                raise BrandGateError(
                    [
                        {
                            "id": "candidate.missing",
                            "detail": "후보 게이트 결과가 빠졌다 — "
                            + ", ".join(missing),
                        }
                    ]
                )
            candidate_failures = [
                gate for gate in candidate_gate_rows if not gate.get("ok")
            ]
            if candidate_failures and not allow:
                raise BrandGateError(candidate_failures)
            for gate in candidate_failures:
                gate["skipped"] = True

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

        lockup: Optional[Dict[str, Optional[str]]] = None
        lockup_gates: List[Dict[str, Any]] = []
        if generated:
            display_name = str(brand.get("display_name") or brand["name"])
            if mode_name == "refine":
                lockup = brand_marks_v2.render_lockup(
                    ctx.run,
                    _binary(ctx),
                    candidate,
                    display_name,
                    pack_dir,
                    brand["palette"],
                    pdf=brand["pdf"],
                )
            else:
                lockup = brand_marks.render_lockup(
                    ctx.run,
                    _binary(ctx),
                    candidate,
                    display_name,
                    pack_dir,
                    brand["palette"],
                    pdf=brand["pdf"],
                )
            lockup_gates = [
                _gate_lockup(
                    Path(str(lockup["svg"])),
                    Path(str(lockup["pdf"])) if lockup.get("pdf") else None,
                    pdf_required=brand["pdf"],
                )
            ]
            lockup_failures = [gate for gate in lockup_gates if not gate.get("ok")]
            if lockup_failures and not allow:
                raise BrandGateError(lockup_failures)
            elif lockup_failures:
                for gate in lockup_failures:
                    gate["skipped"] = True
        lockup_meta: Optional[Dict[str, Any]] = None
        if lockup:
            lockup_meta = {
                # meta에는 실행 디렉터리와 무관한 상대 이름만 남긴다(결정론).
                "svg": Path(str(lockup["svg"])).name,
                "pdf": Path(str(lockup["pdf"])).name if lockup.get("pdf") else None,
                "sha256": core_artifacts.sha256_file(Path(str(lockup["svg"]))),
            }

        png = pack_dir / f"mark-{width}.png"
        ctx.convert(outlined, png, width, max_height)
        jpg = pack_dir / "mark.jpg"
        ctx.jpeg(png, jpg, quality)

        dark_svg: Optional[Path] = None
        dark_png: Optional[Path] = None
        if generated:
            # dark 배경은 ink가 배경과 같아진다 — ink만 paper로 바꾼 변형을 쓴다.
            dark_text = brand_marks.recolor_svg(
                outlined.read_text(encoding="utf-8", errors="replace"),
                {brand["palette"]["ink"]: brand["palette"]["paper"]},
            )
            dark_svg = pack_dir / "outlined-dark.svg"
            dark_svg.write_text(dark_text, encoding="utf-8")
            dark_png = pack_dir / f"mark-{width}-dark.png"
            ctx.convert(dark_svg, dark_png, width, max_height)

        icons = build_icon_variants(
            png,
            icon_dir,
            name=brand["name"],
            sizes=brand["sizes"],
            backgrounds=brand["backgrounds"],
            palette=brand["palette"],
            dark_page=dark_png,
            crop=generated,
        )

        artifacts: Dict[str, Any] = {}
        paths: List[Path] = [normalized, outlined, png, jpg]
        if dark_svg is not None and dark_png is not None:
            paths += [dark_svg, dark_png]
        if pdf.is_file():
            paths.append(pdf)
        if lockup:
            for key in ("svg", "pdf"):
                value = lockup.get(key)
                if value and Path(value).is_file():
                    paths.append(Path(value))
        paths += [Path(row["path"]) for row in icons]
        for path in paths:
            if not path.is_file():
                continue
            artifacts[str(path.relative_to(pack_dir))] = {
                "sha256": core_artifacts.sha256_file(path),
                "size": path.stat().st_size,
            }

        all_gates = list(gates)
        if generated:
            # 후보 게이트와 최종 게이트가 겹치는 3종은 **팩 시점 재검사**를 남기고,
            # 나머지 후보 게이트만 더한다. 같은 id를 두 번 적지 않는다.
            shared = {"brand.no_text", "brand.no_style", "brand.raster"}
            final_shared = [gate for gate in gates if gate["id"] in shared]
            extra = [gate for gate in candidate_gate_rows if gate["id"] not in shared]
            pdf_gates = [gate for gate in gates if gate["id"] == "brand.pdf"]
            all_gates = final_shared + extra + pdf_gates + lockup_gates
        gates_ok = all(gate["ok"] for gate in all_gates)
        mark_sha256 = brand.get("sha256") or core_artifacts.sha256_file(outlined)
        if generated:
            inputs = {
                "mode": mode_name,
                "brief_sha256": brand["brief_sha256"],
                "candidate": candidate,
                "mark_sha256": mark_sha256,
                "palette": brand["palette"],
                "sizes": brand["sizes"],
                "backgrounds": brand["backgrounds"],
                "image_width": width,
                "max_height": max_height,
                "jpg_quality": quality,
                "pdf": brand["pdf"],
            }
        else:
            inputs = {
                "mark_sha256": brand["sha256"],
                "palette": brand["palette"],
                "sizes": brand["sizes"],
                "backgrounds": brand["backgrounds"],
                "image_width": width,
                "max_height": max_height,
                "jpg_quality": quality,
                "pdf": brand["pdf"],
            }
        meta = {
            "profile": "brand",
            "name": brand["name"],
            "mark_path": brand.get("mark_path"),
            "mark_sha256": mark_sha256,
            "inputs_sha256": core_artifacts.sha256_json(inputs),
            "palette": brand["palette"],
            "sizes": brand["sizes"],
            "backgrounds": brand["backgrounds"],
            "image_width": width,
            "max_height": max_height,
            "jpg_quality": quality,
            "pdf_requested": brand["pdf"],
            "allow_brand_checks": allow,
            "gates_ok": gates_ok,
            "gates": all_gates,
            "normalization": dict(item.get("stats") or {}),
            "artifacts": artifacts,
            "icons": icons,
        }
        if generated:
            meta.update(
                {
                    "mode": mode_name,
                    "brief": brand["brief"],
                    "brief_sha256": brand["brief_sha256"],
                    "candidate": candidate,
                    "candidate_gates": candidate_gate_rows,
                    "candidate_gate_summary": candidate_gate_summary(
                        candidate_gate_rows
                    ),
                    "lockup": lockup_meta,
                    "dark_variant": "outlined-dark.svg" if dark_svg else None,
                }
            )
        meta_path = pack_dir / "meta.json"
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        lines = [f"# 브랜드 팩 — {brand['name']}", ""]
        if generated:
            lines += [
                f"- 모드: **{mode_name}** — 후보 `{candidate.get('id')}` "
                f"({candidate.get('family')}, seed `{candidate.get('seed')}`)",
                f"- 브리프: {brand['brief']}",
                f"- 브리프 지문: `{brand['brief_sha256'][:16]}`",
                f"- 팔레트: "
                + " · ".join(
                    f"{key} {value}" for key, value in brand["palette"].items()
                ),
                f"- 아이콘: {brand['sizes']} × {'/'.join(brand['backgrounds'])}",
                f"- 락업: `lockup.svg`"
                + (" · `lockup.pdf`" if lockup and lockup.get("pdf") else ""),
                f"- 입력 지문: `{meta['inputs_sha256'][:16]}`",
                f"- 후보 게이트: {candidate_gate_summary(candidate_gate_rows)}",
                "",
                "## 게이트",
            ]
        else:
            lines += [
                f"- 마크: `{Path(brand['mark_path']).name}` (`{brand['sha256'][:16]}`)",
                f"- 팔레트: "
                + " · ".join(
                    f"{key} {value}" for key, value in brand["palette"].items()
                ),
                f"- 아이콘: {brand['sizes']} × {'/'.join(brand['backgrounds'])}",
                f"- 입력 지문: `{meta['inputs_sha256'][:16]}`",
                "",
                "## 게이트",
            ]
        for gate in all_gates:
            mark = "✓" if gate["ok"] else ("−" if gate.get("skipped") else "✗")
            lines.append(f"- {mark} {gate['id']}: {gate['detail']}")
        lines += ["", "## 산출물"]
        for name, row in artifacts.items():
            lines.append(f"- `{name}` ({row['size']:,}B)")
        entry_path = pack_dir / "entry.md"
        entry_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        packed_item: Dict[str, Any] = {
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
            "gates": all_gates,
            "icons": icons,
        }
        if generated:
            packed_item.update(
                {
                    "mode": mode_name,
                    "candidate": candidate,
                    "candidate_gates": candidate_gate_rows,
                    "lockup": lockup,
                    "lockup_svg": (lockup or {}).get("svg"),
                    "lockup_pdf": (lockup or {}).get("pdf"),
                }
            )
        packed = [packed_item]

        artifact_paths: List[Any] = list(paths) + [meta_path, entry_path]
        manifest = base.run_manifest(
            {**state, "spec": spec},
            run_dir,
            base.collect_artifacts(artifact_paths, run_dir),
            kind="brand",
            status="ok" if gates_ok else "warn",
            gates=all_gates,
        )
        manifest_path = core_artifacts.write_manifest(run_dir, manifest)

        return {
            "dir": str(pack_dir),
            "items": packed,
            "meta": str(meta_path),
            "manifest": str(manifest_path),
            "manifest_digest": core_artifacts.manifest_digest(manifest),
            "gates": all_gates,
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

    def review(
        self,
        mark_svg: Optional[str],
        *,
        ctx: Optional[base.ProfileContext] = None,
        workdir: Optional[Path] = None,
        print_spec: Optional[Mapping[str, Any]] = None,
        craft_failed: bool = False,
        variants: Sequence[Any] = (),
        craft_status: str = "",
        mark_source: Optional[str] = None,
        mark_locked: bool = False,
    ) -> Dict[str, Any]:
        """납품 전 검토 한 번 — context_test + critic + print_prep.

        삭제된 `logo_design` 그래프의 검증 단계를 profile의 공개 API로 묶었다.
        그래프·스크립트는 이 메서드 하나만 호출하면 된다(마크 생성 없음).
        """
        context = context_test(mark_svg, ctx=ctx, workdir=workdir)
        return {
            "context": context,
            "score": critic_score(context["score"], craft_failed=craft_failed),
            "print": print_prep(
                print_spec,
                variants=variants,
                craft_status=craft_status,
                mark_source=mark_source,
                mark_locked=mark_locked,
            ),
        }
