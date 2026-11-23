"""Craft의 판단 로직 — LLM 없이 재현되는 부분만 모아 둔다.

`graphs/asset_craft_graph.py`는 이 함수들을 노드에서 호출한다. LangGraph에
의존하지 않으므로 테스트가 서비스 없이 돈다.

세 단계:

| 단계 | 함수 | 하는 일 |
| --- | --- | --- |
| 브리프 | `parse_brief` | 자연어 → Brief IR(톤·매체·금지사항) |
| 조합 | `plan_variants` | IR + 명부 → 스타일 후보 N개 |
| 판정 | `rank_candidates` | 린트 결과 → 통과/경고/실패 정렬 |

자연어를 파라미터로 옮기는 사전(`TONE_RULES`)은 **의도적으로 작다**. 여기서
욕심을 내면 프롬프트 엔지니어링으로 퇴화한다. 톤 5~6개, 축 이름 매칭이면
충분하고, 나머지는 사람이 고른다.
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
TONE_WEIGHT = {"미니멀": 3, "스위스": 3, "고급": 2, "대담": 2, "편집": 2, "따뜻": 1, "차분": 1}


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
    rows.sort(key=lambda r: (r["verdict"] == "fail", -r["score"], -r["plan_score"], r["id"] or ""))
    return rows


def pick(results, limit=3):
    """선택지로 올릴 후보. FAIL은 제외하고, 없으면 빈 목록."""
    return [row for row in rank_candidates(results) if row["verdict"] != "fail"][:limit]
