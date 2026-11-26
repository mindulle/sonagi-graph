"""remote_assets 어댑터 — devops 렌더 위임 (GRAPH-24).

실제 ssh·rsync 없이 `run` 훅으로 argv를 검증한다. devops가 없어도 도는
L2(fixture E2E) 테스트다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphs.adapters import remote_assets
from graphs.profiles import base
from graphs.profiles import web


@pytest.fixture(autouse=True)
def _clear_sync_cache():
    remote_assets._SYNCED.clear()
    yield
    remote_assets._SYNCED.clear()


@pytest.fixture
def render_repo(tmp_path):
    root = tmp_path / "sonagi-assets"
    for name in ("scripts", "styles", "tokens", "rules"):
        (root / name).mkdir(parents=True)
    (root / "scripts/render_recipe.py").write_text("# stub\n", encoding="utf-8")
    (root / "styles/editorial.json").write_text("{}", encoding="utf-8")
    (root / "tokens/semantic.json").write_text("{}", encoding="utf-8")
    (root / "rules/ruleset.json").write_text("{}", encoding="utf-8")
    recipe = tmp_path / "recipe.json"
    recipe.write_text('{"name": "hero"}', encoding="utf-8")
    return root, recipe


def _paths(tmp_path, root) -> remote_assets.RemotePaths:
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host devops-render\n", encoding="utf-8")
    return remote_assets.RemotePaths(
        host="devops-render",
        root="/home/ubuntu/sonagi-entry-render",
        catalog="/srv/sonagi-assets/catalog/catalog.json",
        ssh_config=ssh_config,
        assets_dir=root,
    )


class Recorder:
    def __init__(self, *, write_outputs: bool = False):
        self.calls = []
        self.write_outputs = write_outputs

    def __call__(self, argv, timeout=300):
        argv = [str(arg) for arg in argv]
        self.calls.append(argv)
        if self.write_outputs and argv[0] == "rsync":
            dest = Path(argv[-1])
            # fetch(`host:remote/` → `local/`)만 로컬 경로다.
            if ":" not in argv[-1]:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "hero.svg").write_text(
                    '<svg width="1440" height="900"></svg>', encoding="utf-8"
                )
                (dest / "hero.manifest.json").write_text(
                    json.dumps({"name": "hero"}), encoding="utf-8"
                )
        return ""

    def commands(self, program: str):
        return [call for call in self.calls if call[0] == program]


def test_enabled_is_opt_in():
    assert remote_assets.enabled({}) is False
    assert remote_assets.enabled({"SONAGI_REMOTE_RENDER": "0"}) is False
    assert remote_assets.enabled({"SONAGI_REMOTE_RENDER": "1"}) is True
    assert remote_assets.enabled({"SONAGI_REMOTE_RENDER": "yes"}) is True


def test_paths_from_env_and_assets_override(tmp_path):
    paths = remote_assets.RemotePaths.from_env(
        {
            "SONAGI_RENDER_HOST": "render-host",
            "SONAGI_RENDER_ROOT": "/srv/render",
            "SONAGI_RENDER_CATALOG": "/srv/cat.json",
            "SONAGI_RENDER_SSH_CONFIG": str(tmp_path / "cfg"),
            "SONAGI_RENDER_TIMEOUT": "42",
        },
        assets_dir=tmp_path / "assets",
    )
    assert paths.host == "render-host"
    assert paths.root == "/srv/render"
    assert paths.catalog == "/srv/cat.json"
    assert paths.assets_dir == tmp_path / "assets"
    assert paths.timeout == 42
    assert paths.style_dir == "/srv/render/styles"
    assert paths.tokens_path == "/srv/render/tokens/semantic.json"


def test_ensure_synced_copies_only_render_code(tmp_path, render_repo):
    root, _ = render_repo
    paths = _paths(tmp_path, root)
    run = Recorder()
    remote_assets.ensure_synced(paths=paths, run=run)

    ssh, rsync = run.commands("ssh"), run.commands("rsync")
    assert ssh and ssh[0][-3:] == ["mkdir", "-p", paths.root]
    assert rsync
    sync = rsync[0]
    assert "--delete" in sync
    assert "--exclude=*" in sync
    for name in remote_assets.SYNC_DIRS:
        assert f"--include={name}/***" in sync
    assert sync[-2] == f"{root}/"
    assert sync[-1] == f"{paths.host}:{paths.root}/"

    # 같은 프로세스에서 두 번째 호출은 재동기화하지 않는다.
    remote_assets.ensure_synced(paths=paths, run=run)
    assert len(run.commands("rsync")) == 1
    remote_assets.ensure_synced(paths=paths, run=run, force=True)
    assert len(run.commands("rsync")) == 2


def test_ensure_synced_requires_render_dirs(tmp_path):
    paths = _paths(tmp_path, tmp_path / "empty")
    with pytest.raises(FileNotFoundError, match="scripts"):
        remote_assets.ensure_synced(paths=paths, run=Recorder())


def test_render_round_trip(tmp_path, render_repo):
    root, recipe = render_repo
    paths = _paths(tmp_path, root)
    out_dir = tmp_path / "out" / "v1"
    run = Recorder(write_outputs=True)

    result = remote_assets.render(
        recipe,
        "styles/editorial.json",
        "mobile",
        dark=True,
        out_dir=out_dir,
        paths=paths,
        run=run,
    )

    assert result["style"] == "editorial"
    assert result["theme"] == "dark"
    assert result["svg"] == str(out_dir / "hero.svg")
    assert result["manifest"] == str(out_dir / "hero.manifest.json")
    assert result["remote"]["host"] == "devops-render"

    ssh = run.commands("ssh")
    render_cmd = next(call for call in ssh if "render_recipe.py" in " ".join(call))
    joined = " ".join(render_cmd)
    assert "--dark" in joined
    assert paths.catalog in joined
    assert f"{paths.root}/styles/editorial.json" in joined
    assert f"{paths.root}/tokens/semantic.json" in joined
    assert "--breakpoint mobile" in joined

    # 레시피는 원격 incoming/<id>/recipe.json으로 복사된다.
    push = next(
        call
        for call in run.commands("rsync")
        if any("recipe.json" in part for part in call)
    )
    assert push[-1].startswith(f"{paths.host}:{paths.root}/incoming/")
    # 정리(rm -rf)는 best-effort로 한 번 돈다.
    assert any("rm" in call for call in ssh)


def test_render_fails_when_no_svg_is_fetched(tmp_path, render_repo):
    root, recipe = render_repo
    with pytest.raises(RuntimeError, match="산출물"):
        remote_assets.render(
            recipe,
            "editorial",
            "desktop",
            out_dir=tmp_path / "out",
            paths=_paths(tmp_path, root),
            run=Recorder(write_outputs=False),
        )


def test_healthcheck_reads_remote_catalog(tmp_path, render_repo):
    root, _ = render_repo
    paths = _paths(tmp_path, root)
    run = Recorder()
    remote_assets.healthcheck(paths=paths, run=run)
    joined = " ".join(run.commands("ssh")[0])
    assert "test -f" in joined
    assert paths.catalog in joined


def test_web_profile_delegates_to_remote_when_enabled(
    tmp_path, monkeypatch, render_repo
):
    root, recipe = render_repo
    monkeypatch.setenv("SONAGI_REMOTE_RENDER", "1")
    calls = {}

    def fake_render(recipe_path, style, breakpoint, dark, out_dir, **kwargs):
        calls.update(
            {
                "recipe": Path(recipe_path),
                "style": style,
                "breakpoint": breakpoint,
                "dark": dark,
                "out_dir": Path(out_dir),
            }
        )
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        svg = out / "hero.svg"
        svg.write_text("<svg/>", encoding="utf-8")
        return {"svg": str(svg), "manifest": None}

    monkeypatch.setattr(web, "_ensure_recipe", lambda ctx, run_dir: recipe)
    monkeypatch.setattr(remote_assets, "render", fake_render)

    ctx = base.ProfileContext(
        run=lambda *a, **k: "",
        out_root=tmp_path,
        assets_dir=root,
        run_dir=tmp_path / "run",
    )
    variant = {
        "id": "v1",
        "style": "editorial",
        "breakpoint": "desktop",
        "theme": "dark",
    }
    rendered = web.WebProfile().render(variant, ctx)

    assert rendered["renderer"] == "devops"
    assert rendered["style"] == "editorial"
    assert calls["style"] == "editorial"
    assert calls["dark"] is True
    assert calls["out_dir"] == tmp_path / "run" / "v1"
