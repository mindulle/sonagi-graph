"""core/env — 경로·서비스 주소의 단일 정의.

그래프마다 `ASSETS_DIR`·`CATALOG`·`OUT_ROOT`를 다시 선언하면 기본값이 조금씩
어긋난다(어떤 그래프는 `/tmp/entry_pack`, 어떤 그래프는 `/tmp/package_delivery`).
이 모듈 하나만 읽으면 "무엇이 어디에 있는지" 확정된다.

우선순위:

1. 모듈 환경변수 — `SONAGI_ASSETS_DIR`, `SONAGI_CATALOG`, `SONAGI_OUT_ROOT` …
2. 그래프별 레거시 별칭 — `out_root("entry_pack")`는 `SONAGI_ENTRY_OUT`
3. 코드 기본값 — 기존 그래프와 같은 값

카탈로그(`CATALOG`)만 예외적으로 파일 존재 여부를 본다. 실카탈로그
(`<assets>/catalog/catalog.json`, devops에서 `scripts/sync_catalog.sh`로
동기화)가 있으면 그것을 쓰고, 없으면 골든 테스트 카탈로그(8건)로
폴백한다. 폴백이면 프로세스당 한 번 stderr 경고를 낸다. 자세한 내용은
`docs/CATALOG_SYNC.md`.

    env.CATALOG                  # 실카탈로그 경로(있으면)
    env.CATALOG_SOURCE           # "real" | "fallback"
    env.resolve_catalog({})      # (Path, "real"|"fallback")

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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, TextIO, Tuple

DEFAULT_ASSETS_DIR = "/home/ubuntu/sonagi-assets"
DEFAULT_WIKI_DIR = "/home/ubuntu/llm-wiki"
DEFAULT_OUT_ROOT = "/tmp/sonagi-graph"
DEFAULT_SEARXNG_URL = "http://localhost:8080"
DEFAULT_VECTORCRAFT = "vectorcraft-cli"

#: 실카탈로그는 자산 디렉터리 아래 `catalog/catalog.json`에 둔다.
#: devops `/srv/sonagi-assets/catalog/catalog.json`에서 rsync로 동기화한다.
REAL_CATALOG_RELPATH = Path("catalog/catalog.json")
#: 동기화 이전/실패 시 쓰는 골든 테스트 카탈로그.
FALLBACK_CATALOG_RELPATH = Path("test_data/golden/catalog.json")

CATALOG_SOURCE_REAL = "real"
CATALOG_SOURCE_FALLBACK = "fallback"

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
    catalog_source: str
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
            "catalog_source": self.catalog_source,
            "tokens": str(self.tokens),
            "out_root": str(self.out_root),
            "wiki_dir": str(self.wiki_dir),
            "searxng_url": self.searxng_url,
            "vectorcraft": self.vectorcraft,
        }


def _get(environ: Mapping[str, str], key: str, default: str) -> str:
    value = environ.get(key)
    return value if value else default


def _file_exists(path: Path) -> bool:
    """테스트에서 갈아끼울 수 있는 존재 확인 함수."""
    return path.is_file()


def resolve_catalog(
    environ: Optional[Mapping[str, str]] = None,
    *,
    exists: Optional[Callable[[Path], bool]] = None,
) -> Tuple[Path, str]:
    """카탈로그 경로와 출처(`real`|`fallback`)를 정한다.

    `SONAGI_CATALOG`가 명시되어 있으면 그대로 존중한다(파일 존재를
    검사하지 않는다 — 기존 동작 유지). 명시가 없으면 자산 디렉터리의
    실카탈로그를 먼저 보고, 없을 때만 골든 카탈로그로 폴백한다.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    explicit = source.get("SONAGI_CATALOG")
    if explicit:
        return Path(explicit), CATALOG_SOURCE_REAL
    assets_dir = Path(_get(source, "SONAGI_ASSETS_DIR", DEFAULT_ASSETS_DIR))
    check = exists or _file_exists
    real = assets_dir / REAL_CATALOG_RELPATH
    if check(real):
        return real, CATALOG_SOURCE_REAL
    return assets_dir / FALLBACK_CATALOG_RELPATH, CATALOG_SOURCE_FALLBACK


_warned_fallback = False


def warn_catalog_fallback(
    catalog: Path,
    source: str,
    *,
    stream: Optional[TextIO] = None,
) -> bool:
    """골든 폴백이면 프로세스당 한 번만 stderr 경고를 낸다.

    경고했으면 True를 돌려준다. 실행을 실패시키지 않는다.
    """
    global _warned_fallback
    if source != CATALOG_SOURCE_FALLBACK or _warned_fallback:
        return False
    _warned_fallback = True
    print(
        "[sonagi-graph] catalog: real catalog not found — using golden fallback "
        f"{catalog}. Run scripts/sync_catalog.sh to sync the real one.",
        file=stream or sys.stderr,
    )
    return True


def load_paths(environ: Optional[Mapping[str, str]] = None) -> Paths:
    """환경변수에서 경로 묶음을 만든다. 파일 시스템은 건드리지 않는다."""
    source: Mapping[str, str] = os.environ if environ is None else environ
    assets_dir = Path(_get(source, "SONAGI_ASSETS_DIR", DEFAULT_ASSETS_DIR))
    catalog, catalog_source = resolve_catalog(source)
    return Paths(
        assets_dir=assets_dir,
        catalog=catalog,
        catalog_source=catalog_source,
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
CATALOG_SOURCE: str = PATHS.catalog_source
TOKENS: Path = PATHS.tokens
OUT_ROOT: Path = PATHS.out_root
WIKI_DIR: Path = PATHS.wiki_dir
SEARXNG_URL: str = PATHS.searxng_url
VECTORCRAFT: str = PATHS.vectorcraft

#: import 시점에 한 번만 폴백 경고를 낸다(실행은 계속).
warn_catalog_fallback(CATALOG, CATALOG_SOURCE)


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
