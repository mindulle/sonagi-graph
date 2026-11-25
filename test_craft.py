"""Craft 판단 로직 검증 — LLM·서비스 없이 도는 부분."""

import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import craft  # noqa: E402

ASSETS = os.environ.get("SONAGI_ASSETS_DIR", "/home/ubuntu/sonagi-assets")
needs_assets = pytest.mark.skipif(
    not os.path.isfile(os.path.join(ASSETS, "registry/styles.json")),
    reason="sonagi-assets 명부 없음",
)

STYLES = [
    {
        "id": "minimal@1",
        "name": "minimal",
        "axes": {"palette": "minimal", "type": "archivo-airy"},
    },
    {
        "id": "brutalist@2",
        "name": "brutalist",
        "axes": {"palette": "brutalist", "type": "archivo-caps-loud"},
    },
    {
        "id": "victorian@3",
        "name": "victorian",
        "axes": {"palette": "victorian", "type": "cormorant-gowun"},
    },
]


def test_parse_brief_string():
    ir = craft.parse_brief("따뜻하고 고급스러운 인쇄물. 금지: 네온")
    assert "따뜻" in ir["tones"] and "고급" in ir["tones"]
    assert ir["media"] == "print"
    assert any("네온" in item for item in ir["must_not"])


def test_parse_brief_dict_fills_blanks():
    ir = craft.parse_brief({"purpose": "랜딩", "tones": ["차분"]})
    assert ir["tones"] == ["차분"]
    assert ir["media"] == "web"
    assert ir["must_not"] == []


def test_plan_variants_scores_tones():
    ir = craft.parse_brief("미니멀한 화면")
    variants = craft.plan_variants(
        ir, STYLES, limit=1, breakpoints=("mobile",), themes=("light",)
    )
    assert len(variants) == 1
    assert variants[0]["style"] == "minimal"
    assert variants[0]["id"] == "minimal-mobile-light"


def test_plan_variants_falls_back_when_no_match():
    ir = {"purpose": "우주", "tones": ["없는톤"], "media": "web"}
    variants = craft.plan_variants(ir, STYLES, limit=2)
    assert len(variants) == 2
    assert all(v["breakpoint"] == "desktop" for v in variants)


def test_rank_candidates_orders_and_penalizes():
    rows = craft.rank_candidates(
        [
            {"id": "a", "verdict": "pass", "findings": []},
            {
                "id": "b",
                "verdict": "warn",
                "findings": [{"rule": "text.overflow", "severity": "warn"}],
            },
            {
                "id": "c",
                "verdict": "fail",
                "findings": [{"rule": "block.overlap", "severity": "fail"}],
            },
        ]
    )
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert rows[0]["score"] == 100 and rows[1]["score"] == 95 and rows[2]["score"] == 75


def test_pick_excludes_failures():
    picked = craft.pick(
        [
            {"id": "a", "verdict": "pass", "findings": []},
            {
                "id": "c",
                "verdict": "fail",
                "findings": [{"rule": "block.overlap", "severity": "fail"}],
            },
        ]
    )
    assert [row["id"] for row in picked] == ["a"]


@needs_assets
def test_registry_is_usable():
    styles = craft.load_registry(ASSETS)
    assert len(styles) > 10
    assert all(style["id"].count("@") == 1 for style in styles)


@needs_assets
def test_minimal_brief_picks_brand_neutral_styles():
    styles = craft.load_registry(ASSETS)
    ir = craft.parse_brief("미니멀하고 차분한 랜딩")
    names = {v["style"] for v in craft.plan_variants(ir, styles, limit=3)}
    assert names & {"minimal", "swiss", "mint-swiss", "ocean-swiss", "forest-paper"}


def _ref(
    name,
    *,
    category="ui-component",
    ext="svg",
    topics=(),
    tags=(),
    scope="bundle",
    kind="asset",
    **extra,
):
    item = {
        "name": name,
        "kind": kind,
        "category": category,
        "ext": ext,
        "topics": list(topics),
        "tags": list(tags),
        "license_scope": scope,
        "license": "mit",
    }
    item.update(extra)
    return item


def test_extract_topic_terms_pulls_subject_nouns():
    terms = craft.extract_topic_terms(craft.parse_brief("민들레 홀씨 유리 문진"))
    assert terms == ["dandelion", "seed", "glass", "paperweight"]


def test_korean_particles_do_not_break_terms():
    terms = craft.extract_topic_terms({"purpose": "등대가 있는 바다를 담은 그림"})
    assert terms == ["lighthouse", "sea", "ocean"]


def test_tone_and_medium_words_are_not_topic_terms():
    assert craft.extract_topic_terms(craft.parse_brief("미니멀하고 차분한 랜딩")) == []
    assert (
        craft.extract_topic_terms(craft.parse_brief("따뜻하고 고급스러운 인쇄물")) == []
    )
    assert craft.extract_topic_terms(craft.parse_brief("대담한 포스터")) == []


def test_generic_short_terms_are_rejected():
    for word in ("none", "rule", "arch", "deco"):
        assert not craft.is_topic_term(word)
    # 4자 이하 미등록 낱말은 주제어가 아니다. 사전에 올린 short term만 통과.
    assert not craft.is_topic_term("calm")
    assert craft.is_topic_term("glass")
    assert craft.is_topic_term("seed")  # 사전 등재


def test_name_match_is_token_exact_not_substring():
    items = [_ref("lighthouse-keeper", category="typography", ext="ttf")]
    # 'house'는 'lighthouse'의 부분 문자열이지만 토큰이 아니다.
    assert craft.select_references({"purpose": "house"}, items) == []
    refs = craft.select_references({"purpose": "등대"}, items)
    assert [ref["name"] for ref in refs] == ["lighthouse-keeper"]


def test_archivo_and_border_none_never_match():
    items = [
        _ref("Archivo-Black", category="typography", ext="ttf"),
        _ref("border-none", tags=["kind:icon", "topic:icon"], topics=["icon"]),
        _ref("cell-signal-none", tags=["kind:icon", "topic:icon"], topics=["icon"]),
        _ref("BigelowRules-Regular", category="typography", ext="ttf"),
    ]
    for purpose in (
        "미니멀하고 차분한 랜딩",
        "따뜻하고 고급스러운 인쇄물",
        "대담한 포스터",
    ):
        assert craft.select_references(craft.parse_brief(purpose), items) == []


def _glass_catalog():
    items = [_ref(f"Glass-Plate-{i}", category="painting", ext="jpg") for i in range(4)]
    items += [
        _ref(f"glass-{i}", tags=["kind:icon", "topic:icon"], topics=["icon"])
        for i in range(4)
    ]
    items += [
        _ref(f"Glass-Regular-{i}", category="typography", ext="ttf") for i in range(3)
    ]
    return items


def test_role_quota_fills_each_role():
    refs = craft.select_references({"purpose": "유리 문진"}, _glass_catalog())
    roles = [ref["role"] for ref in refs]
    assert roles.count("illustration") == 3
    assert roles.count("icon") == 3
    assert roles.count("typography") == 2
    assert len(refs) == 8  # 최대 8건
    # 정렬은 (점수 desc, 이름) — 점수는 단조 감소
    assert [ref["score"] for ref in refs] == sorted(
        (ref["score"] for ref in refs), reverse=True
    )
    assert all({"role", "score", "matched", "reason"} <= set(ref) for ref in refs)


def test_quota_shortfall_returns_fewer_with_reason():
    items = [
        _ref(f"glass-{i}", tags=["kind:icon", "topic:icon"], topics=["icon"])
        for i in range(5)
    ]
    report = craft.scout_references({"purpose": "유리 문진"}, items)
    assert (
        len(report["references"]) == 3
    )  # 아이콘으로 illustration 슬롯을 메우지 않는다
    assert {ref["role"] for ref in report["references"]} == {"icon"}
    shortfall = {row["role"]: row for row in report["shortfall"]}
    assert set(shortfall) == {"illustration", "typography"}
    assert shortfall["illustration"]["got"] == 0
    assert "paperweight" in shortfall["illustration"]["reason"]


def test_select_references_filters_and_ranks():
    items = [
        _ref("floral-plate", category="painting", ext="jpg", topics=["floral"]),
        _ref(
            "floral-plate-output",
            category="painting",
            ext="jpg",
            topics=["floral"],
            scope="output",
        ),
        _ref(
            "blocked-floral",
            category="painting",
            ext="jpg",
            topics=["floral"],
            scope="blocked",
        ),
        _ref(
            "junk-floral", category="painting", ext="jpg", topics=["floral"], junk=True
        ),
    ]
    refs = craft.select_references({"purpose": "floral 카드"}, items, limit=5)
    names = [ref["name"] for ref in refs]
    assert names == ["floral-plate", "floral-plate-output"]
    assert refs[0]["score"] > refs[1]["score"]  # bundle > output
    assert refs[0]["matched"] == ["floral"]
    assert "topics" in refs[0]["reason"]


@needs_assets
def test_references_from_fixture_catalog():
    import json

    path = os.path.join(ASSETS, "test_data/golden/catalog.json")
    items = json.load(open(path))["items"]
    report = craft.scout_references(craft.parse_brief("plate 참고"), items)
    refs = report["references"]
    # 픽스처에는 plate 일러스트만 있다. 8건을 억지로 채우지 않는다.
    assert 0 < len(refs) < 8
    assert all(ref["role"] == "illustration" for ref in refs)
    assert all(ref["scope"] in ("bundle", "output") for ref in refs)
    assert any(row["role"] == "typography" for row in report["shortfall"])


def test_scout_dedupes_same_name_across_sets():
    items = [
        {"id": "a1", "name": "lighthouse", "kind": "icon", "license_scope": "bundle",
         "tags": ["kind:icon", "topic:lighthouse"]},
        {"id": "a2", "name": "lighthouse", "kind": "icon", "license_scope": "bundle",
         "tags": ["kind:icon", "topic:lighthouse"]},
        {"id": "b1", "name": "harbor", "kind": "icon", "license_scope": "bundle",
         "tags": ["kind:icon", "topic:lighthouse"]},
    ]
    refs = craft.scout_references({"purpose": "등대", "tones": []}, items)["references"]
    names = [r["name"] for r in refs]
    assert names.count("lighthouse") == 1
    assert all(r.get("id") for r in refs)


def test_lexicon_covers_illustration_names():
    terms = craft.extract_topic_terms({"purpose": "커피와 카메라가 있는 페이지"})
    assert "coffee" in terms and "camera" in terms
    terms = craft.extract_topic_terms({"purpose": "가방과 시계가 있는 캠핑 페이지"})
    assert {"bag", "watch", "camping"} <= set(terms)
    assert "gaming" in craft.extract_topic_terms({"purpose": "게임 화면"})


@needs_assets
def test_coffee_brief_finds_real_illustration():
    import json

    path = os.environ.get("SONAGI_CATALOG", os.path.join(ASSETS, "test_data/golden/catalog.json"))
    items = json.load(open(path))["items"]
    refs = craft.scout_references({"purpose": "커피 일러스트", "tones": []}, items)
    # 픽스처 카탈로그에는 coffee가 없으므로, 실제 카탈로그가 있을 때만 의미가 있다
    if not any(i.get("name") == "coffee" for i in items):
        pytest.skip("coffee 자산이 카탈로그에 없음(픽스처)")
    assert any(r["name"] == "coffee" for r in refs["references"])


def _split_ledger_decision(raw):
    """scripts/ledger.py의 parse_decision과 같은 규칙."""
    key, rest = raw.split("=", 1)
    choice, _, reason = rest.partition(":")
    return key.strip(), choice.strip(), reason.strip()


def test_scout_decision_keeps_role_counts_in_choice():
    # 실카탈로그 스카우트는 역할별로 채워진다. `role:count`로 적으면
    # ledger.py가 그 콜론에서 잘라 choice가 `4건 [illustration`이 된다.
    refs = [
        {"name": "camera", "role": "icon", "scope": "bundle"},
        {"name": "camera-ai", "role": "icon", "scope": "bundle"},
        {"name": "vintage-camera", "role": "illustration", "scope": "bundle"},
    ]
    report = {"filled": {"illustration": 1, "icon": 2, "typography": 0}}
    key, choice, reason = _split_ledger_decision(craft.scout_decision(refs, report))
    assert key == "scout"
    assert choice == "3건 [illustration=1,icon=2,typography=0]"
    assert reason == "camera, camera-ai, vintage-camera"


def test_scout_decision_without_references_says_why():
    key, choice, reason = _split_ledger_decision(craft.scout_decision([]))
    assert key == "scout"
    assert choice == "0건"
    assert "주제어" in reason
