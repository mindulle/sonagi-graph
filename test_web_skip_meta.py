"""web pack의 스킵 자산 메타 노출 검증 (GRAPH-30).

GRAPH-24가 렌더 결과에 붙인 `skipped_assets`/`preflight`를 web profile이
합쳐 meta.json·manifest·entry.md에 남기는지 본다. 전부 오프라인이다 —
변환 훅만 monkeypatch하고 ssh·devops·네트워크는 쓰지 않는다.
"""

import json
from pathlib import Path

import pytest
from PIL import Image

from graphs.adapters import preflight
from graphs.core import artifacts as core_artifacts
from graphs.profiles import base
from graphs.profiles import web as web_profile


def _fake_convert(svg, out, width, max_height=3000):
    Image.new("RGB", (100, 140), (250, 249, 245)).save(out, "PNG")


def _fake_jpg(png, out, quality=90):
    Path(out).write_bytes(b"jpg")


def _ctx(run_dir):
    return base.ProfileContext(
        run=lambda *a, **k: "",
        convert_png=_fake_convert,
        to_jpg=_fake_jpg,
        run_dir=Path(run_dir),
    )


def _render(tmp_path, variant_id, *, skipped=None, checked=None, mode="remote"):
    svg = tmp_path / f"{variant_id}.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")
    render = {
        "id": variant_id,
        "svg": str(svg),
        "style": "style-1",
        "theme": "light",
        "direction": "차분한 여백",
    }
    if skipped is not None:
        render["skipped_assets"] = list(skipped)
    if checked is not None:
        render["preflight"] = {
            "mode": mode,
            "checked": checked,
            "skipped": list(skipped or []),
            "renderable": max(0, checked - len(skipped or [])),
        }
    return render


def _pack(tmp_path, renders):
    spec = {"platform": "generic", "skip_llm": True}
    state = {
        "brief": "차분한 티 브랜드 랜딩",
        "spec": spec,
        "profile_ctx": _ctx(tmp_path / "run"),
        "selection": [render["id"] for render in renders],
    }
    return web_profile.WebProfile().pack(renders, spec, state)


def _read(result):
    meta = json.loads(Path(result["meta"]).read_text(encoding="utf-8"))
    manifest = core_artifacts.read_manifest(result["manifest"])
    entry_md = (Path(result["dir"]) / "entry.md").read_text(encoding="utf-8")
    return meta, manifest, entry_md


def test_zero_skips_keeps_fields_present_and_empty(tmp_path):
    result = _pack(tmp_path, [_render(tmp_path, "v1", skipped=[], checked=3)])

    meta, manifest, entry_md = _read(result)
    assert meta["skipped_assets"] == []
    assert meta["preflight"] == {"checked": 3, "skipped": 0, "mode": "remote"}
    assert manifest["meta"]["skipped_assets"] == []
    assert manifest["meta"]["preflight"] == meta["preflight"]
    assert "⚠" not in entry_md
    # entry payload도 같은 값을 나른다(그래프가 읽는 경로).
    assert result["skipped_assets"] == []
    assert result["preflight"] == meta["preflight"]


def test_no_preflight_info_defaults_to_local_empty(tmp_path):
    """preflight를 안 붙인 기존 렌더도 키는 있고 비어 있다(동작 불변)."""
    result = _pack(tmp_path, [_render(tmp_path, "v1")])

    meta, manifest, _ = _read(result)
    assert meta["skipped_assets"] == []
    assert meta["preflight"] == {"checked": 0, "skipped": 0, "mode": "local"}
    assert manifest["meta"]["preflight"] == meta["preflight"]


def test_partial_skips_are_sorted_and_warned(tmp_path):
    renders = [
        _render(
            tmp_path,
            "v1",
            skipped=[{"path": "/srv/lib/b.png", "reason": "missing"}],
            checked=3,
        ),
        _render(
            tmp_path,
            "v2",
            skipped=[{"path": "/srv/lib/a.png", "reason": "not_in_catalog"}],
            checked=3,
        ),
    ]
    result = _pack(tmp_path, renders)

    meta, manifest, entry_md = _read(result)
    assert meta["skipped_assets"] == [
        {"path": "/srv/lib/a.png", "reason": "not_in_catalog"},
        {"path": "/srv/lib/b.png", "reason": "missing"},
    ]
    # 같은 레시피를 공유하므로 checked는 합산하지 않는다.
    assert meta["preflight"] == {"checked": 3, "skipped": 2, "mode": "remote"}
    assert manifest["meta"]["preflight"] == meta["preflight"]
    assert "⚠ 스킵된 자산 2개 — /srv/lib/a.png, /srv/lib/b.png" in entry_md


def test_duplicate_paths_collapse_once(tmp_path):
    renders = [
        _render(
            tmp_path,
            "v1",
            skipped=[{"path": "/srv/lib/x.png", "reason": "missing"}],
            checked=4,
        ),
        _render(
            tmp_path,
            "v2",
            skipped=[{"path": "/srv/lib/x.png", "reason": "missing"}],
            checked=4,
        ),
    ]
    result = _pack(tmp_path, renders)

    meta, manifest, entry_md = _read(result)
    assert meta["skipped_assets"] == [{"path": "/srv/lib/x.png", "reason": "missing"}]
    assert meta["preflight"] == {"checked": 4, "skipped": 1, "mode": "remote"}
    assert manifest["meta"]["skipped_assets"] == meta["skipped_assets"]
    assert entry_md.count("/srv/lib/x.png") == 1


def test_same_path_with_different_reasons_is_deterministic(tmp_path):
    renders = [
        _render(
            tmp_path,
            "v1",
            skipped=[{"path": "/srv/lib/x.png", "reason": "missing"}],
            checked=1,
        ),
        _render(
            tmp_path,
            "v2",
            skipped=[{"path": "/srv/lib/x.png", "reason": "not_in_catalog"}],
            checked=1,
        ),
    ]
    meta = json.loads(
        Path(_pack(tmp_path, renders)["meta"]).read_text(encoding="utf-8")
    )
    assert meta["skipped_assets"] == [{"path": "/srv/lib/x.png", "reason": "missing"}]


def test_warning_line_caps_paths_at_five(tmp_path):
    skipped = [
        {"path": f"/srv/lib/{index:02d}.png", "reason": "missing"} for index in range(7)
    ]
    result = _pack(tmp_path, [_render(tmp_path, "v1", skipped=skipped, checked=7)])
    entry_md = (Path(result["dir"]) / "entry.md").read_text(encoding="utf-8")

    line = next(row for row in entry_md.splitlines() if row.startswith("⚠"))
    assert "스킵된 자산 7개" in line
    assert line.count("/srv/lib/") == 5
    assert line.endswith("외 2개")


def test_all_skipped_error_path_is_untouched(tmp_path, monkeypatch):
    """전부 스킵이면 렌더 어댑터가 멈춘다 — web은 그 오류를 삼키지 않는다."""
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps({"blocks": []}), encoding="utf-8")
    ctx = _ctx(tmp_path / "run")
    ctx.extra["recipe"] = str(recipe)

    def boom(*args, **kwargs):
        raise preflight.PreflightError("렌더 가능한 자산이 없습니다 (1건 누락)")

    monkeypatch.setattr(web_profile.remote_assets, "enabled", lambda *a, **k: True)
    monkeypatch.setattr(web_profile.remote_assets, "render", boom)

    with pytest.raises(preflight.PreflightError, match="렌더 가능한 자산"):
        web_profile.WebProfile().render(
            {"id": "v1", "style": "style-1", "breakpoint": "desktop"},
            ctx,
        )
    assert not (tmp_path / "run" / "pack" / "meta.json").exists()
