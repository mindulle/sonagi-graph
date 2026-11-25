"""core/env — 경로·서비스 주소의 단일 정의.

그래프마다 `ASSETS_DIR`·`CATALOG`·`OUT_ROOT`를 다시 선언하면 기본값이 조금씩
어긋난다(어떤 그래프는 `/tmp/entry_pack`, 어떤 그래프는 `/tmp/package_delivery`).
이 모듈 하나만 읽으면 "무엇이 어디에 있는지" 확정된다.

우선순위:

1. 모듈 환경변수 — `SONAGI_ASSETS_DIR`, `SONAGI_CATALOG`, `SONAGI_OUT_ROOT` …
2. 그래프별 레거시 별칭 — `out_root("entry_pack")`는 `SONAGI_ENTRY_OUT`
3. 코드 기본값 — 기존 그래프와 같은 값

import는 경로를 계산만 한다. 디렉터리 생성은 `artifacts.create_run_dir()`이
맡는다(부작용 없음).

사용::

    from graphs.core import env

    env.ASSETS_DIR                # /home/ubuntu/sonagi-assets
    env.out_root("entry_pack")    # SONAGI_ENTRY_OUT, 기본 /tmp/entry_pack
    env.load_paths({"SONAGI_ASSETS_DIR": "/tmp/a"}).catalog
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

DEFAULT_ASSETS_DIR = "/home/ubuntu/sonagi-assets"
DEFAULT_WIKI_DIR = "/home/ubuntu/llm-wiki"
DEFAULT_OUT_ROOT = "/tmp/sonagi-graph"
DEFAULT_SEARXNG_URL = "http://localhost:8080"
DEFAULT_VECTORCRAFT = "vectorcraft-cli"

#: 그래프별 OUT_ROOT 레거시 별칭 — (환경변수, 기본값). W2 이관에서 기본값을
#: 바꾸지 않기 위해 남긴다. 새 그래프는 `SONAGI_OUT_ROOT` 하나만 쓴다.
OUT_ROOT_ALIASES: Dict[str, Tuple[str, str]] = {
    "entry_pack": ("SONAGI_ENTRY_OUT", "/tmp/entry_pack"),
    "asset_craft": ("SONAGI_CRAFT_OUT", "/tmp/asset_craft"),
    "logo_design": ("SONAGI_PACKAGE_OUT", "/tmp/package_delivery"),
    "package_series": ("SONAGI_PACKAGE_OUT", "/tmp/package_delivery"),
}


@dataclass(frozen=True)
class Paths:
    """한 실행이 참조하는 모든 경로·주소."""

    assets_dir: Path
    catalog: Path
    tokens: Path
    out_root: Path
    wiki_dir: Path
    searxng_url: str
    vectorcraft: str

    def as_dict(self) -> Dict[str, str]:
        """manifest·로그에 남길 수 있는 문자열 사전."""
        return {
            "assets_dir": str(self.assets_dir),
            "catalog": str(self.catalog),
            "tokens": str(self.tokens),
            "out_root": str(self.out_root),
            "wiki_dir": str(self.wiki_dir),
            "searxng_url": self.searxng_url,
            "vectorcraft": self.vectorcraft,
        }


def _get(environ: Mapping[str, str], key: str, default: str) -> str:
    value = environ.get(key)
    return value if value else default


def load_paths(environ: Optional[Mapping[str, str]] = None) -> Paths:
    """환경변수에서 경로 묶음을 만든다. 파일 시스템은 건드리지 않는다."""
    source: Mapping[str, str] = os.environ if environ is None else environ
    assets_dir = Path(_get(source, "SONAGI_ASSETS_DIR", DEFAULT_ASSETS_DIR))
    return Paths(
        assets_dir=assets_dir,
        catalog=Path(
            _get(
                source,
                "SONAGI_CATALOG",
                str(assets_dir / "test_data/golden/catalog.json"),
            )
        ),
        tokens=Path(
            _get(source, "SONAGI_TOKENS", str(assets_dir / "tokens/semantic.json"))
        ),
        out_root=Path(_get(source, "SONAGI_OUT_ROOT", DEFAULT_OUT_ROOT)),
        wiki_dir=Path(_get(source, "SONAGI_WIKI_DIR", DEFAULT_WIKI_DIR)),
        searxng_url=_get(source, "SEARXNG_URL", DEFAULT_SEARXNG_URL),
        vectorcraft=_get(source, "VECTORCRAFT_BIN", DEFAULT_VECTORCRAFT),
    )


PATHS = load_paths()

ASSETS_DIR: Path = PATHS.assets_dir
CATALOG: Path = PATHS.catalog
TOKENS: Path = PATHS.tokens
OUT_ROOT: Path = PATHS.out_root
WIKI_DIR: Path = PATHS.wiki_dir
SEARXNG_URL: str = PATHS.searxng_url
VECTORCRAFT: str = PATHS.vectorcraft


def out_root(graph: str, environ: Optional[Mapping[str, str]] = None) -> Path:
    """그래프 하나의 실행 루트.

    `SONAGI_OUT_ROOT`가 있으면 그것이 이긴다. 없으면 그래프 별칭
    (`SONAGI_ENTRY_OUT` …)을 보고, 그것도 없으면 그 그래프의 기존 기본값을 쓴다.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    explicit = source.get("SONAGI_OUT_ROOT")
    if explicit:
        return Path(explicit)
    alias = OUT_ROOT_ALIASES.get(graph)
    if alias:
        key, default = alias
        return Path(source.get(key) or default)
    return Path(DEFAULT_OUT_ROOT)
