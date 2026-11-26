"""DesignCritique 규칙 층 평가기 (GRAPH-48).

설계 원칙:
- subprocess 0 원칙: 외부 린터 바이너리(rg, eslint, node 등)를 호출하지 않고 파이썬 표준 라이브러리만 사용한다.
- 규칙 kind: attribute(기존 후보 dict 속성 검사) | source(신규: path glob + grep/ast/token).
  kind가 없는 기존 룰셋은 attribute로 하위 호환한다.
- 모든 규칙은 checkable_by(grep | ast | token | render | human)를 가진다.
- render·human 규칙은 실행하지 않고 not_verified 배열로 분리 보고한다("검사 못 했으면 발견 아님").
- exceptions(path glob + 면제 이유 + 근거)에 걸리면 발견을 만들지 않는다.
- 심각도 매핑(HIGH/MEDIUM/LOW → blocker/major/minor)은 severity_map.json 데이터에서 읽는다.
- JSX/TSX는 파이썬 표준 라이브러리로 정확히 파싱할 수 없으므로 grep 규칙으로 내리고 주석/메타에 남긴다.
- 점수 필드는 만들지 않는다 (GRAPH-47 계약 유지).
"""

from __future__ import annotations

import ast as py_ast
import json
import math
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VALID_RULE_KINDS = ("attribute", "source")
VALID_CHECKABLE_BY = ("grep", "ast", "token", "render", "human")
NON_EXECUTABLE_CHECKERS = ("render", "human")

_FAST_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".next",
    "dist",
    "out",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "playwright-report",
    "test-results",
}

_MAX_SOURCE_BYTES = 512 * 1024


def load_severity_map(rules_dir: Path) -> Dict[str, Any]:
    """severity_map.json을 로드한다. 파일이 없으면 빈 맵과 기본값만 반환한다."""
    path = rules_dir / "severity_map.json"
    if not path.is_file():
        return {"map": {}, "default": "major"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"map": {}, "default": "major"}


def resolve_severity(rule: Dict[str, Any], severity_map: Dict[str, Any]) -> str:
    """규칙의 심각도를 severity_map.json 데이터 기준으로 blocker | major | minor로 매핑한다."""
    mapping = severity_map.get("map") or {}
    default_sev = str(severity_map.get("default") or "major")
    raw = rule.get("jakub_severity") or rule.get("severity")
    if raw is None:
        return default_sev
    key = str(raw)
    if key in mapping:
        return str(mapping[key])
    if key.upper() in mapping:
        return str(mapping[key.upper()])
    if key.lower() in mapping:
        return str(mapping[key.lower()])
    if not mapping and key in ("blocker", "major", "minor"):
        return key
    return default_sev


def normalize_rule_kind(rule: Dict[str, Any]) -> str:
    """kind가 없는 기존 룰은 attribute로 하위 호환한다."""
    kind = rule.get("kind")
    return str(kind) if kind else "attribute"


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    norm = pattern.replace("\\", "/").lstrip("./")
    parts: List[str] = []
    i, n = 0, len(norm)
    while i < n:
        ch = norm[i]
        if ch == "*":
            if i + 1 < n and norm[i + 1] == "*":
                if i + 2 < n and norm[i + 2] == "/":
                    parts.append("(?:.*/)?")
                    i += 3
                    continue
                parts.append(".*")
                i += 2
                continue
            parts.append("[^/]*")
            i += 1
        elif ch == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


_GLOB_CACHE: Dict[str, re.Pattern[str]] = {}


def matches_glob(rel_path: str, pattern: str) -> bool:
    norm_path = rel_path.replace("\\", "/").lstrip("./")
    regex = _GLOB_CACHE.get(pattern)
    if regex is None:
        regex = _glob_to_regex(pattern)
        _GLOB_CACHE[pattern] = regex
    return bool(regex.match(norm_path))


def matches_any_glob(rel_path: str, patterns: Iterable[str]) -> bool:
    return any(matches_glob(rel_path, p) for p in patterns)


def find_exception_match(
    rel_path: str,
    rule: Dict[str, Any],
    global_excludes: List[str],
) -> Optional[Dict[str, str]]:
    """전역 exclude 또는 규칙의 exceptions(glob + reason + basis)에 걸리면 해당 항목을 반환한다."""
    for pat in global_excludes:
        if matches_glob(rel_path, pat):
            return {"glob": pat, "reason": "global_scope_exclude", "basis": "ruleset.scope.exclude"}
    for exc in rule.get("exceptions") or []:
        if not isinstance(exc, dict):
            continue
        pat = exc.get("glob") or exc.get("path_glob")
        if pat and matches_glob(rel_path, str(pat)):
            return {
                "glob": str(pat),
                "reason": str(exc.get("reason", "")),
                "basis": str(exc.get("basis", "")),
            }
    return None


def _eval_rule_op(actual: Any, op: str, expected: Any) -> bool:
    if op == "eq":
        return actual == expected
    if op == "in":
        return actual in (expected or [])
    if op == "gte":
        return isinstance(actual, (int, float)) and actual >= expected
    if op == "gt":
        return isinstance(actual, (int, float)) and actual > expected
    if op == "truthy":
        return bool(actual)
    return True


def _collect_source_files(candidate: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if isinstance(candidate.get("source_files"), dict):
        return {
            str(k).replace("\\", "/").lstrip("./"): str(v)
            for k, v in candidate["source_files"].items()
        }

    collected: Dict[str, str] = {}
    root_raw = candidate.get("source_dir") or candidate.get("repo_path")
    if root_raw:
        root = Path(str(root_raw))
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in _FAST_SKIP_DIRS]
                for fname in filenames:
                    fpath = Path(dirpath) / fname
                    try:
                        if fpath.stat().st_size > _MAX_SOURCE_BYTES:
                            continue
                        rel = fpath.relative_to(root).as_posix()
                        collected[rel] = fpath.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
            return collected

    if isinstance(candidate.get("paths"), list):
        for raw_p in candidate["paths"]:
            fpath = Path(str(raw_p))
            if fpath.is_file():
                try:
                    if fpath.stat().st_size <= _MAX_SOURCE_BYTES:
                        collected[fpath.as_posix()] = fpath.read_text(
                            encoding="utf-8", errors="ignore"
                        )
                except OSError:
                    continue
        return collected

    return None


_CSS_COLOR_ONLY_PROPS = frozenset(
    {
        "color",
        "background",
        "background-color",
        "border-color",
        "border-top-color",
        "border-right-color",
        "border-bottom-color",
        "border-left-color",
        "outline-color",
        "text-decoration-color",
        "caret-color",
        "fill",
        "stroke",
    }
)


def _is_color_only_css_hover(lines: List[str], match_idx: int, max_scan_lines: int = 40) -> bool:
    """CSS :hover 규칙 블록이 색상 관련 속성만 변경하면 True를 반환한다."""
    chunk = "\n".join(lines[match_idx : min(len(lines), match_idx + max_scan_lines)])
    brace_open = chunk.find("{")
    if brace_open == -1:
        return False
    depth = 0
    brace_close = -1
    for i in range(brace_open, len(chunk)):
        ch = chunk[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                brace_close = i
                break
    if brace_close == -1:
        return False
    body = chunk[brace_open + 1 : brace_close]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    props: List[str] = []
    for decl in body.split(";"):
        decl = decl.strip()
        if not decl or ":" not in decl:
            continue
        prop = decl.split(":", 1)[0].strip().lower()
        if prop:
            props.append(prop)
    return bool(props) and all(p in _CSS_COLOR_ONLY_PROPS for p in props)


def _evaluate_grep_rule(
    rule: Dict[str, Any],
    source_files: Dict[str, str],
    candidate_label: str,
    severity: str,
    global_includes: List[str],
    global_excludes: List[str],
) -> List[Dict[str, str]]:
    pattern_str = rule.get("pattern")
    if not pattern_str:
        return []
    try:
        pattern_re = re.compile(pattern_str)
    except re.error:
        return []

    negate_re = None
    if rule.get("negate"):
        try:
            negate_re = re.compile(str(rule["negate"]))
        except re.error:
            negate_re = None

    line_negate_re = None
    if rule.get("line_negate"):
        try:
            line_negate_re = re.compile(str(rule["line_negate"]))
        except re.error:
            line_negate_re = None

    context_lines = int(rule.get("context_lines") or 0)
    path_globs = rule.get("path_globs") or ["**/*"]
    findings: List[Dict[str, str]] = []
    rule_id = str(rule.get("id", "unknown"))
    fix_msg = str(rule.get("fix", f"{rule_id} 규칙을 충족하도록 수정하세요."))

    for rel_path, content in sorted(source_files.items()):
        if global_includes and not matches_any_glob(rel_path, global_includes):
            continue
        if not matches_any_glob(rel_path, path_globs):
            continue
        if find_exception_match(rel_path, rule, global_excludes) is not None:
            continue
        if negate_re is not None and negate_re.search(content):
            continue

        lines = content.splitlines()
        for idx, line in enumerate(lines):
            if not pattern_re.search(line):
                continue
            if line_negate_re is not None:
                start = max(0, idx - context_lines)
                end = min(len(lines), idx + context_lines + 1)
                window = "\n".join(lines[start:end])
                if line_negate_re.search(window):
                    continue
            if rule.get("ignore_color_only_css_hover") and _is_color_only_css_hover(lines, idx):
                continue
            findings.append(
                {
                    "rule_id": rule_id,
                    "severity": severity,
                    "location": f"{candidate_label}:{rel_path}:{idx + 1}",
                    "fix": fix_msg,
                }
            )
    return findings


class _MarkupScanner(HTMLParser):
    """HTML/SVG 구조 검사용 순수 파이썬 stdlib 파서 (subprocess 0)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: List[Tuple[str, Dict[str, Optional[str]], int]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        line, _col = self.getpos()
        self.tags.append((tag.lower(), {k.lower(): v for k, v in attrs}, line))


def _evaluate_ast_rule(
    rule: Dict[str, Any],
    source_files: Dict[str, str],
    candidate_label: str,
    severity: str,
    global_includes: List[str],
    global_excludes: List[str],
) -> List[Dict[str, str]]:
    """파이썬 stdlib(html.parser / ast)로 구조 규칙을 평가한다.

    주의: JSX/TSX는 파이썬 표준 라이브러리로 정확히 파싱할 수 없으므로
    해당 규칙은 grep으로 내려서 정의한다.
    """
    ast_check = str(rule.get("ast_check") or "")
    path_globs = rule.get("path_globs") or ["**/*.svg", "**/*.html"]
    rule_id = str(rule.get("id", "unknown"))
    fix_msg = str(rule.get("fix", f"{rule_id} 구조 규칙을 충족하도록 수정하세요."))
    findings: List[Dict[str, str]] = []

    for rel_path, content in sorted(source_files.items()):
        if global_includes and not matches_any_glob(rel_path, global_includes):
            continue
        if not matches_any_glob(rel_path, path_globs):
            continue
        if find_exception_match(rel_path, rule, global_excludes) is not None:
            continue

        if rel_path.endswith(".py") and ast_check == "no_subprocess":
            try:
                tree = py_ast.parse(content)
            except SyntaxError:
                continue
            for node in py_ast.walk(tree):
                if isinstance(node, py_ast.Import):
                    for alias in node.names:
                        if alias.name == "subprocess":
                            findings.append(
                                {
                                    "rule_id": rule_id,
                                    "severity": severity,
                                    "location": f"{candidate_label}:{rel_path}:{node.lineno}",
                                    "fix": fix_msg,
                                }
                            )
            continue

        scanner = _MarkupScanner()
        try:
            scanner.feed(content)
            scanner.close()
        except Exception:
            continue

        if ast_check == "svg_missing_viewbox":
            for tag, attrs, line in scanner.tags:
                if tag == "svg" and "viewbox" not in attrs:
                    findings.append(
                        {
                            "rule_id": rule_id,
                            "severity": severity,
                            "location": f"{candidate_label}:{rel_path}:{line}",
                            "fix": fix_msg,
                        }
                    )
        elif ast_check == "svg_hardcoded_hex":
            for tag, attrs, line in scanner.tags:
                if tag in ("svg", "path", "circle", "rect", "polygon", "line", "polyline"):
                    for attr_name in ("fill", "stroke"):
                        val = attrs.get(attr_name) or ""
                        if val.startswith("#"):
                            findings.append(
                                {
                                    "rule_id": rule_id,
                                    "severity": severity,
                                    "location": f"{candidate_label}:{rel_path}:{line}",
                                    "fix": fix_msg,
                                }
                            )
                            break
        elif ast_check == "svg_raster_embed":
            for tag, attrs, line in scanner.tags:
                if tag == "image":
                    href = attrs.get("href") or attrs.get("xlink:href") or ""
                    if href:
                        findings.append(
                            {
                                "rule_id": rule_id,
                                "severity": severity,
                                "location": f"{candidate_label}:{rel_path}:{line}",
                                "fix": fix_msg,
                            }
                        )
        elif ast_check == "html_img_missing_alt":
            for tag, attrs, line in scanner.tags:
                if tag == "img" and "alt" not in attrs:
                    findings.append(
                        {
                            "rule_id": rule_id,
                            "severity": severity,
                            "location": f"{candidate_label}:{rel_path}:{line}",
                            "fix": fix_msg,
                        }
                    )
        elif ast_check == "html_viewport_no_scale":
            for tag, attrs, line in scanner.tags:
                if tag == "meta" and (attrs.get("name") or "").lower() == "viewport":
                    cval = (attrs.get("content") or "").lower()
                    if "maximum-scale=1" in cval or "user-scalable=no" in cval:
                        findings.append(
                            {
                                "rule_id": rule_id,
                                "severity": severity,
                                "location": f"{candidate_label}:{rel_path}:{line}",
                                "fix": fix_msg,
                            }
                        )
    return findings


_CSS_VAR_RE = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:\s*([^;}{]+);")
_OKLCH_RE = re.compile(
    r"oklch\(\s*([0-9.]+%?)\s+([0-9.]+)\s+([0-9.]+)(?:deg)?(?:\s*/\s*([0-9.]+%?))?\s*\)"
)
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _hex_to_rgb01(hex_str: str) -> Optional[Tuple[float, float, float]]:
    m = _HEX_RE.match(hex_str.strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0


def _rel_luminance(rgb: Tuple[float, float, float]) -> float:
    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast_ratio(c1: Tuple[float, float, float], c2: Tuple[float, float, float]) -> float:
    l1, l2 = _rel_luminance(c1), _rel_luminance(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _hue_diff_deg(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 360.0
    return 360.0 - d if d > 180.0 else d


def _evaluate_token_rule(
    rule: Dict[str, Any],
    source_files: Dict[str, str],
    candidate_label: str,
    severity: str,
    global_includes: List[str],
    global_excludes: List[str],
) -> List[Dict[str, str]]:
    """CSS 커스텀 프로퍼티 및 JSON 토큰 파일을 파싱해 결정론적 계산 규칙을 평가한다."""
    token_check = str(rule.get("token_check") or "")
    path_globs = rule.get("path_globs") or ["**/*.css", "**/tokens/**/*.json"]
    rule_id = str(rule.get("id", "unknown"))
    fix_msg = str(rule.get("fix", f"{rule_id} 토큰 규칙을 충족하도록 수정하세요."))
    findings: List[Dict[str, str]] = []

    for rel_path, content in sorted(source_files.items()):
        if global_includes and not matches_any_glob(rel_path, global_includes):
            continue
        if not matches_any_glob(rel_path, path_globs):
            continue
        if find_exception_match(rel_path, rule, global_excludes) is not None:
            continue

        tokens: List[Tuple[str, str, int]] = []
        if rel_path.endswith(".css"):
            for idx, line in enumerate(content.splitlines(), start=1):
                for m in _CSS_VAR_RE.finditer(line):
                    tokens.append((m.group(1), m.group(2).strip(), idx))
        elif rel_path.endswith(".json"):
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                continue

            def _walk_json(obj: Any, prefix: str = "") -> None:
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        nxt = f"{prefix}.{k}" if prefix else str(k)
                        if isinstance(v, str):
                            tokens.append((nxt, v.strip(), 1))
                        else:
                            _walk_json(v, nxt)

            _walk_json(data)

        if not tokens:
            continue

        if token_check == "oklch_hue_collision":
            accents: List[Tuple[str, float, int]] = []
            statuses: List[Tuple[str, float, int]] = []
            for name, val, line in tokens:
                m = _OKLCH_RE.search(val)
                if not m:
                    continue
                hue = float(m.group(3))
                lname = name.lower()
                if any(k in lname for k in ("accent", "primary", "brand")):
                    accents.append((name, hue, line))
                elif any(k in lname for k in ("error", "danger", "warning", "success", "destructive")):
                    statuses.append((name, hue, line))
            for sname, shue, sline in statuses:
                for aname, ahue, _ in accents:
                    if _hue_diff_deg(shue, ahue) < 15.0:
                        findings.append(
                            {
                                "rule_id": rule_id,
                                "severity": severity,
                                "location": f"{candidate_label}:{rel_path}:{sline}",
                                "fix": f"{sname} hue({shue}°)가 {aname} hue({ahue}°)와 15° 이내로 겹칩니다. {fix_msg}",
                            }
                        )
                        break

        elif token_check == "pure_black_white":
            for name, val, line in tokens:
                lval = val.lower()
                if lval in ("#000", "#000000", "#fff", "#ffffff", "rgb(0, 0, 0)", "rgb(255, 255, 255)"):
                    findings.append(
                        {
                            "rule_id": rule_id,
                            "severity": severity,
                            "location": f"{candidate_label}:{rel_path}:{line}",
                            "fix": f"{name} ({val}): {fix_msg}",
                        }
                    )

        elif token_check == "opacity_text_token":
            for name, val, line in tokens:
                lname = name.lower()
                if not any(k in lname for k in ("text", "fg", "foreground")):
                    continue
                lval = val.lower()
                if "rgba(" in lval or ("oklch(" in lval and "/" in lval):
                    findings.append(
                        {
                            "rule_id": rule_id,
                            "severity": severity,
                            "location": f"{candidate_label}:{rel_path}:{line}",
                            "fix": f"{name} ({val}): {fix_msg}",
                        }
                    )

        elif token_check == "token_contrast_aa":
            hex_map: Dict[str, Tuple[Tuple[float, float, float], int]] = {}
            for name, val, line in tokens:
                rgb = _hex_to_rgb01(val)
                if rgb is not None:
                    hex_map[name.lower()] = (rgb, line)
            fg = (
                hex_map.get("--text")
                or hex_map.get("--foreground")
                or hex_map.get("text")
                or hex_map.get("foreground")
            )
            bg = (
                hex_map.get("--bg")
                or hex_map.get("--background")
                or hex_map.get("bg")
                or hex_map.get("background")
            )
            if fg and bg:
                ratio = _contrast_ratio(fg[0], bg[0])
                if math.isfinite(ratio) and ratio < 4.5:
                    findings.append(
                        {
                            "rule_id": rule_id,
                            "severity": severity,
                            "location": f"{candidate_label}:{rel_path}:{fg[1]}",
                            "fix": f"선언된 전경/배경 토큰 대비비 {ratio:.2f}:1 (< 4.5:1). {fix_msg}",
                        }
                    )

    return findings


def evaluate_ruleset_for_candidate(
    candidate: Dict[str, Any],
    candidate_label: str,
    ruleset: Dict[str, Any],
    rules_dir: Optional[Path] = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """단일 후보에 대해 attribute 및 source 규칙을 평가한다.

    반환: (findings, not_verified)
    - render/human 규칙은 실행하지 않고 not_verified에 분리한다.
    - 검사하지 못한 source 규칙(소스 입력 없음)도 findings에 넣지 않고 not_verified에 분리한다.
    """
    severity_map = load_severity_map(rules_dir) if rules_dir else {"map": {}, "default": "major"}
    scope = ruleset.get("scope") or {}
    global_includes: List[str] = list(scope.get("include") or [])
    global_excludes: List[str] = list(scope.get("exclude") or [])

    findings: List[Dict[str, str]] = []
    not_verified: List[Dict[str, str]] = []

    source_files: Optional[Dict[str, str]] = None
    source_checked = False

    for rule in ruleset.get("rules", []):
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("id") or rule.get("field") or "unknown")
        checkable_by = str(rule.get("checkable_by") or "token")
        severity = resolve_severity(rule, severity_map)
        kind = normalize_rule_kind(rule)

        if checkable_by in NON_EXECUTABLE_CHECKERS:
            not_verified.append(
                {
                    "rule_id": rule_id,
                    "checkable_by": checkable_by,
                    "severity": severity,
                    "location": f"{candidate_label}:{rule.get('location', rule_id)}",
                    "reason": "requires_render_or_human_verification",
                }
            )
            continue

        if kind == "attribute":
            field = rule.get("field")
            if not field:
                continue
            when_present = bool(rule.get("when_present", True))
            has_field = field in candidate and candidate.get(field) is not None
            if when_present and not has_field:
                continue

            actual = candidate.get(field)
            op = str(rule.get("op", "truthy"))
            expected = rule.get("value")
            if not _eval_rule_op(actual, op, expected):
                findings.append(
                    {
                        "severity": severity,
                        "location": f"{candidate_label}:{rule.get('location', field)}",
                        "fix": str(rule.get("fix", f"{field} 규칙을 충족하도록 수정하세요.")),
                    }
                )
            continue

        if kind == "source":
            if not source_checked:
                source_files = _collect_source_files(candidate)
                source_checked = True
            if source_files is None:
                not_verified.append(
                    {
                        "rule_id": rule_id,
                        "checkable_by": checkable_by,
                        "severity": severity,
                        "location": f"{candidate_label}:{rule.get('location', rule_id)}",
                        "reason": "no_source_input",
                    }
                )
                continue

            if checkable_by == "grep":
                findings.extend(
                    _evaluate_grep_rule(
                        rule,
                        source_files,
                        candidate_label,
                        severity,
                        global_includes,
                        global_excludes,
                    )
                )
            elif checkable_by == "ast":
                findings.extend(
                    _evaluate_ast_rule(
                        rule,
                        source_files,
                        candidate_label,
                        severity,
                        global_includes,
                        global_excludes,
                    )
                )
            elif checkable_by == "token":
                findings.extend(
                    _evaluate_token_rule(
                        rule,
                        source_files,
                        candidate_label,
                        severity,
                        global_includes,
                        global_excludes,
                    )
                )

    return findings, not_verified
