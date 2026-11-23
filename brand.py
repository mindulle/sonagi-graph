"""브랜드 토큰 로더 — 색을 코드에 박지 않고 `sonagi-assets` SSOT에서 읽는다.

그래프가 팔레트를 직접 들고 있으면 토큰을 개정해도 그래프만 옛 색으로 남는다.
(2026-11-23 기준 `design_ops_graph.py`에 hex 12개가 박혀 있었다.)

SSOT는 `sonagi-assets/tokens/semantic.json`(팔레트), `tokens/tokens.css`(서체·선),
`sonagi-assets/registry/`(스타일 id·룰 버전)다. 경로는 `SONAGI_ASSETS_DIR`로
바꿀 수 있다.

```bash
export SONAGI_ASSETS_DIR=/home/ubuntu/sonagi-assets   # 기본값
```

토큰에 없는 색은 **파생**한다. 파생은 결정론적이며(같은 입력 → 같은 hex),
원래 값을 그대로 베끼지 않는다는 점을 문서에 남긴다.

| 그래프 역할       | 출처                                             |
| ----------------- | ------------------------------------------------ |
| `bg_obsidian`     | architecture `background`                        |
| `bg_dome`         | architecture `surface`                           |
| `border_slate`    | architecture `border`                            |
| `border_dark`     | `border`를 `background` 쪽으로 45%               |
| `ice_blue`        | architecture `accent`                            |
| `cyan_soft`       | `accent`를 흰색 쪽으로 45% (원래 #00ffcc 근사)   |
| `gold_dandelion`  | architecture `accent-strong`                     |
| `gold_core`       | editorial `accent-strong`                        |
| `gold_highlight`  | architecture `accent-strong`를 흰색 쪽으로 72%   |
| `white`           | architecture `ink`                               |
| `mono_ink`        | editorial `ink`                                  |
| `mono_paper`      | editorial `background`                           |

사용::

    import brand

    palette = brand.design_palette("architecture")
    typo = brand.typography()     # display/sans/mono — tokens.css의 --mds-font-*
    strand = brand.stroke("border-width")   # "2px" (True 2px Stem)
    stamp = brand.contract()      # tokens_version·style_count — 산출물에 남긴다
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


class BrandSourceMissing(RuntimeError):
    """토큰 SSOT를 찾지 못했을 때. 경로를 알려주고 멈춘다."""


def assets_dir() -> Path:
    return Path(os.environ.get("SONAGI_ASSETS_DIR", "/home/ubuntu/sonagi-assets"))


def _read(path: Path):
    if not path.is_file():
        raise BrandSourceMissing(
            f"브랜드 SSOT가 없습니다: {path}\n"
            "sonagi-assets 저장소를 두고 SONAGI_ASSETS_DIR를 지정하세요 "
            "(기본 /home/ubuntu/sonagi-assets)."
        )
    return json.loads(path.read_text())


def available() -> bool:
    """SSOT가 있는지. 테스트에서 skip 조건으로 쓴다."""
    return (assets_dir() / "tokens/semantic.json").is_file()


def tokens() -> dict:
    return _read(assets_dir() / "tokens/semantic.json")


def semantic(theme: str = "architecture") -> dict:
    palette = tokens()
    if theme not in palette:
        raise KeyError(
            f"토큰에 없는 테마: {theme} (있는 것: {', '.join(sorted(palette))})"
        )
    return palette[theme]


def tokens_version() -> str:
    path = assets_dir() / "tokens/semantic.json"
    if not path.is_file():
        raise BrandSourceMissing(f"브랜드 SSOT가 없습니다: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def registry() -> dict:
    """명부 색인 + 스타일 목록. 없으면 BrandSourceMissing."""
    index = _read(assets_dir() / "registry/index.json")
    styles = _read(assets_dir() / "registry/styles.json")["styles"]
    return {"index": index, "styles": styles, "by_id": {s["id"]: s for s in styles}}


def style(name_or_id: str) -> dict:
    """이름(`editorial`) 또는 id(`editorial@6b6b1bac5733`)로 스타일을 찾는다."""
    data = registry()
    if name_or_id in data["by_id"]:
        return data["by_id"][name_or_id]
    for entry in data["styles"]:
        if entry["name"] == name_or_id:
            return entry
    raise KeyError(f"명부에 없는 스타일: {name_or_id}")


def contract() -> dict:
    """산출물에 남길 재현 좌표. SSOT가 없으면 BrandSourceMissing."""
    data = registry()
    return {
        "tokens_version": tokens_version(),
        "registry_schema": data["index"].get("schema"),
        "style_count": len(data["styles"]),
        "ruleset_version": data["index"].get("ruleset_version"),
        "source": str(assets_dir()),
    }


# --- 서체·선 -------------------------------------------------------------

_CSS_VAR = re.compile(r"--mds-([a-z-]+):\s*([^;]+);")


def css_vars() -> dict:
    """`tokens/tokens.css`의 `--mds-*` 변수를 이름 그대로 읽는다."""
    path = assets_dir() / "tokens/tokens.css"
    if not path.is_file():
        raise BrandSourceMissing(f"토큰 CSS가 없습니다: {path}")
    return dict(_CSS_VAR.findall(path.read_text()))


def _first_family(stack: str) -> str:
    first = stack.split(",")[0].strip()
    return first.strip("'\"")


def typography() -> dict:
    """토큰의 서체 세 역할. `stack`은 CSS 값, `family`는 인용부호 없는 이름."""
    variables = css_vars()
    out = {}
    for role in ("display", "sans", "mono"):
        stack = variables.get(f"font-{role}")
        if not stack:
            raise BrandSourceMissing(f"토큰에 --mds-font-{role}이 없습니다")
        out[role] = {"stack": stack.strip(), "family": _first_family(stack)}
    return out


def google_fonts_url(weights: dict[str, str]) -> str:
    """`{"display": "700", "mono": "600"}` → Google Fonts CSS URL."""
    families = typography()
    parts = []
    for role, weight in weights.items():
        if role not in families:
            raise KeyError(f"토큰에 없는 서체 역할: {role}")
        parts.append(
            f"family={families[role]['family'].replace(' ', '+')}:wght@{weight}"
        )
    return "https://fonts.googleapis.com/css2?" + "&".join(parts)


def stroke(role: str = "border-width") -> str:
    """토큰의 선 두께(px 문자열). 기본은 구조선 2px."""
    value = css_vars().get(role)
    if not value:
        raise BrandSourceMissing(f"토큰에 --mds-{role}이 없습니다")
    return value.strip()


def stroke_px(role: str = "border-width") -> float:
    return float(stroke(role).removesuffix("px"))


# --- 색 파생 -------------------------------------------------------------


def _rgb(value: str) -> tuple[int, int, int]:
    raw = value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) != 6:
        raise ValueError(f"hex가 아닙니다: {value!r}")
    return tuple(int(raw[i : i + 2], 16) for i in (0, 2, 4))


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(a: str, b: str, t: float) -> str:
    """a에서 b로 t(0~1)만큼 섞는다."""
    left, right = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(left, right)))


def tint(color: str, t: float) -> str:
    return mix(color, "#ffffff", t)


def shade(color: str, t: float, onto: str = "#000000") -> str:
    return mix(color, onto, t)


def design_palette(theme: str = "architecture") -> dict:
    """design_ops 그래프가 쓰는 12개 역할. 모두 토큰 또는 토큰의 파생이다."""
    base = semantic(theme)
    editorial = semantic("editorial")
    return {
        "bg_obsidian": base["background"],
        "bg_dome": base["surface"],
        "border_slate": base["border"],
        "border_dark": shade(base["border"], 0.45, onto=base["background"]),
        "ice_blue": base["accent"],
        "cyan_soft": tint(base["accent"], 0.45),
        "gold_dandelion": base["accent-strong"],
        "gold_core": editorial["accent-strong"],
        "gold_highlight": tint(base["accent-strong"], 0.72),
        "white": base["ink"],
        "mono_ink": editorial["ink"],
        "mono_paper": editorial["background"],
    }
