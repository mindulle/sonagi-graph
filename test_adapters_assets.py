"""assets 어댑터 검증 — L2(fixture E2E) + L3(결정론) (GRAPH-21).

`asset_craft` 그래프가 하던 일을 함수 API로 옮겼으므로, 그래프 대신 어댑터를
직접 호출한다. `sonagi-assets` 실저장소·실렌더·네트워크 없이 돈다 —
`asset_repo` 픽스처가 `scripts/*.py`를 결정론 스텁으로 대신한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import craft
from graphs.adapters import assets

BRIEF = "민들레 홀씨 미니멀 인쇄물"

#: L3 골든 — 고정 브리프의 IR·참조·후보 계획 지문(결정론이 깨지면 갱신 대상).
PLAN_SHA256 = "97103695c9cad8a23c9ab6fdb96ba8f821aec8091cd57f8fc0af3df028c14afa"


def _variants(repo, *, brief: str = BRIEF):
    styles = craft.load_registry(repo.paths.assets_dir)
    ir = craft.parse_brief(brief)
    return craft.plan_variants(
        ir, styles, limit=3, breakpoints=("mobile",), themes=("light",)
    )


def _pipeline(repo, *, run_id: str, content: str = "본문"):
    return assets.run_pipeline(
        content,
        _variants(repo),
        run_id=run_id,
        paths=repo.paths,
    )


# ---------------------------------------------------------------------------
# 경로 계약
# ---------------------------------------------------------------------------


def test_asset_paths_from_env_matches_core_env(monkeypatch):
    monkeypatch.delenv("SONAGI_OUT_ROOT", raising=False)
    monkeypatch.delenv("SONAGI_CRAFT_OUT", raising=False)
    paths = assets.AssetPaths.from_env()
    assert paths.assets_dir == assets.core_env.ASSETS_DIR
    assert paths.catalog == assets.core_env.CATALOG
    assert paths.tokens == assets.core_env.TOKENS
    # 삭제된 asset_craft 그래프의 출력 루트를 그대로 이어받는다.
    assert paths.out_root == Path("/tmp/asset_craft")

    monkeypatch.setenv("SONAGI_CRAFT_OUT", "/srv/craft")
    assert assets.AssetPaths.from_env().out_root == Path("/srv/craft")
    # 공통 루트가 그래프 별칭을 이긴다.
    monkeypatch.setenv("SONAGI_OUT_ROOT", "/srv/all")
    assert assets.AssetPaths.from_env().out_root == Path("/srv/all")


# ---------------------------------------------------------------------------
# L2 — 렌더 → 린트 → 후보
# ---------------------------------------------------------------------------


def test_pipeline_renders_lints_and_selects(asset_repo):
    """content→recipe→render→lint→options 계약을 고정한다(사람 게이트는 호출부)."""
    result = _pipeline(asset_repo, run_id="adapt-e2e")

    assert result["run_id"] == "adapt-e2e"
    assert Path(result["recipe"]).is_file()
    assert result["run_dir"] == asset_repo.out / "adapt-e2e"

    renders = result["renders"]
    assert len(renders) == 3
    for render in renders:
        assert Path(render["svg"]).is_file()
        assert Path(render["manifest"]).is_file()
        assert Path(render["svg"]).read_text(encoding="utf-8").startswith("<svg")

    critiques = {row["id"]: row for row in result["critiques"]}
    assert set(critiques) == {variant["id"] for variant in renders}
    assert all(
        {"verdict", "findings", "plan_score", "id", "svg"} <= set(row)
        for row in critiques.values()
    )

    options = result["options"]
    assert options
    assert all(option["verdict"] != "fail" for option in options)
    assert [o["score"] for o in options] == sorted(
        (o["score"] for o in options), reverse=True
    )
    assert "risk-loud-mobile-light" not in {o["id"] for o in options}
    # 후보는 보여줄 수 있어야 한다 — 렌더 경로와 축이 붙는다.
    assert all(Path(option["svg"]).is_file() for option in options)
    assert {option["theme"] for option in options} == {"light"}
    assert {option["breakpoint"] for option in options} == {"mobile"}


def test_dark_theme_passes_dark_flag_to_renderer(asset_repo):
    """어댑터는 변주의 테마를 렌더러에 넘긴다(라이트로 조용히 대체하지 않는다)."""
    calls = []

    def fake_run(argv, timeout=300, **kwargs):
        calls.append(argv)
        out = Path(argv[argv.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "render.svg").write_text("<svg/>", encoding="utf-8")
        (out / "render.manifest.json").write_text("{}", encoding="utf-8")
        return ""

    variant = {
        **_variants(asset_repo)[0],
        "id": "dark-x",
        "theme": "dark",
        "breakpoint": "desktop",
    }
    assets.render_recipe(
        asset_repo.out / "recipe.json",
        variant,
        out_dir=asset_repo.out / "dark-x",
        paths=asset_repo.paths,
        run=fake_run,
    )
    assert "--dark" in calls[0]
    assert calls[0][calls[0].index("--breakpoint") + 1] == "desktop"

    calls.clear()
    assets.render_recipe(
        asset_repo.out / "recipe.json",
        {**variant, "id": "light-x", "theme": "light"},
        out_dir=asset_repo.out / "light-x",
        paths=asset_repo.paths,
        run=fake_run,
    )
    assert "--dark" not in calls[0]


def test_web_style_registry_drives_breakpoint_independently(asset_repo):
    """어댑터는 변주를 그대로 렌더한다 — 브레이크포인트는 호출부가 정한다."""
    styles = craft.load_registry(asset_repo.paths.assets_dir)
    ir = craft.parse_brief("따뜻한 랜딩")
    variants = craft.plan_variants(
        ir, styles, limit=3, breakpoints=("desktop",), themes=("light",)
    )
    result = assets.run_pipeline(
        "본문", variants, run_id="desktop-run", paths=asset_repo.paths
    )
    assert {variant["breakpoint"] for variant in result["renders"]} == {"desktop"}
    assert len(result["renders"]) == 3


def test_write_ledger_records_style_and_scout_decisions(asset_repo):
    """원장(decisions.json)에 판정·스카우트 근거가 순서대로 남는다."""
    result = _pipeline(asset_repo, run_id="adapt-ledger")
    render = result["renders"][-1]

    ledger = assets.write_ledger(
        render,
        ["style=warn-soft: 판정 warn · 점수 95", "scout=4건 [icon:1]: Seed Glyph"],
        by="craft-graph",
        paths=asset_repo.paths,
    )

    assert ledger == Path(render["svg"]).parent / "decisions.json"
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["by"] == "craft-graph"
    assert payload["svg"] == Path(render["svg"]).name
    assert any(decision.startswith("style=") for decision in payload["decisions"])
    assert any(decision.startswith("scout=") for decision in payload["decisions"])


def test_top_options_drops_fail_and_keeps_warn():
    """FAIL은 후보에서만 빠지고 경고는 남는다(린트 결과를 지우지 않는다)."""
    critiques = [
        {"id": "a-pass", "verdict": "pass", "findings": []},
        {
            "id": "b-fail",
            "verdict": "fail",
            "findings": [{"rule": "contrast", "severity": "fail"}],
        },
        {
            "id": "c-warn",
            "verdict": "warn",
            "findings": [{"rule": "spacing", "severity": "warn"}],
        },
    ]

    options = assets.top_options(critiques)

    assert [option["id"] for option in options] == ["a-pass", "c-warn"]
    assert options[1]["rules"] == ["spacing"]


# ---------------------------------------------------------------------------
# 오류 경로
# ---------------------------------------------------------------------------


def test_import_content_requires_content(asset_repo):
    with pytest.raises(ValueError, match="content"):
        assets.import_content(
            "", run_dir=asset_repo.out / "no-content", paths=asset_repo.paths
        )


def test_render_recipe_fails_when_outputs_are_missing(asset_repo):
    """렌더러가 아무것도 만들지 않으면 조용히 넘어가지 않는다."""
    recipe = assets.import_content(
        "본문", run_dir=asset_repo.out / "empty", paths=asset_repo.paths
    )
    variant = _variants(asset_repo)[0]
    with pytest.raises(RuntimeError, match="렌더 산출물이 없습니다"):
        assets.render_recipe(
            recipe,
            variant,
            out_dir=asset_repo.out / "empty" / variant["id"],
            paths=asset_repo.paths,
            run=lambda argv, timeout=300: "",
        )


# ---------------------------------------------------------------------------
# L3 — 결정론
# ---------------------------------------------------------------------------


def _normalized(result):
    """경로가 다른 두 실행을 비교 가능하게 만든다(내용·판정만 남긴다)."""
    return {
        "svg": [
            Path(row["svg"]).read_text(encoding="utf-8") for row in result["renders"]
        ],
        "manifest": [
            json.loads(Path(row["manifest"]).read_text(encoding="utf-8"))
            for row in result["renders"]
        ],
        "critiques": [
            {key: row[key] for key in ("id", "verdict", "findings", "plan_score")}
            for row in result["critiques"]
        ],
        "options": [
            {key: row[key] for key in ("id", "verdict", "score", "rules")}
            for row in result["options"]
        ],
    }


def test_pipeline_is_deterministic(asset_repo):
    """L3: 같은 입력 2회 → 렌더 내용·린트·후보가 동일하다."""
    first = _normalized(_pipeline(asset_repo, run_id="det-a"))
    second = _normalized(_pipeline(asset_repo, run_id="det-b"))
    assert first == second
    assert first["svg"]  # 렌더가 비지 않았다


def _plan_snapshot(repo):
    ir = craft.parse_brief(BRIEF)
    items = json.loads(Path(repo.catalog).read_text(encoding="utf-8"))["items"]
    report = craft.scout_references(ir, items, limit=craft.REF_LIMIT)
    return {
        "ir": ir,
        "references": report["references"],
        "scout_report": report,
        "variants": _variants(repo),
    }


def test_l3_plan_snapshot_is_deterministic_and_matches_golden(asset_repo):
    """L3: 같은 브리프 2회 → IR·참조·후보가 동일하고, 골든 계획과도 일치한다."""
    first, second = _plan_snapshot(asset_repo), _plan_snapshot(asset_repo)

    assert first == second
    canonical = json.dumps(first, sort_keys=True, ensure_ascii=False)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == PLAN_SHA256
    assert [variant["id"] for variant in first["variants"]] == [
        "minimal-clean-mobile-light",
        "risk-loud-mobile-light",
        "warn-soft-mobile-light",
    ]
    assert [ref["id"] for ref in first["references"]] == [
        "ill-1",
        "ico-1",
        "ill-2",
        "typo-1",
    ]


def test_scout_reports_shortfall_without_topic_terms(asset_repo, tmp_path):
    """주제어가 없으면 다른 역할로 억지로 메우지 않고 shortfall에 이유를 남긴다."""
    catalog = tmp_path / "catalog-empty.json"
    catalog.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "x1",
                        "name": "Unrelated",
                        "category": "painting",
                        "ext": "png",
                        "topics": ["unrelated"],
                        "license_scope": "bundle",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    items = json.loads(catalog.read_text(encoding="utf-8"))["items"]
    report = craft.scout_references(
        craft.parse_brief("미니멀 인쇄물"), items, limit=craft.REF_LIMIT
    )

    assert report["references"] == []
    assert report["terms"] == []
    assert report["shortfall"]
    assert "주제어 없음" in report["shortfall"][0]["reason"]
