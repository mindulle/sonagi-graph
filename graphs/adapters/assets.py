"""assets 어댑터 — sonagi-assets 렌더·린트·원장 파이프라인 (GRAPH-21).

기존 `asset_craft_graph.py`가 그래프 노드 안에서 직접 조립하던 순서를
**그래프 없는 함수 API**로 옮겼다. LangGraph 배선은 호출부가 맡고, 여기서는
"본문 → 레시피 → 변주 렌더 → 린트 → 후보 선택 → 결정 원장"만 책임진다.

```
content ─ import_content ─▶ recipe.json
                                │
                render_variants ├─▶ SVG + manifest (변주 N)
                                │
                     lint_renders ─▶ 판정(pass/warn/fail)
                                │
                    top_options ─▶ 상위 3안 (FAIL 제외)
                                │
                    write_ledger ─▶ decisions.json
```

사용::

    from graphs.adapters import assets

    paths = assets.AssetPaths.from_env()
    result = assets.run_pipeline(
        "브리프 본문",
        variants,
        run_id="craft-1",
        paths=paths,
    )
    result["options"]           # 사람에게 보여줄 후보
    assets.write_ledger(result["renders"][0], ["style=...: 통과"])

프로세스 실행은 전부 `graphs/core/exec.py`를 거친다. `run` 훅을 넘기면
테스트가 외부 도구 없이 같은 경로를 검증할 수 있다(`profiles`의 `ctx.run`과
같은 규약: argv → stdout 문자열).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import craft  # noqa: E402  - 저장소 루트의 판단 로직(LLM 없음)

from graphs.core import env as core_env  # noqa: E402
from graphs.core import exec as core_exec  # noqa: E402
from graphs.adapters import preflight as preflight_mod  # noqa: E402

#: argv → stdout. 넘기지 않으면 core/exec가 현재 인터프리터로 실행한다.
Runner = Callable[..., str]


@dataclass(frozen=True)
class AssetPaths:
    """한 실행이 참조하는 sonagi-assets 경로.

    `from_env()`는 기존 그래프와 같은 기본값을 쓴다(`SONAGI_ASSETS_DIR`·
    `SONAGI_CATALOG`·`SONAGI_TOKENS`·`SONAGI_CRAFT_OUT`). 실행마다 값을
    바꿔야 하면 dataclass를 직접 만든다(테스트가 그렇게 한다).
    """

    assets_dir: Path = field(default_factory=lambda: Path(core_env.ASSETS_DIR))
    catalog: Path = field(default_factory=lambda: Path(core_env.CATALOG))
    tokens: Path = field(default_factory=lambda: Path(core_env.TOKENS))
    #: `asset_craft` 별칭은 core/env가 계속 들고 있다(기존 경로 보존).
    out_root: Path = field(
        default_factory=lambda: Path(core_env.out_root("asset_craft"))
    )

    @classmethod
    def from_env(cls, graph: str = "asset_craft") -> "AssetPaths":
        """지금 환경변수에서 경로를 다시 읽는다(import 시점 값에 묶이지 않는다)."""
        paths = core_env.load_paths()
        return cls(
            assets_dir=paths.assets_dir,
            catalog=paths.catalog,
            tokens=paths.tokens,
            out_root=Path(core_env.out_root(graph)),
        )


def new_run_id() -> str:
    """실행 디렉터리 이름. 결정론 대상이 아니므로 짧은 난수를 쓴다."""
    return uuid.uuid4().hex[:8]


def _paths(paths: Optional[AssetPaths]) -> AssetPaths:
    return paths if paths is not None else AssetPaths.from_env()


def _execute(
    argv: Sequence[Any],
    *,
    paths: AssetPaths,
    run: Optional[Runner] = None,
    timeout: float = 300,
) -> str:
    """argv를 실행하고 stdout을 돌려준다. 실패는 `CommandError`로 올라온다."""
    command = [str(arg) for arg in argv]
    if run is not None:
        return run(command, timeout=timeout)
    return core_exec.run(
        command, timeout=timeout, cwd=Path(paths.assets_dir).resolve()
    ).stdout


def _script(paths: AssetPaths, name: str) -> Path:
    return Path(paths.assets_dir).resolve() / "scripts" / name


def _absolute(value: Any) -> Path:
    """프로세스 cwd가 `assets_dir`이므로 넘기는 경로는 절대 경로로 고정한다."""
    return Path(value).expanduser().resolve()


# ---------------------------------------------------------------------------
# 단계별 API
# ---------------------------------------------------------------------------


def import_content(
    content: str,
    *,
    run_dir: Path,
    paths: Optional[AssetPaths] = None,
    run: Optional[Runner] = None,
) -> Path:
    """본문(파일 경로 또는 문자열) → `recipe.json`.

    경로가 아니면 `run_dir/brief.md`로 저장한다. 입력이 비면 실행 전에 멈춘다.
    """
    if not content:
        raise ValueError("content(마크다운 경로 또는 본문)가 필요합니다")
    paths = _paths(paths)
    run_dir = _absolute(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    content_path = Path(str(content)).expanduser()
    try:
        is_file = content_path.is_file()
    except OSError:  # 본문이 너무 길면 경로로 볼 수 없다
        is_file = False
    if is_file:
        content_path = content_path.resolve()
    else:
        content_path = run_dir / "brief.md"
        content_path.write_text(str(content), encoding="utf-8")

    recipe = run_dir / "recipe.json"
    _execute(
        [
            sys.executable,
            _script(paths, "import_content.py"),
            content_path,
            "--catalog",
            _absolute(paths.catalog),
            "--out",
            recipe,
        ],
        paths=paths,
        run=run,
    )
    return recipe


def render_recipe(
    recipe: Path,
    variant: Mapping[str, Any],
    *,
    out_dir: Path,
    paths: Optional[AssetPaths] = None,
    run: Optional[Runner] = None,
    preflight: Optional[Mapping[str, Any]] = None,
    check_assets: bool = True,
) -> Dict[str, Any]:
    """변주 1개를 렌더하고 SVG·manifest 경로를 붙여 돌려준다.

    `variant["theme"] == "dark"`면 렌더러에 `--dark`를 넘긴다(web profile과 같은 규약).

    렌더 전에 레시피의 자산 참조를 확인하고, 없는 파일만 걷어낸 레시피로
    렌더한다(GRAPH-24). 결과에는 `skipped_assets`·`preflight`가 함께 붙는다.
    `preflight`를 넘기면 이미 확인된 것으로 보고 다시 ssh/파일 검사를 하지
    않는다(변주 여러 개를 렌더할 때 호출부가 한 번만 확인한다).
    """
    paths = _paths(paths)
    out_dir = _absolute(out_dir)
    recipe = _absolute(recipe)
    report: Mapping[str, Any]
    if preflight is not None:
        report = dict(preflight)
    elif check_assets and recipe.is_file():
        filtered, checked = preflight_mod.preflight_recipe_file(
            recipe,
            catalog_path=paths.catalog,
            assets_dir=paths.assets_dir,
        )
        preflight_mod.ensure_renderable(checked)
        recipe = preflight_mod.write_filtered_recipe(recipe, filtered, checked)
        report = checked
    else:
        # 레시피가 아직 없으면(스텁 렌더러 테스트) 참조를 모을 수 없다.
        report = preflight_mod.empty_report("local")
    argv: List[Any] = [
        sys.executable,
        _script(paths, "render_recipe.py"),
        "--recipe",
        recipe,
        "--style",
        _absolute(paths.assets_dir) / "styles" / f"{variant['style']}.json",
        "--catalog",
        _absolute(paths.catalog),
        "--tokens",
        _absolute(paths.tokens),
        "--out",
        out_dir,
        "--breakpoint",
        variant["breakpoint"],
    ]
    if variant.get("theme") == "dark":
        argv.append("--dark")
    _execute(argv, paths=paths, run=run)

    svg = next(out_dir.glob("*.svg"), None)
    if svg is None:
        raise RuntimeError(f"렌더 산출물이 없습니다: {out_dir}")
    manifest = next(out_dir.glob("*.manifest.json"), None)
    return {
        **dict(variant),
        "svg": str(svg),
        "manifest": str(manifest) if manifest is not None else None,
        "skipped_assets": list(report.get("skipped") or []),
        "preflight": dict(report),
    }


def render_variants(
    variants: Sequence[Mapping[str, Any]],
    *,
    recipe: Path,
    run_dir: Path,
    paths: Optional[AssetPaths] = None,
    run: Optional[Runner] = None,
    preflight: Optional[Mapping[str, Any]] = None,
    check_assets: bool = True,
) -> List[Dict[str, Any]]:
    """변주 전부를 `run_dir/<variant-id>`에 렌더한다.

    자산 preflight는 변주 수와 무관하게 **한 번**만 한다(ssh·카탈로그 비용).
    """
    paths = _paths(paths)
    recipe = _absolute(recipe)
    report: Mapping[str, Any]
    if preflight is not None:
        report = dict(preflight)
    elif check_assets and recipe.is_file():
        filtered, checked = preflight_mod.preflight_recipe_file(
            recipe,
            catalog_path=paths.catalog,
            assets_dir=paths.assets_dir,
        )
        preflight_mod.ensure_renderable(checked)
        recipe = preflight_mod.write_filtered_recipe(recipe, filtered, checked)
        report = checked
    else:
        report = preflight_mod.empty_report("local")
    return [
        render_recipe(
            recipe,
            variant,
            out_dir=Path(run_dir) / str(variant["id"]),
            paths=paths,
            run=run,
            preflight=report,
        )
        for variant in variants
    ]


def lint_render(
    render: Mapping[str, Any],
    *,
    paths: Optional[AssetPaths] = None,
    run: Optional[Runner] = None,
) -> Dict[str, Any]:
    """렌더 1건을 린트하고 선택 단계가 쓰는 필드를 붙인다."""
    paths = _paths(paths)
    argv: List[Any] = [
        sys.executable,
        _script(paths, "lint_render.py"),
        "--svg",
        render["svg"],
        "--tokens",
        _absolute(paths.tokens),
        "--json",
    ]
    if render.get("manifest"):
        argv += ["--manifest", render["manifest"]]
    payload = json.loads(_execute(argv, paths=paths, run=run))
    payload["id"] = render["id"]
    payload["svg"] = render["svg"]
    payload["plan_score"] = render.get("score", 0)
    return payload


def lint_renders(
    renders: Sequence[Mapping[str, Any]],
    *,
    paths: Optional[AssetPaths] = None,
    run: Optional[Runner] = None,
) -> List[Dict[str, Any]]:
    """렌더 전부를 린트한다(순서 유지)."""
    paths = _paths(paths)
    return [lint_render(render, paths=paths, run=run) for render in renders]


def top_options(
    critiques: Sequence[Mapping[str, Any]],
    limit: int = 3,
    *,
    renders: Optional[Sequence[Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """판정 목록 → 사람이 볼 후보. FAIL은 빠지고 점수 내림차순이다.

    `renders`를 주면 후보에 렌더 정체성(`svg`·`style`·`theme`·`breakpoint`)을
    다시 붙인다 — `craft.pick`은 점수만 남기므로 그대로는 보여줄 수 없다.
    """
    options = craft.pick(list(critiques), limit=limit)
    by_id = {str(render.get("id")): render for render in renders or []}
    for option in options:
        render = by_id.get(str(option.get("id")))
        if render is None:
            continue
        for key in ("svg", "manifest", "direction", "style", "breakpoint", "theme"):
            if render.get(key) is not None:
                option[key] = render[key]
    return options


def write_ledger(
    render: Mapping[str, Any],
    decisions: Sequence[str],
    *,
    out: Optional[Path] = None,
    by: Optional[str] = None,
    paths: Optional[AssetPaths] = None,
    run: Optional[Runner] = None,
) -> Path:
    """결정 원장(`decisions.json`)을 남기고 경로를 돌려준다."""
    paths = _paths(paths)
    ledger = (
        _absolute(out)
        if out is not None
        else Path(render["svg"]).resolve().parent / "decisions.json"
    )
    argv: List[Any] = [
        sys.executable,
        _script(paths, "ledger.py"),
        "--svg",
        render["svg"],
    ]
    if render.get("manifest"):
        argv += ["--manifest", render["manifest"]]
    for decision in decisions:
        argv += ["--decision", str(decision)]
    argv += [
        "--by",
        by or os.environ.get("SONAGI_CRAFT_BY", "assets-adapter"),
        "--out",
        ledger,
    ]
    _execute(argv, paths=paths, run=run)
    return ledger


# ---------------------------------------------------------------------------
# 묶음 API
# ---------------------------------------------------------------------------


def run_pipeline(
    content: str,
    variants: Sequence[Mapping[str, Any]],
    *,
    run_id: Optional[str] = None,
    paths: Optional[AssetPaths] = None,
    run: Optional[Runner] = None,
    limit: int = 3,
) -> Dict[str, Any]:
    """본문 → 레시피 → 렌더 → 린트 → 후보를 한 번에 만든다.

    반환값은 `run_dir`·`recipe`·`renders`·`critiques`·`options`다. 후보가
    전부 FAIL이면 `options`가 비고, 선택·원장은 호출부가 결정한다(사람 게이트).
    """
    paths = _paths(paths)
    resolved_id = run_id or new_run_id()
    run_dir = _absolute(paths.out_root) / resolved_id
    run_dir.mkdir(parents=True, exist_ok=True)

    recipe = import_content(content, run_dir=run_dir, paths=paths, run=run)
    filtered, report = preflight_mod.preflight_recipe_file(
        recipe,
        catalog_path=paths.catalog,
        assets_dir=paths.assets_dir,
    )
    preflight_mod.ensure_renderable(report)
    prepared = preflight_mod.write_filtered_recipe(recipe, filtered, report)
    renders = render_variants(
        variants,
        recipe=prepared,
        run_dir=run_dir,
        paths=paths,
        run=run,
        preflight=report,
    )
    critiques = lint_renders(renders, paths=paths, run=run)
    return {
        "run_id": resolved_id,
        "run_dir": run_dir,
        "recipe": recipe,
        "render_recipe": prepared,
        "renders": renders,
        "critiques": critiques,
        "options": top_options(critiques, limit=limit, renders=renders),
        "skipped_assets": list(report.get("skipped") or []),
        "preflight": dict(report),
    }
