"""Craft의 판단 로직 — LLM 없이 재현되는 부분만 모아 둔다.

`graphs/asset_craft_graph.py`는 이 함수들을 노드에서 호출한다. LangGraph에
의존하지 않으므로 테스트가 서비스 없이 돈다.

네 단계:

| 단계 | 함수 | 하는 일 |
| --- | --- | --- |
| 브리프 | `parse_brief` | 자연어 → Brief IR(톤·매체·금지사항) |
| 스카우트 | `scout_references` | 브리프 주제어 + 역할 쿼터 → 참고 자산 |
| 조합 | `plan_variants` | IR + 명부 → 스타일 후보 N개 |
| 판정 | `rank_candidates` | 린트 결과 → 통과/경고/실패 정렬 |

자연어를 파라미터로 옮기는 사전(`TONE_RULES`)은 **의도적으로 작다**. 여기서
욕심을 내면 프롬프트 엔지니어링으로 퇴화한다. 톤 5~6개, 축 이름 매칭이면
충분하고, 나머지는 사람이 고른다.

톤 사전은 스타일 후보(`plan_variants`)에만 쓴다. 레퍼런스 스카우트는 톤이
아니라 **주제 명사 + 역할**로 고른다(`TOPIC_LEXICON`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 톤 → 스타일 축 키워드. 값은 styles/axes.json의 이름 조각과 맞춘다.
TONE_RULES = {
    "고급": {
        "type": ["cormorant", "gowun", "archivo-airy"],
        "ornament": ["nouveau", "deco", "victorian", "arch"],
    },
    "따뜻": {"palette": ["terracotta", "sand", "floral", "bead", "lilac"]},
    "차분": {
        "palette": ["ocean", "forest", "mint", "charcoal"],
        "ornament": ["rule", "none"],
    },
    "대담": {"palette": ["brutalist", "poster", "deco"], "type": ["archivo-caps"]},
    "미니멀": {"palette": ["minimal", "swiss"], "ornament": ["none"]},
    "편집": {
        "palette": ["editorial", "newsprint", "paper", "magazine"],
        "layout": ["asymmetric", "list"],
    },
    "스위스": {
        "palette": ["swiss", "mint-swiss", "ocean-swiss"],
        "layout": ["columns"],
    },
}

MEDIA = {
    "web": "web",
    "랜딩": "web",
    "웹": "web",
    "인쇄": "print",
    "포스터": "print",
    "print": "print",
}


def _empty_ir(text=""):
    return {
        "purpose": text.strip(),
        "tones": [],
        "media": "web",
        "must_have": [],
        "must_not": [],
        "success": [],
    }


def parse_brief(brief):
    """문자열 또는 dict → Brief IR. dict는 빠진 칸만 채운다."""
    if isinstance(brief, dict):
        ir = _empty_ir(str(brief.get("purpose", "")))
        ir.update({k: v for k, v in brief.items() if k in ir and v is not None})
        ir["tones"] = list(ir.get("tones") or [])
        return ir

    text = str(brief or "").strip()
    ir = _empty_ir(text)
    ir["tones"] = [tone for tone in TONE_RULES if tone in text]
    for word, media in MEDIA.items():
        if word in text:
            ir["media"] = media
            break
    for match in re.finditer(r"(?:반드시|꼭)\s*([^,.]+)", text):
        ir["must_have"].append(match.group(1).strip())
    for match in re.finditer(r"(?:금지|쓰지\s*말|없이)\s*([^,.]+)", text):
        ir["must_not"].append(match.group(1).strip())
    if not ir["tones"]:
        ir["tones"] = ["편집"]
    return ir


def load_registry(assets_dir):
    path = Path(assets_dir) / "registry" / "styles.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"명부가 없습니다: {path} (build_registry.py를 먼저 실행)"
        )
    return json.loads(path.read_text())["styles"]


AXIS_WEIGHT = {"palette": 3, "type": 2, "layout": 2, "ornament": 1}
# 스타일을 직접 지목하는 말이 무드보다 강하다. "미니멀한 차분한"에서 미니멀이 이긴다.
TONE_WEIGHT = {
    "미니멀": 3,
    "스위스": 3,
    "고급": 2,
    "대담": 2,
    "편집": 2,
    "따뜻": 1,
    "차분": 1,
}


def score_style(style, tones, text=""):
    """톤 키워드가 스타일의 축 값과 얼마나 겹치는가. 이름이 브리프에 있으면 가산."""
    axes = style.get("axes", {})
    score = 0
    for tone in tones:
        tone_weight = TONE_WEIGHT.get(tone, 1)
        for axis, keywords in TONE_RULES.get(tone, {}).items():
            value = str(axes.get(axis, "")).lower()
            weight = tone_weight * AXIS_WEIGHT.get(axis, 1)
            score += sum(weight for keyword in keywords if keyword in value)
    name = style.get("name", "")
    if name and name.lower() in text.lower():
        score += 5
    return score


def plan_variants(brief, styles, limit=3, breakpoints=("desktop",), themes=("light",)):
    """IR과 명부로 렌더 후보를 만든다. 스타일마다 (스타일, 브레이크포인트, 테마) 조합."""
    text = brief.get("purpose", "")
    tones = brief.get("tones") or ["편집"]
    ranked = sorted(
        styles,
        key=lambda s: (-score_style(s, tones, text), s.get("name", "")),
    )
    chosen = [s for s in ranked if score_style(s, tones, text) > 0][:limit]
    if not chosen:  # 톤이 하나도 안 맞으면 이름순 상위로 폴백
        chosen = ranked[:limit]
    variants = []
    for style in chosen:
        for breakpoint in breakpoints:
            for theme in themes:
                variants.append(
                    {
                        "id": f"{style['name']}-{breakpoint}-{theme}",
                        "style": style["name"],
                        "style_id": style["id"],
                        "breakpoint": breakpoint,
                        "theme": theme,
                        "score": score_style(style, tones, text),
                    }
                )
    return variants


SEVERITY_WEIGHT = {"fail": 25, "warn": 5}


def rank_candidates(results):
    """린트 결과 목록을 점수순으로. FAIL은 후보에서 뺀다(버리지 않고 표시만)."""
    rows = []
    for row in results:
        findings = row.get("findings", [])
        penalty = sum(SEVERITY_WEIGHT.get(f.get("severity"), 0) for f in findings)
        rows.append(
            {
                "id": row.get("id"),
                "verdict": row.get("verdict", "pass"),
                "score": max(0, 100 - penalty),
                "plan_score": row.get("plan_score", 0),
                "rules": sorted({f["rule"] for f in findings}),
            }
        )
    # 점수가 같으면 브리프 적합도(plan_score)로, 그것도 같으면 이름으로.
    rows.sort(
        key=lambda r: (
            r["verdict"] == "fail",
            -r["score"],
            -r["plan_score"],
            r["id"] or "",
        )
    )
    return rows


def pick(results, limit=3):
    """선택지로 올릴 후보. FAIL은 제외하고, 없으면 빈 목록."""
    return [row for row in rank_candidates(results) if row["verdict"] != "fail"][:limit]


# --- 레퍼런스 스카우트 ----------------------------------------------------
#
# 레퍼런스는 "톤"이 아니라 "주제어 + 역할"로 고른다. 실카탈로그(약 1.5만 건)에는
# 미니멀·고급·따뜻 같은 톤 태그가 없고, 있다 해도 이름 부분 문자열로 걸리면
# `Archivo`(arch) · `border-none`(none) 같은 오매칭만 남는다.
#
#   1. 브리프에서 주제 명사를 뽑아 영문 주제어로 옮긴다(TOPIC_LEXICON).
#   2. 카탈로그 이름·topics·주제 태그를 **토큰 집합**으로 비교한다(부분 문자열 금지).
#   3. 역할(illustration/icon/typography) 쿼터를 채운다. 못 채우면 적게 돌려준다.
#
# 톤·매체 단어(인쇄물·랜딩·포스터)는 주제어가 아니다. 톤은 plan_variants에서만
# 쓰고, 매체는 render/compose가 쓴다.

REF_LIMIT = 8
REF_ROLES = ("illustration", "icon", "typography")
REF_ROLE_QUOTA = {"illustration": 3, "icon": 3, "typography": 2}
SCOPE_WEIGHT = {"bundle": 2, "output": 1}

# 점수에 쓰는 표면. 라이선스 태그(license_provider:line-seed 등)는 주제가 아니다.
MATCH_TAG_NAMESPACES = {"topic", "style", "theme", "medium", "dept", "shape"}
TERM_WEIGHT = {"topics": 6, "tags": 4, "name": 3}

# 4자 이하 낱말은 이름 안에서 우연히 걸리기 쉽다. 사전에 올린 주제어만 허용한다.
SHORT_TERM = 4

# 조판·UI 일반어. 시각 주제를 말해 주지 않으므로 주제어 후보에서 뺀다.
GENERIC_TERMS = frozenset(
    {
        "none",
        "rule",
        "arch",
        "deco",
        "line",
        "form",
        "text",
        "type",
        "grid",
        "list",
        "item",
        "note",
        "home",
        "menu",
        "user",
        "plus",
        "minus",
        "check",
        "close",
        "arrow",
        "cell",
        "block",
        "alert",
        "action",
        "edit",
        "file",
        "link",
        "lock",
        "play",
        "stop",
        "code",
        "data",
        "zoom",
        "crop",
        "move",
    }
)

# 브리프의 주제 명사 → 카탈로그 주제어. **명사만** 넣는다. 톤(미니멀·고급)과
# 매체(랜딩·인쇄물·포스터)는 넣지 않는다. 넣는 순간 톤 매칭이 이름만 바꿔 돌아온다.
TOPIC_LEXICON = {
    "민들레": ["dandelion"],
    "홀씨": ["seed"],
    "씨앗": ["seed"],
    "문진": ["paperweight"],
    "유리": ["glass"],
    "종이": ["paper"],
    "꽃": ["flower", "floral"],
    "식물": ["botanical"],
    "잎": ["leaf"],
    "나무": ["tree"],
    "산": ["mountain"],
    "바다": ["sea", "ocean"],
    "파도": ["wave"],
    "등대": ["lighthouse"],
    "물고기": ["fish"],
    "배": ["ship"],
    "새": ["bird"],
    "나비": ["butterfly"],
    "곤충": ["insect"],
    "고양이": ["cat"],
    "개": ["dog"],
    "사람": ["people"],
    "인물": ["figure", "portrait"],
    "풍경": ["landscape"],
    "과일": ["fruit"],
    "채소": ["vegetable"],
    "가지": ["eggplant"],
    "백합": ["lily"],
    "지도": ["map"],
    "돌": ["stone"],
    "질감": ["texture"],
    "추상": ["abstract"],
    "색": ["color"],
    "로고": ["logo"],
    "연필": ["pencil"],
    "별": ["star"],
    "달": ["moon"],
}
LEXICON_TERMS = frozenset(term for terms in TOPIC_LEXICON.values() for term in terms)

# "민들레가/홀씨를"처럼 조사가 붙은 토큰을 받아 준다. 어간이 명사 안에서
# 우연히 걸리는 것(가지고 → 가지)은 조사 화이트리스트로 막는다.
PARTICLES = frozenset(
    {
        "이",
        "가",
        "을",
        "를",
        "의",
        "에",
        "와",
        "과",
        "도",
        "은",
        "는",
        "로",
        "으로",
        "만",
        "에서",
        "부터",
        "까지",
        "보다",
        "처럼",
        "이나",
        "나",
        "랑",
        "이랑",
        "에는",
        "에도",
        "이며",
        "으로서",
        "하고",
    }
)

FONT_EXTS = {"ttf", "otf", "woff", "woff2"}
IMAGE_EXTS = {"jpg", "jpeg", "png"}
ILLUSTRATION_CATEGORIES = {
    "painting",
    "drawing",
    "print",
    "lace",
    "ceramic",
    "miscellaneous",
}


def _tokens(value):
    return set(re.findall(r"[a-z0-9]+", str(value).lower()))


def is_topic_term(term):
    """주제어로 쓸 수 있는 낱말인가. 일반어·짧은 미등록어는 버린다."""
    term = str(term or "").lower()
    if len(term) < 2 or term in GENERIC_TERMS:
        return False
    if len(term) <= SHORT_TERM and term not in LEXICON_TERMS:
        return False
    return True


def _korean_hit(token, key):
    if token == key:
        return True
    if len(key) < 2:  # 한 글자 명사는 토큰 전체가 같을 때만
        return False
    return token.startswith(key) and token[len(key) :] in PARTICLES


def extract_topic_terms(brief):
    """브리프에서 주제 명사를 뽑아 영문 주제어 목록으로. 톤 단어는 쓰지 않는다."""
    text = str((brief or {}).get("purpose") or "")
    terms = []
    for token in re.split(r"[^0-9A-Za-z가-힣]+", text):
        if not token:
            continue
        for korean, mapped in TOPIC_LEXICON.items():
            if _korean_hit(token, korean):
                terms.extend(mapped)
    # 브리프에 이미 영어 명사가 있으면 그대로 주제어로 인정한다(floral card 등).
    terms.extend(re.findall(r"[a-z][a-z0-9]+", text.lower()))
    seen = set()
    picked = []
    for term in terms:
        if term in seen or not is_topic_term(term):
            continue
        seen.add(term)
        picked.append(term)
    return picked


def reference_role(item):
    """카탈로그 항목의 역할. 쿼터 슬롯은 이 셋뿐이다."""
    tags = {str(tag) for tag in item.get("tags") or []}
    kinds = {tag.split(":", 1)[1] for tag in tags if tag.startswith("kind:")}
    category = str(item.get("category") or "")
    ext = str(item.get("ext") or "").lower()
    if category == "typography" or "specimen" in kinds:
        return "typography"
    if kinds & {"illustration", "logo"}:
        return "illustration"
    if kinds & {"icon", "glyph"}:
        return "icon"
    if category in ILLUSTRATION_CATEGORIES:
        return "illustration"
    if ext in FONT_EXTS:
        return "typography"
    if ext in IMAGE_EXTS:
        return "illustration"
    return None


def _item_surfaces(item):
    name = _tokens(item.get("name"))
    topics = set()
    for topic in item.get("topics") or []:
        topics |= _tokens(topic)
    tags = set()
    for tag in item.get("tags") or []:
        namespace, _, value = str(tag).partition(":")
        if namespace in MATCH_TAG_NAMESPACES:
            tags |= _tokens(value)
    return name, topics, tags


def _match_item(terms, item):
    """토큰 집합 비교. `arch`는 `Archivo`에 걸리지 않는다."""
    name, topics, tags = _item_surfaces(item)
    matched = []
    sources = {}
    score = 0
    for term in terms:
        where = []
        if term in topics:
            where.append("topics")
        if term in tags:
            where.append("tags")
        if term in name:
            where.append("name")
        if not where:
            continue
        matched.append(term)
        sources[term] = where
        score += max(TERM_WEIGHT[source] for source in where)
    return matched, sources, score


def _quota_for(limit):
    """limit에 맞춰 역할 쿼터를 비율 배분한다(최대 나머지). 8이면 3/3/2."""
    base_total = sum(REF_ROLE_QUOTA.values())
    if limit >= base_total:
        return dict(REF_ROLE_QUOTA)
    shares = [(role, limit * REF_ROLE_QUOTA[role] / base_total) for role in REF_ROLES]
    quota = {role: min(int(share), REF_ROLE_QUOTA[role]) for role, share in shares}
    left = limit - sum(quota.values())
    for role, share in sorted(
        shares, key=lambda s: (-(s[1] - int(s[1])), REF_ROLES.index(s[0]))
    ):
        if left <= 0:
            break
        if quota[role] < REF_ROLE_QUOTA[role]:
            quota[role] += 1
            left -= 1
    while left > 0:  # 남으면 순서대로 한 바퀴 더
        progressed = False
        for role in REF_ROLES:
            if left > 0 and quota[role] < REF_ROLE_QUOTA[role]:
                quota[role] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return quota


def scout_references(brief, items, limit=REF_LIMIT, quota=None):
    """브리프 주제어로 참고 자산을 고르고, 역할 쿼터를 채운다.

    `bundle`(재배포 가능)·`output`(결과물에만)만 후보. `blocked`·junk는 뺀다.
    쿼터를 못 채우면 다른 역할로 억지로 메우지 않고 `shortfall`에 이유를 남긴다.
    """
    limit = max(0, min(int(limit if limit is not None else REF_LIMIT), REF_LIMIT))
    ir = brief if isinstance(brief, dict) else {"purpose": str(brief or "")}
    terms = extract_topic_terms(ir)
    want = dict(quota or _quota_for(limit))

    rows = []
    term_hits = {term: 0 for term in terms}
    for item in items:
        if item.get("junk"):
            continue
        scope = item.get("license_scope") or "output"
        if scope not in SCOPE_WEIGHT:  # blocked 제외
            continue
        role = reference_role(item)
        if role is None:
            continue
        matched, sources, score = _match_item(terms, item)
        if not matched:
            continue
        for term in matched:
            term_hits[term] += 1
        score += SCOPE_WEIGHT[scope]
        reason = " · ".join(f"{term}∈{'+'.join(sources[term])}" for term in matched)
        rows.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "kind": item.get("kind"),
                "role": role,
                "license": item.get("license"),
                "scope": scope,
                "score": score,
                "matched": matched,
                "reason": f"{reason} · scope={scope}",
            }
        )

    # 같은 이름의 자산이 여러 세트에 있을 수 있다. 슬롯을 중복으로 쓰지 않는다.
    rows.sort(key=lambda r: (-r["score"], r["name"] or ""))
    by_role = {role: [] for role in REF_ROLES}
    seen = set()
    for row in rows:
        key = (row["role"], row["name"])
        if key in seen:
            continue
        seen.add(key)
        by_role[row["role"]].append(row)

    picked = []
    filled = {}
    for role in REF_ROLES:
        ranked = sorted(by_role[role], key=lambda r: (-r["score"], r["name"] or ""))
        taken = ranked[: want.get(role, 0)]
        filled[role] = len(taken)
        picked.extend(taken)
    picked.sort(key=lambda r: (-r["score"], r["name"] or ""))
    picked = picked[:limit]

    missing = [term for term, hits in term_hits.items() if hits == 0]
    shortfall = []
    for role in REF_ROLES:
        wanted = want.get(role, 0)
        got = filled.get(role, 0)
        if got >= wanted:
            continue
        if not terms:
            reason = "주제어 없음 — 톤 단어는 참조 매칭에 쓰지 않는다"
        elif not by_role[role]:
            hits = [term for term in terms if term_hits[term] > 0]
            if hits:
                reason = (
                    f"{role} 후보 0건 — 일치 주제어 {', '.join(hits)}가 이 역할에 없다"
                )
            else:
                reason = f"{role} 후보 0건 — 주제어 {', '.join(terms)} 자산 없음"
            if hits and missing:
                reason += f" (미일치: {', '.join(missing)})"
        else:
            reason = f"{role} 후보 {len(by_role[role])}건 < 슬롯 {wanted}"
        shortfall.append({"role": role, "wanted": wanted, "got": got, "reason": reason})

    return {
        "references": picked,
        "quota": want,
        "filled": filled,
        "shortfall": shortfall,
        "terms": terms,
        "term_hits": term_hits,
    }


def select_references(brief, items, limit=REF_LIMIT, quota=None):
    """`scout_references`의 references만. 기존 호출부·테스트 호환용."""
    return scout_references(brief, items, limit=limit, quota=quota)["references"]
