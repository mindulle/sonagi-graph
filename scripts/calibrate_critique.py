#!/usr/bin/env python3
"""Calibration metrics runner for the design_critique graph (GRAPH-53).

Reads fixtures/*.json and graph evaluation result JSON(s), validates fixture
structure without external dependencies, and prints a summary table with:
  - human_agreement
  - halo_rate
  - swap_reversal_rate
  - abstain_rate
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

COMMON_AXES = (
    "brief_fit",
    "concept_logic",
    "form_craft",
    "distinctiveness",
)

ASSET_PACK_AXES: dict[str, tuple[str, ...]] = {
    "logo": ("logotype", "small_scale_legibility", "system_extension"),
}

ALL_LOGO_AXES = set(COMMON_AXES + ASSET_PACK_AXES["logo"])
VALID_VERDICTS = {"A", "B", "TIE", "BOTH_REJECT", "NEEDS_HUMAN"}
ABSTAIN_VERDICTS = {"NEEDS_HUMAN", "TIE"}
DIRECTIONAL_VERDICTS = {"A", "B"}
VALID_VERSIONS = {"horizontal", "stacked", "symbol_only", "monochrome", "inverse", "favicon"}
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
FORBIDDEN_SCORE_KEYS = {"score", "scores", "rating", "ratings", "points", "total_score"}

FLIP_MAP = {
    "A": "B",
    "B": "A",
    "TIE": "TIE",
    "BOTH_REJECT": "BOTH_REJECT",
    "NEEDS_HUMAN": "NEEDS_HUMAN",
}


def validate_fixture(data: dict[str, Any], source: str = "<memory>") -> list[str]:
    """Validate a single fixture dict against the GRAPH-53 schema rules."""
    errors: list[str] = []
    required_top = {"id", "brief", "asset_type", "candidate_a", "candidate_b", "gold", "source_note"}
    missing = required_top - set(data.keys())
    if missing:
        errors.append(f"{source}: missing required fields {sorted(missing)}")
        return errors

    extra = set(data.keys()) - required_top
    if extra:
        errors.append(f"{source}: unexpected top-level fields {sorted(extra)}")

    fid = data.get("id")
    if not isinstance(fid, str) or not ID_RE.match(fid):
        errors.append(f"{source}: invalid id {fid!r}")

    if not isinstance(data.get("brief"), str) or not data["brief"].strip():
        errors.append(f"{source}: brief must be a non-empty string")

    if data.get("asset_type") not in ASSET_PACK_AXES:
        errors.append(f"{source}: unsupported asset_type {data.get('asset_type')!r}")

    if not isinstance(data.get("source_note"), str) or not data["source_note"].strip():
        errors.append(f"{source}: source_note must be a non-empty string")

    for side in ("candidate_a", "candidate_b"):
        cand = data.get(side)
        if not isinstance(cand, dict):
            errors.append(f"{source}.{side}: must be an object")
            continue
        req_cand = {"description", "colors", "min_size_px", "versions", "concept_sentence"}
        c_missing = req_cand - set(cand.keys())
        if c_missing:
            errors.append(f"{source}.{side}: missing fields {sorted(c_missing)}")
        for bad_key in set(cand.keys()) & FORBIDDEN_SCORE_KEYS:
            errors.append(f"{source}.{side}: forbidden score field {bad_key!r}")
        if not isinstance(cand.get("description"), str) or not cand["description"].strip():
            errors.append(f"{source}.{side}.description: must be non-empty string")
        if not isinstance(cand.get("concept_sentence"), str) or not cand["concept_sentence"].strip():
            errors.append(f"{source}.{side}.concept_sentence: must be non-empty string")
        colors = cand.get("colors")
        if not isinstance(colors, list) or not colors or not all(
            isinstance(c, str) and HEX_COLOR_RE.match(c) for c in colors
        ):
            errors.append(f"{source}.{side}.colors: must be non-empty list of #RRGGBB strings")
        min_px = cand.get("min_size_px")
        if isinstance(min_px, bool) or not isinstance(min_px, int) or min_px < 1:
            errors.append(f"{source}.{side}.min_size_px: must be positive integer")
        versions = cand.get("versions")
        if (
            not isinstance(versions, list)
            or not versions
            or len(set(versions)) != len(versions)
            or not all(isinstance(v, str) and v in VALID_VERSIONS for v in versions)
        ):
            errors.append(f"{source}.{side}.versions: invalid version list")

    gold = data.get("gold")
    if not isinstance(gold, dict):
        errors.append(f"{source}.gold: must be an object")
    else:
        if gold.get("verdict") not in VALID_VERDICTS:
            errors.append(f"{source}.gold.verdict: invalid verdict {gold.get('verdict')!r}")
        axes = gold.get("decisive_axes")
        if (
            not isinstance(axes, list)
            or not axes
            or len(set(axes)) != len(axes)
            or not all(isinstance(ax, str) and ax in ALL_LOGO_AXES for ax in axes)
        ):
            errors.append(f"{source}.gold.decisive_axes: invalid axis list")

    return errors


def load_fixtures(fixtures_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load and validate all calibration fixtures from fixtures/*.json."""
    if not fixtures_dir.is_dir():
        raise FileNotFoundError(f"Fixtures directory not found: {fixtures_dir}")

    fixtures: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for path in sorted(fixtures_dir.glob("*.json")):
        if path.name == "schema.json" or path.name.endswith(".schema.json"):
            continue
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            errors.append(f"{path}: root must be a JSON object")
            continue
        errors.extend(validate_fixture(data, str(path)))
        fid = str(data.get("id", path.stem))
        if fid in fixtures:
            errors.append(f"{path}: duplicate fixture id {fid!r}")
        fixtures[fid] = data

    return fixtures, errors


def _extract_result_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if "results" in payload and isinstance(payload["results"], list):
            return [item for item in payload["results"] if isinstance(item, dict)]
        if "id" in payload or "fixture_id" in payload:
            return [payload]
    return []


def load_results(results_path: Path) -> dict[str, dict[str, Any]]:
    """Load graph output JSON(s) from a file or directory."""
    if not results_path.exists():
        raise FileNotFoundError(f"Results path not found: {results_path}")

    files = sorted(results_path.glob("*.json")) if results_path.is_dir() else [results_path]
    results: dict[str, dict[str, Any]] = {}

    for path in files:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for record in _extract_result_records(payload):
            fid = record.get("fixture_id") or record.get("id")
            if not fid:
                continue
            # Unwrap `output` if the record is a full graph state dump.
            if "output" in record and isinstance(record["output"], dict) and "verdict" not in record:
                merged = dict(record["output"])
                merged["id"] = fid
                results[str(fid)] = merged
            else:
                results[str(fid)] = record
    return results


def expected_axes_for(fixture: dict[str, Any]) -> tuple[str, ...]:
    asset_type = str(fixture.get("asset_type", "logo"))
    return COMMON_AXES + ASSET_PACK_AXES.get(asset_type, ())


def extract_axis_verdicts(result: dict[str, Any]) -> dict[str, str]:
    """Normalize axis verdict mapping from dict or design_critique_graph list."""
    raw = result.get("axis_verdicts") or result.get("axes") or result.get("axis_results")
    if isinstance(raw, dict):
        normalized: dict[str, str] = {}
        for axis, val in raw.items():
            verdict = val.get("verdict") if isinstance(val, dict) else val
            if isinstance(verdict, str):
                normalized[str(axis)] = verdict.upper()
        return normalized
    if isinstance(raw, list):
        normalized = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            axis_id = item.get("axis_id") or item.get("id")
            verdict = item.get("verdict") or item.get("forward_winner")
            if isinstance(axis_id, str) and isinstance(verdict, str):
                normalized[axis_id] = verdict.upper()
        return normalized
    return {}


def is_halo_pair(result: dict[str, Any], axes: tuple[str, ...]) -> bool | None:
    """Return True if all expected axes point to the same winner (all A or all B)."""
    axis_verdicts = extract_axis_verdicts(result)
    if not axis_verdicts:
        return None
    values = [axis_verdicts.get(ax) for ax in axes]
    if any(v is None for v in values):
        return None
    unique_vals = set(values)
    return len(unique_vals) == 1 and next(iter(unique_vals)) in DIRECTIONAL_VERDICTS


def is_swap_reversed(result: dict[str, Any]) -> bool | None:
    """Check whether swapping A<->B broke order consistency."""
    if "swap" in result and result["swap"] is not None:
        swap_val = result["swap"]
        if isinstance(swap_val, bool):
            return swap_val
        verdict = str(result.get("forward_verdict") or result.get("verdict") or "").upper()
        if isinstance(swap_val, str):
            swap_verdict = swap_val.upper()
            if verdict in FLIP_MAP and swap_verdict in VALID_VERDICTS:
                return swap_verdict != FLIP_MAP[verdict]
            return None
        if isinstance(swap_val, dict):
            if "reversed" in swap_val and isinstance(swap_val["reversed"], bool):
                return swap_val["reversed"]
            swap_verdict = str(swap_val.get("verdict", "")).upper()
            if verdict in FLIP_MAP and swap_verdict in VALID_VERDICTS:
                return swap_verdict != FLIP_MAP[verdict]
        return None

    # Fallback: native design_critique_graph output fields
    if "swap_consistent" in result and isinstance(result["swap_consistent"], bool):
        return not result["swap_consistent"]

    return None


def compute_metrics(
    fixtures: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, str, str, str]], int]:
    """Compute the 4 calibration metrics and return formatted table rows."""
    matched_ids = [fid for fid in fixtures if fid in results]
    total = len(matched_ids)

    agree_num = 0
    halo_num = 0
    halo_den = 0
    swap_num = 0
    swap_den = 0
    abstain_num = 0

    for fid in matched_ids:
        fixture = fixtures[fid]
        result = results[fid]

        gold_verdict = str(fixture["gold"]["verdict"]).upper()
        pred_verdict = str(result.get("verdict", "")).upper()

        if pred_verdict == gold_verdict:
            agree_num += 1

        if pred_verdict in ABSTAIN_VERDICTS:
            abstain_num += 1

        halo = is_halo_pair(result, expected_axes_for(fixture))
        if halo is not None:
            halo_den += 1
            if halo:
                halo_num += 1

        reversed_flag = is_swap_reversed(result)
        if reversed_flag is not None:
            swap_den += 1
            if reversed_flag:
                swap_num += 1

    def fmt_rate(num: int, den: int) -> tuple[str, str]:
        if den == 0:
            return "N/A", "0/0"
        return f"{(num / den) * 100:.1f}%", f"{num}/{den}"

    agree_pct, agree_frac = fmt_rate(agree_num, total)
    halo_pct, halo_frac = fmt_rate(halo_num, halo_den)
    swap_pct, swap_frac = fmt_rate(swap_num, swap_den)
    abstain_pct, abstain_frac = fmt_rate(abstain_num, total)

    rows = [
        ("human_agreement", agree_pct, agree_frac, "gold.verdict == graph.verdict"),
        ("halo_rate", halo_pct, halo_frac, "all 7 axes identical (A or B)"),
        ("swap_reversal_rate", swap_pct, swap_frac, "inconsistent flip under A<->B swap"),
        ("abstain_rate", abstain_pct, abstain_frac, "NEEDS_HUMAN or TIE"),
    ]
    return rows, total


def render_table(rows: list[tuple[str, str, str, str]]) -> str:
    headers = ("Metric", "Rate", "Count", "Definition")
    all_rows = [headers, *rows]
    widths = [max(len(row[i]) for row in all_rows) for i in range(4)]

    def fmt_row(row: tuple[str, str, str, str]) -> str:
        return (
            f"| {row[0].ljust(widths[0])} "
            f"| {row[1].rjust(widths[1])} "
            f"| {row[2].rjust(widths[2])} "
            f"| {row[3].ljust(widths[3])} |"
        )

    border = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    lines = [border, fmt_row(headers), border]
    lines.extend(fmt_row(r) for r in rows)
    lines.append(border)
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate calibration fixtures and compute design_critique metrics."
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("fixtures"),
        help="Directory containing fixture JSON files (default: fixtures)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="Path to graph result JSON file or directory of result JSON files",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate fixtures/*.json against the schema and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    fixtures, errors = load_fixtures(args.fixtures)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    if not fixtures:
        print(f"No fixtures found in {args.fixtures}", file=sys.stderr)
        return 1

    if args.validate_only:
        print(f"Validated {len(fixtures)} fixtures in {args.fixtures} (0 errors).")
        return 0

    if args.results is None:
        print("--results is required unless --validate-only is set.", file=sys.stderr)
        return 2

    results = load_results(args.results)
    rows, matched = compute_metrics(fixtures, results)
    if matched == 0:
        print(
            f"No matching fixture IDs between {args.fixtures} and {args.results}",
            file=sys.stderr,
        )
        return 1

    print(f"Fixtures loaded: {len(fixtures)} | Matched results: {matched}")
    print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
