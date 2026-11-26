"""자산 preflight·스킵 정책 검증 (GRAPH-24).

실카탈로그는 파일이 없는 항목을 섞는다(표본 200건 중 21건 누락). 누락 하나가
렌더 전체를 죽이지 않도록, 렌더 전에 참조를 모아 없는 것만 걷어내는 계약을
고정한다. ssh·파일 시스템은 전부 `run` 훅과 임시 파일로 대신한다 — 네트워크 없음.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphs.adapters import assets, preflight, remote_assets


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


def _write_catalog(path: Path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    return path


def _item(name: str, path: str, *, kind: str = "illustration"):
    return {
        "id": name,
        "name": name,
        "kind": kind,
        "tags": [f"kind:{kind}"],
        "path": path,
        "license_scope": "bundle",
    }


def _write_recipe(path: Path, blocks):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"name": "preflight", "blocks": blocks}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


_RENDER_STUB = """\
import argparse, json, pathlib
parser = argparse.ArgumentParser()
for flag in ("--recipe", "--style", "--catalog", "--tokens", "--out", "--breakpoint"):
    parser.add_argument(flag)
parser.add_argument("--dark", action="store_true")
args = parser.parse_args()
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
(out / "hero.svg").write_text("<svg/>", encoding="utf-8")
(out / "hero.manifest.json").write_text(
    json.dumps({"recipe": pathlib.Path(args.recipe).name}), encoding="utf-8"
)
"""


@pytest.fixture
def repo(tmp_path):
    """로컬 렌더 스텁 + 카탈로그. 자산 파일은 테스트가 만든다."""
    root = tmp_path / "sonagi-assets"
    script = root / "scripts/render_recipe.py"
    script.parent.mkdir(parents=True)
    script.write_text(_RENDER_STUB, encoding="utf-8")
    (root / "tokens").mkdir()
    (root / "tokens/semantic.json").write_text("{}", encoding="utf-8")
    (root / "styles").mkdir()
    (root / "styles/editorial.json").write_text("{}", encoding="utf-8")

    catalog = _write_catalog(
        root / "catalog/catalog.json",
        [
            _item("present", "library/img/present.svg"),
            _item("missing", "library/img/missing.svg"),
            _item("ghost", "library/img/ghost.svg"),
            _item("arrow-right", "library/icons/arrow-right.svg", kind="icon"),
        ],
    )
    paths = assets.AssetPaths(
        assets_dir=root,
        catalog=catalog,
        tokens=root / "tokens/semantic.json",
        out_root=tmp_path / "out",
    )
    (root / "library/img").mkdir(parents=True)
    (root / "library/img/present.svg").write_text("<svg/>", encoding="utf-8")
    (root / "library/icons").mkdir(parents=True)
    (root / "library/icons/arrow-right.svg").write_text("<svg/>", encoding="utf-8")
    return SimpleNamespace(root=root, paths=paths, catalog=catalog, tmp=tmp_path)


class RenderRecorder:
    """렌더 스텁을 실행하지 않고 argv만 기록하고 산출물을 쓴다."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=300):
        argv = [str(arg) for arg in argv]
        self.calls.append(argv)
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "hero.svg").write_text("<svg/>", encoding="utf-8")
        (out / "hero.manifest.json").write_text("{}", encoding="utf-8")
        return ""

    def recipe_paths(self):
        return [Path(call[call.index("--recipe") + 1]) for call in self.calls]


# ---------------------------------------------------------------------------
# 로컬 모드
# ---------------------------------------------------------------------------


def test_none_missing_keeps_original_recipe(repo):
    recipe = _write_recipe(
        repo.tmp / "recipe.json",
        [{"type": "hero", "illustration": ["present"], "ctaIcon": "present"}],
    )
    run = RenderRecorder()
    rendered = assets.render_recipe(
        recipe,
        {"id": "v1", "style": "editorial", "breakpoint": "desktop", "theme": "light"},
        out_dir=repo.tmp / "out/v1",
        paths=repo.paths,
        run=run,
    )

    # 같은 자산을 두 번 참조해도 한 번만 센다(중복 확인·ssh 제거).
    assert rendered["preflight"] == {
        "mode": "local",
        "checked": 1,
        "skipped": [],
        "renderable": 1,
    }
    assert rendered["skipped_assets"] == []
    # 누락이 없으면 임시 레시피를 만들지 않고 원본을 그대로 렌더한다.
    assert run.recipe_paths() == [recipe.resolve()]


def test_partially_missing_skips_and_continues(repo):
    recipe = _write_recipe(
        repo.tmp / "recipe.json",
        [
            {
                "type": "hero",
                "illustration": ["present", "missing", "ghost"],
                "ctaIcon": "missing",
            },
            {
                "type": "features",
                "items": [
                    {"icon": "present", "title": "남는 항목"},
                    {"icon": "missing", "title": "빠지는 항목"},
                ],
            },
        ],
    )
    run = RenderRecorder()
    rendered = assets.render_recipe(
        recipe,
        {"id": "v1", "style": "editorial", "breakpoint": "desktop", "theme": "light"},
        out_dir=repo.tmp / "out/v1",
        paths=repo.paths,
        run=run,
    )

    report = rendered["preflight"]
    assert report["mode"] == "local"
    assert report["checked"] == 3
    # 누락된 ctaIcon은 실제로 있는 arrow-right로 대체된다(히어로 KeyError 방지).
    assert report["renderable"] == 2
    assert [entry["path"] for entry in report["skipped"]] == [
        str(repo.root / "library/img/missing.svg"),
        str(repo.root / "library/img/ghost.svg"),
    ]
    assert all(entry["reason"] == "missing" for entry in report["skipped"])
    assert rendered["skipped_assets"] == report["skipped"]
    # 렌더는 계속됐고, 렌더러는 걷어낸 레시피를 받았다.
    assert Path(rendered["svg"]).is_file()
    filtered_path = run.recipe_paths()[0]
    assert filtered_path.name == "recipe.preflight.json"
    filtered = json.loads(filtered_path.read_text(encoding="utf-8"))
    assert filtered["blocks"][0]["illustration"] == ["present"]
    assert filtered["blocks"][0]["ctaIcon"] == "arrow-right"
    assert [item["icon"] for item in filtered["blocks"][1]["items"]] == [
        "present",
        "arrow-right",
    ]


def test_all_missing_fails_with_full_list(repo):
    recipe = _write_recipe(
        repo.tmp / "recipe.json",
        [{"type": "hero", "illustration": ["missing", "ghost"]}],
    )
    with pytest.raises(preflight.PreflightError) as error:
        assets.render_recipe(
            recipe,
            {
                "id": "v1",
                "style": "editorial",
                "breakpoint": "desktop",
                "theme": "light",
            },
            out_dir=repo.tmp / "out/v1",
            paths=repo.paths,
            run=RenderRecorder(),
        )

    message = str(error.value)
    assert "2건 누락" in message
    assert str(repo.root / "library/img/missing.svg") in message
    assert str(repo.root / "library/img/ghost.svg") in message


def test_unknown_catalog_name_is_reported_and_skipped(repo):
    recipe = _write_recipe(
        repo.tmp / "recipe.json",
        [{"type": "hero", "illustration": ["present", "not-in-catalog"]}],
    )
    filtered, report = preflight.preflight_local(
        json.loads(recipe.read_text(encoding="utf-8")),
        catalog_path=repo.catalog,
        assets_dir=repo.root,
    )

    assert report["checked"] == 2
    assert report["skipped"] == [
        {"path": "not-in-catalog", "reason": preflight.REASON_NOT_IN_CATALOG}
    ]
    assert filtered["blocks"][0]["illustration"] == ["present"]


def test_literal_path_reference_is_checked(repo):
    literal = repo.tmp / "literal.svg"
    literal.write_text("<svg/>", encoding="utf-8")
    recipe = _write_recipe(
        repo.tmp / "recipe.json",
        [{"type": "hero", "illustration": [str(literal)]}],
    )
    _, report = preflight.preflight_local(
        json.loads(recipe.read_text(encoding="utf-8")),
        catalog_path=repo.catalog,
        assets_dir=repo.root,
    )
    assert report == {"mode": "local", "checked": 1, "skipped": [], "renderable": 1}


def test_no_references_does_not_read_catalog(tmp_path):
    """참조가 없으면 12MB 카탈로그를 읽지 않는다(기존 실행 비용 보존)."""
    recipe = {"blocks": [{"type": "header", "brand": "SONAGI"}]}
    filtered, report = preflight.preflight_local(
        recipe, catalog_path=tmp_path / "does-not-exist.json"
    )
    assert filtered == recipe
    assert report == {"mode": "local", "checked": 0, "skipped": [], "renderable": 0}


def test_report_is_deterministic(repo):
    recipe = json.loads(
        _write_recipe(
            repo.tmp / "recipe.json",
            [{"type": "hero", "illustration": ["missing", "present", "ghost"]}],
        ).read_text(encoding="utf-8")
    )
    first = preflight.preflight_local(
        recipe, catalog_path=repo.catalog, assets_dir=repo.root
    )
    second = preflight.preflight_local(
        recipe, catalog_path=repo.catalog, assets_dir=repo.root
    )
    assert first == second
    assert [entry["path"] for entry in first[1]["skipped"]] == [
        str(repo.root / "library/img/missing.svg"),
        str(repo.root / "library/img/ghost.svg"),
    ]


def test_render_variants_checks_assets_once(repo):
    recipe = _write_recipe(
        repo.tmp / "recipe.json",
        [{"type": "hero", "illustration": ["present", "missing"]}],
    )
    run = RenderRecorder()
    variants = [
        {"id": f"v{index}", "style": "editorial", "breakpoint": "desktop"}
        for index in range(3)
    ]
    rendered = assets.render_variants(
        variants,
        recipe=recipe,
        run_dir=repo.tmp / "out",
        paths=repo.paths,
        run=run,
    )

    assert len(rendered) == 3
    assert all(row["preflight"]["checked"] == 2 for row in rendered)
    assert all(len(row["skipped_assets"]) == 1 for row in rendered)
    # 세 변주 모두 같은 필터된 레시피를 쓴다.
    assert len({str(path) for path in run.recipe_paths()}) == 1


# ---------------------------------------------------------------------------
# 원격 모드 — ssh 1회 배치
# ---------------------------------------------------------------------------


def _remote_paths(tmp_path, catalog):
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text("Host devops-render\n", encoding="utf-8")
    return remote_assets.RemotePaths(
        host="devops-render",
        root="/home/ubuntu/sonagi-entry-render",
        catalog="/srv/sonagi-assets/catalog/catalog.json",
        ssh_config=ssh_config,
        assets_dir=tmp_path / "sonagi-assets",
        local_catalog=catalog,
    )


def _remote_recipe(tmp_path, blocks):
    return _write_recipe(tmp_path / "recipe.json", blocks)


class SshRecorder:
    def __init__(self, found=(), *, error=None, output=None):
        self.calls = []
        self.found = set(found)
        self.error = error
        self.output = output

    def __call__(self, argv, timeout=300):
        argv = [str(arg) for arg in argv]
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        if self.output is not None:
            return self.output
        joined = " ".join(argv)
        if "/bin/sh" in joined:
            lines = []
            for token in argv[-1].split():
                path = token.strip("'")
                if not path.startswith("/"):
                    continue
                status = "ok" if path in self.found else "missing"
                lines.append(f"{status}\t{path}")
            return "\n".join(lines)
        return ""

    def probe_calls(self):
        return [call for call in self.calls if "/bin/sh" in " ".join(call)]


def test_remote_preflight_uses_one_batched_ssh(tmp_path):
    paths = _remote_paths(
        tmp_path,
        _write_catalog(
            tmp_path / "local-catalog.json",
            [
                _item("present", "img/present.svg"),
                _item("missing", "img/missing.svg"),
            ],
        ),
    )
    recipe = _remote_recipe(
        tmp_path, [{"type": "hero", "illustration": ["present", "missing"]}]
    )
    run = SshRecorder(found={f"{paths.library_root}/img/present.svg"})

    filtered, report = preflight.preflight_remote(
        json.loads(recipe.read_text(encoding="utf-8")),
        catalog_path=paths.local_catalog,
        ssh_prefix=remote_assets.ssh_argv(paths),
        to_remote_path=paths.to_remote_path,
        run=run,
    )

    assert report == {
        "mode": "remote",
        "checked": 2,
        "skipped": [
            {"path": f"{paths.library_root}/img/missing.svg", "reason": "missing"}
        ],
        "renderable": 1,
    }
    assert filtered["blocks"][0]["illustration"] == ["present"]
    # 파일마다 ssh를 띄우지 않는다 — 확인 전체가 한 번의 호출이다.
    probe_calls = run.probe_calls()
    assert len(probe_calls) == 1
    command = probe_calls[0][-1]
    assert command.count("test") <= 1 and "for p in" in command
    assert f"{paths.library_root}/img/present.svg" in command
    assert f"{paths.library_root}/img/missing.svg" in command


def test_remote_preflight_ssh_failure_is_loud(tmp_path):
    paths = _remote_paths(
        tmp_path,
        _write_catalog(
            tmp_path / "local-catalog.json", [_item("present", "img/present.svg")]
        ),
    )
    recipe = _remote_recipe(tmp_path, [{"type": "hero", "illustration": ["present"]}])
    with pytest.raises(preflight.PreflightError, match="ssh"):
        preflight.preflight_remote(
            json.loads(recipe.read_text(encoding="utf-8")),
            catalog_path=paths.local_catalog,
            ssh_prefix=remote_assets.ssh_argv(paths),
            to_remote_path=paths.to_remote_path,
            run=SshRecorder(error=OSError("connection refused")),
        )


def test_remote_preflight_unparsable_output_is_loud(tmp_path):
    paths = _remote_paths(
        tmp_path,
        _write_catalog(
            tmp_path / "local-catalog.json", [_item("present", "img/present.svg")]
        ),
    )
    recipe = _remote_recipe(tmp_path, [{"type": "hero", "illustration": ["present"]}])
    with pytest.raises(preflight.PreflightError, match="해석"):
        preflight.preflight_remote(
            json.loads(recipe.read_text(encoding="utf-8")),
            catalog_path=paths.local_catalog,
            ssh_prefix=remote_assets.ssh_argv(paths),
            to_remote_path=paths.to_remote_path,
            run=SshRecorder(output=""),  # 응답 없음 = 상태 불명
        )


def test_remote_paths_map_local_library_to_devops(tmp_path):
    paths = _remote_paths(tmp_path, tmp_path / "catalog.json")
    assert paths.library_root == "/srv/sonagi-assets/library"
    assert (
        paths.to_remote_path("/home/ubuntu/sonagi-assets/library/img/a.svg")
        == "/srv/sonagi-assets/library/img/a.svg"
    )
    assert (
        paths.to_remote_path("/app/data/img/b.svg")
        == "/srv/sonagi-assets/library/img/b.svg"
    )
    assert paths.to_remote_path("img/c.svg") == "/srv/sonagi-assets/library/img/c.svg"
    # 이미 원격 경로면 그대로 둔다.
    remote = "/srv/sonagi-assets/library/img/d.svg"
    assert paths.to_remote_path(remote) == remote


class RemoteRenderRecorder(SshRecorder):
    def __init__(self, found=()):
        super().__init__(found=found)

    def __call__(self, argv, timeout=300):
        argv = [str(arg) for arg in argv]
        if argv[0] == "rsync" and ":" not in argv[-1]:
            dest = Path(argv[-1])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "hero.svg").write_text("<svg/>", encoding="utf-8")
            (dest / "hero.manifest.json").write_text("{}", encoding="utf-8")
            self.calls.append(argv)
            return ""
        return super().__call__(argv, timeout=timeout)


def test_remote_render_returns_preflight_fields(tmp_path):
    root = tmp_path / "sonagi-assets"
    for name in remote_assets.SYNC_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    catalog = _write_catalog(
        tmp_path / "local-catalog.json",
        [
            _item("present", "img/present.svg"),
            _item("missing", "img/missing.svg"),
        ],
    )
    paths = _remote_paths(tmp_path, catalog)
    recipe = _remote_recipe(
        tmp_path, [{"type": "hero", "illustration": ["present", "missing"]}]
    )
    run = RemoteRenderRecorder(found={f"{paths.library_root}/img/present.svg"})

    rendered = remote_assets.render(
        recipe,
        "editorial",
        "desktop",
        out_dir=tmp_path / "out/v1",
        paths=paths,
        run=run,
        sync=False,
    )

    assert rendered["preflight"]["mode"] == "remote"
    assert rendered["preflight"]["checked"] == 2
    assert rendered["preflight"]["renderable"] == 1
    assert rendered["skipped_assets"] == [
        {"path": f"{paths.library_root}/img/missing.svg", "reason": "missing"}
    ]
    assert Path(rendered["svg"]).is_file()
    assert len(run.probe_calls()) == 1


def test_missing_cta_icon_is_substituted(repo):
    """히어로 렌더러는 ctaIcon 키를 무조건 읽는다 — 지우지 말고 대체한다."""
    recipe = _write_recipe(
        repo.tmp / "recipe.json",
        [{"type": "hero", "ctaIcon": "missing"}],
    )
    filtered, report = preflight.preflight_local(
        json.loads(recipe.read_text(encoding="utf-8")),
        catalog_path=repo.catalog,
        assets_dir=repo.root,
    )

    assert filtered["blocks"][0]["ctaIcon"] == "arrow-right"
    assert report["checked"] == 1
    assert report["skipped"] == [
        {"path": str(repo.root / "library/img/missing.svg"), "reason": "missing"}
    ]
    # 대체 아이콘이 있으므로 렌더 가능으로 센다(조기 실패 방지).
    assert report["renderable"] == 1
    preflight.ensure_renderable(report)


def test_missing_cta_icon_without_icon_set_fails(tmp_path):
    catalog = _write_catalog(
        tmp_path / "catalog.json", [_item("present", "img/present.svg")]
    )
    with pytest.raises(preflight.PreflightError, match="ctaIcon"):
        preflight.preflight_local(
            {"blocks": [{"type": "hero", "ctaIcon": "nope"}]},
            catalog_path=catalog,
            assets_dir=tmp_path,
        )


def test_legacy_content_map_is_preflighted(repo):
    """레거시 `content` 맵 레시피도 블록으로 정규화해 누락을 걷어낸다."""
    recipe = {
        "name": "legacy",
        "content": {
            "hero": {
                "illustration": ["present", "missing"],
                "ctaIcon": "arrow-right",
            }
        },
    }
    filtered, report = preflight.preflight_local(
        recipe, catalog_path=repo.catalog, assets_dir=repo.root
    )

    assert report["checked"] == 3
    assert "content" not in filtered
    assert filtered["blocks"][0]["type"] == "hero"
    assert filtered["blocks"][0]["illustration"] == ["present"]
    assert filtered["blocks"][0]["ctaIcon"] == "arrow-right"


def test_gallery_all_missing_is_removed(repo):
    recipe = {
        "blocks": [
            {"type": "header", "brand": "SONAGI"},
            {"type": "gallery", "items": ["missing", "ghost"]},
        ]
    }
    filtered, report = preflight.preflight_local(
        recipe, catalog_path=repo.catalog, assets_dir=repo.root
    )

    assert report["checked"] == 2
    assert [block["type"] for block in filtered["blocks"]] == ["header"]
