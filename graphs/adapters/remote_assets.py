"""remote_assets — 웹 렌더를 devops로 넘긴다 (GRAPH-24).

`llmops`(이 호스트)의 디스크에서 자산 라이브러리(7.2GB)를 걷어내고, 렌더만
devops에서 돌린다. 카탈로그·라이브러리는 devops의 `/srv/sonagi-assets`에 있고,
이 모듈은 **렌더 코드(scripts·styles·tokens·rules)만** 원격 작업 디렉터리로
동기화한 뒤 레시피를 복사해 실행하고 산출물을 되가져온다.

```
llmops (langgraph 컨테이너)
  assets/scripts,styles,tokens,rules ──rsync──▶ devops:/home/ubuntu/sonagi-entry-render
  recipe.json                       ──rsync──▶ .../incoming/<id>/recipe.json
                                     ssh     python3 scripts/render_recipe.py \
                                               --catalog /srv/sonagi-assets/catalog/catalog.json
  out/<id>/  ◀──────────────────────rsync──── .../out/<id>/
```

원격 카탈로그는 devops 절대경로(`/srv/sonagi-assets/library/...`)를 담으므로
로컬 카탈로그처럼 경로를 다시 쓸 필요가 없다.

사용::

    from graphs.adapters import remote_assets

    if remote_assets.enabled():
        result = remote_assets.render(
            recipe, "editorial", "desktop", dark=False, out_dir=run_dir / "v1"
        )
        result["svg"]        # 로컬로 되가져온 SVG 경로

환경변수(기본값):

- `SONAGI_REMOTE_RENDER`  `1`이면 원격 렌더를 켠다(기본 `0` — 테스트·로컬은 로컬 렌더)
- `SONAGI_RENDER_HOST`    ssh 별칭 (`devops-render`)
- `SONAGI_RENDER_ROOT`    원격 작업 디렉터리 (`/home/ubuntu/sonagi-entry-render`)
- `SONAGI_RENDER_CATALOG` 원격 카탈로그 (`/srv/sonagi-assets/catalog/catalog.json`)
- `SONAGI_RENDER_SSH_CONFIG` ssh 설정 파일 (기본 `~/.ssh/sonagi_render_config`)
- `SONAGI_RENDER_TIMEOUT` ssh/rsync 한 번의 제한 시간(초, 기본 300)

`run` 훅을 넘기면 테스트가 실제 ssh 없이 argv만 검증할 수 있다(`assets` 어댑터와
같은 규약: argv → stdout 문자열).
"""

from __future__ import annotations

import os
import shlex
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from graphs.core import env as core_env
from graphs.core import exec as core_exec

#: argv → stdout. 넘기지 않으면 core/exec가 실행한다.
Runner = Callable[..., str]

DEFAULT_HOST = "devops-render"
DEFAULT_ROOT = "/home/ubuntu/sonagi-entry-render"
DEFAULT_CATALOG = "/srv/sonagi-assets/catalog/catalog.json"
DEFAULT_SSH_CONFIG = "~/.ssh/sonagi_render_config"
DEFAULT_TIMEOUT = 300

#: 원격에 필요한 최소 렌더 코드. 나머지(.git·out·library·catalog)는 보내지 않는다.
SYNC_DIRS = ("scripts", "styles", "tokens", "rules")

_TRUTHY = {"1", "true", "yes", "on"}
_SYNCED: set = set()
_SYNC_LOCK = threading.Lock()


@dataclass(frozen=True)
class RemotePaths:
    """한 실행이 쓰는 원격 렌더 경로와 접속 설정."""

    host: str = DEFAULT_HOST
    root: str = DEFAULT_ROOT
    catalog: str = DEFAULT_CATALOG
    ssh_config: Path = field(
        default_factory=lambda: Path(DEFAULT_SSH_CONFIG).expanduser()
    )
    assets_dir: Path = field(default_factory=lambda: Path(core_env.ASSETS_DIR))
    tokens: str = "tokens/semantic.json"
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(
        cls,
        environ: Optional[Dict[str, str]] = None,
        *,
        assets_dir: Optional[Path] = None,
    ) -> "RemotePaths":
        source = os.environ if environ is None else environ
        return cls(
            host=source.get("SONAGI_RENDER_HOST") or DEFAULT_HOST,
            root=source.get("SONAGI_RENDER_ROOT") or DEFAULT_ROOT,
            catalog=source.get("SONAGI_RENDER_CATALOG") or DEFAULT_CATALOG,
            ssh_config=Path(
                source.get("SONAGI_RENDER_SSH_CONFIG") or DEFAULT_SSH_CONFIG
            ).expanduser(),
            assets_dir=Path(
                assets_dir or source.get("SONAGI_ASSETS_DIR") or core_env.ASSETS_DIR
            ),
            timeout=float(source.get("SONAGI_RENDER_TIMEOUT") or DEFAULT_TIMEOUT),
        )

    @property
    def style_dir(self) -> str:
        return f"{self.root}/styles"

    @property
    def tokens_path(self) -> str:
        return f"{self.root}/{self.tokens}"


def enabled(environ: Optional[Dict[str, str]] = None) -> bool:
    """원격 렌더 스위치. 기본은 꺼짐(테스트·로컬 렌더를 깨지 않는다)."""
    source = os.environ if environ is None else environ
    return str(source.get("SONAGI_REMOTE_RENDER", "")).strip().lower() in _TRUTHY


def ssh_argv(paths: RemotePaths) -> Sequence[str]:
    """원격 명령·rsync가 함께 쓰는 ssh 옵션."""
    return (
        "ssh",
        "-F",
        str(paths.ssh_config),
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={min(int(paths.timeout), 30)}",
        paths.host,
    )


def _rsync_argv(paths: RemotePaths) -> Sequence[str]:
    transport = " ".join(shlex.quote(part) for part in ssh_argv(paths)[:-1])
    return ("rsync", "-a", "-e", transport)


def _execute(
    argv: Sequence[Any],
    *,
    paths: RemotePaths,
    run: Optional[Runner] = None,
    timeout: Optional[float] = None,
) -> str:
    command = [str(arg) for arg in argv]
    if run is not None:
        return run(command, timeout=timeout or paths.timeout)
    return core_exec.run(command, timeout=timeout or paths.timeout).stdout


def _remote(paths: RemotePaths, *parts: str) -> str:
    """원격 절대 경로. `root`가 비어 있으면 붙이지 않는다."""
    base = paths.root.rstrip("/")
    return "/".join([base, *[part.strip("/") for part in parts]])


def _remote_arg(paths: RemotePaths, value: str) -> str:
    """원격 셸에 넘길 인자 하나를 안전하게 인용한다."""
    return shlex.quote(value)


def ensure_synced(
    *,
    paths: Optional[RemotePaths] = None,
    run: Optional[Runner] = None,
    force: bool = False,
) -> None:
    """렌더 코드(scripts·styles·tokens·rules)를 원격에 맞춘다(프로세스당 1회).

    `--delete`는 전용 원격 루트 안에서만 적용된다. 이 함수가 건드리는 경로는
    `SONAGI_RENDER_ROOT` 하나뿐이므로 `sonagi-render`(mcp-gateway 소유)와
    섞이지 않는다.
    """
    paths = paths or RemotePaths.from_env()
    assets = Path(paths.assets_dir).resolve()
    key = (paths.host, paths.root, str(assets))
    with _SYNC_LOCK:
        if not force and key in _SYNCED:
            return

        missing = [name for name in SYNC_DIRS if not (assets / name).is_dir()]
        if missing:
            raise FileNotFoundError(
                f"원격 렌더에 필요한 디렉터리가 없습니다: {', '.join(missing)} "
                f"(assets_dir={assets})"
            )

        _execute(
            [*ssh_argv(paths), "mkdir", "-p", paths.root],
            paths=paths,
            run=run,
            timeout=60,
        )
        argv: list[Any] = [*_rsync_argv(paths), "--delete"]
        for name in SYNC_DIRS:
            argv += [f"--include={name}/***"]
        argv += ["--exclude=*", f"{assets}/", f"{paths.host}:{paths.root}/"]
        _execute(argv, paths=paths, run=run)
        _SYNCED.add(key)


def render(
    recipe_path: Path,
    style: str,
    breakpoint: str,
    dark: bool = False,
    out_dir: Optional[Path] = None,
    *,
    paths: Optional[RemotePaths] = None,
    run: Optional[Runner] = None,
    sync: bool = True,
    keep_remote: bool = False,
) -> Dict[str, Any]:
    """레시피 1개를 devops에서 렌더하고 산출물을 로컬 `out_dir`로 가져온다.

    `style`은 스타일 이름(`editorial`) 또는 로컬 `styles/*.json` 경로다.
    반환값은 `assets.render_recipe`와 같은 모양에 원격 정보를 덧붙인다.
    """
    paths = paths or RemotePaths.from_env()
    recipe = Path(recipe_path).expanduser().resolve()
    if not recipe.is_file():
        raise FileNotFoundError(f"레시피가 없습니다: {recipe}")
    style_name = Path(str(style)).stem
    if not style_name:
        raise ValueError("style 이름이 비어 있습니다")

    if sync:
        ensure_synced(paths=paths, run=run)

    out_dir = (
        Path(out_dir).expanduser().resolve()
        if out_dir is not None
        else Path(core_env.out_root("entry_pack")).resolve() / "remote"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    run_id = uuid.uuid4().hex[:12]
    remote_in = _remote(paths, "incoming", run_id)
    remote_out = _remote(paths, "out", run_id)
    remote_recipe = f"{remote_in}/recipe.json"

    _execute(
        [*ssh_argv(paths), "mkdir", "-p", remote_in, remote_out],
        paths=paths,
        run=run,
        timeout=60,
    )
    _execute(
        [*_rsync_argv(paths), str(recipe), f"{paths.host}:{remote_recipe}"],
        paths=paths,
        run=run,
    )

    remote_cmd = " ".join(
        [
            "python3",
            _remote_arg(paths, _remote(paths, "scripts/render_recipe.py")),
            "--recipe",
            _remote_arg(paths, remote_recipe),
            "--style",
            _remote_arg(paths, f"{paths.style_dir}/{style_name}.json"),
            "--catalog",
            _remote_arg(paths, paths.catalog),
            "--tokens",
            _remote_arg(paths, paths.tokens_path),
            "--out",
            _remote_arg(paths, remote_out),
            "--breakpoint",
            _remote_arg(paths, breakpoint),
        ]
    )
    if dark:
        remote_cmd += " --dark"
    _execute([*ssh_argv(paths), remote_cmd], paths=paths, run=run)

    _execute(
        [
            *_rsync_argv(paths),
            f"{paths.host}:{remote_out}/",
            f"{out_dir}/",
        ],
        paths=paths,
        run=run,
    )

    if not keep_remote:
        # 산출물은 이미 로컬에 있다. 실패해도 렌더 결과를 버리지 않는다.
        try:
            _execute(
                [*ssh_argv(paths), "rm", "-rf", remote_in, remote_out],
                paths=paths,
                run=run,
                timeout=60,
            )
        except Exception:  # noqa: BLE001 - 정리는 best-effort
            pass

    svg = next(out_dir.glob("*.svg"), None)
    if svg is None:
        raise RuntimeError(f"원격 렌더 산출물이 없습니다: {out_dir}")
    manifest = next(out_dir.glob("*.manifest.json"), None)
    return {
        "style": style_name,
        "breakpoint": breakpoint,
        "theme": "dark" if dark else "light",
        "svg": str(svg),
        "manifest": str(manifest) if manifest is not None else None,
        "remote": {"host": paths.host, "root": paths.root, "out": remote_out},
    }


def healthcheck(
    *,
    paths: Optional[RemotePaths] = None,
    run: Optional[Runner] = None,
) -> str:
    """devops에서 python3·카탈로그·렌더 스크립트가 보이는지 확인한다."""
    paths = paths or RemotePaths.from_env()
    script = (
        "import json, pathlib, sys;"
        " cat=pathlib.Path(sys.argv[1]);"
        " items=len(json.loads(cat.read_text())['items']);"
        " print(f'{items} items')"
    )
    remote_cmd = " ".join(
        [
            "test -f",
            _remote_arg(paths, _remote(paths, "scripts/render_recipe.py")),
            "&& python3 -c",
            _remote_arg(paths, script),
            _remote_arg(paths, paths.catalog),
        ]
    )
    return _execute([*ssh_argv(paths), remote_cmd], paths=paths, run=run)
