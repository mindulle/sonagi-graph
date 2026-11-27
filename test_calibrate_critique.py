"""Offline unit tests for scripts/calibrate_critique.py and fixtures/*.json."""

import copy
import unittest
from pathlib import Path

from scripts.calibrate_critique import (
    ASSET_PACK_AXES,
    COMMON_AXES,
    compute_metrics,
    expected_axes_for,
    load_fixtures,
    render_table,
    validate_fixture,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestCalibrateCritique(unittest.TestCase):
    def test_controlled_fixtures_pass_schema(self):
        fixtures, errors = load_fixtures(FIXTURES_DIR)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(fixtures), 8)

    def test_empty_gold_passes_validation_and_is_skipped(self):
        fixtures, _ = load_fixtures(FIXTURES_DIR)
        sample_ids = sorted(fixtures.keys())[:4]
        subset = {fid: copy.deepcopy(fixtures[fid]) for fid in sample_ids}

        unlabeled_id = "real_logo_01_draft"
        unlabeled = copy.deepcopy(fixtures[sample_ids[0]])
        unlabeled["id"] = unlabeled_id
        unlabeled["gold"] = {}
        self.assertEqual(validate_fixture(unlabeled, "memory"), [])
        subset[unlabeled_id] = unlabeled

        axes = expected_axes_for(subset[sample_ids[0]])
        results = {
            sample_ids[0]: {
                "id": sample_ids[0],
                "verdict": subset[sample_ids[0]]["gold"]["verdict"],
                "axes": [
                    {"axis_id": ax, "verdict": "A" if i == 0 else "TIE"}
                    for i, ax in enumerate(axes)
                ],
                "swap": {
                    "verdict": "B"
                    if subset[sample_ids[0]]["gold"]["verdict"] == "A"
                    else "A"
                },
            },
            sample_ids[1]: {
                "id": sample_ids[1],
                "verdict": subset[sample_ids[1]]["gold"]["verdict"],
                "axis_verdicts": {ax: "A" for ax in axes},
                "swap": {
                    "verdict": (
                        "B" if subset[sample_ids[1]]["gold"]["verdict"] == "A" else "A"
                    )
                },
            },
            sample_ids[2]: {
                "id": sample_ids[2],
                "verdict": "NEEDS_HUMAN",
                "axis_verdicts": {ax: "TIE" for ax in axes},
                "swap": {"reversed": True},
            },
            sample_ids[3]: {
                "id": sample_ids[3],
                "verdict": "TIE",
                "axis_verdicts": {ax: "TIE" for ax in axes},
                "swap": {"verdict": "TIE"},
            },
            unlabeled_id: {
                "id": unlabeled_id,
                "verdict": "A",
                "axis_verdicts": {ax: "A" for ax in axes},
                "swap": {"verdict": "B"},
            },
        }

        rows, evaluated, skipped = compute_metrics(subset, results)
        self.assertEqual(evaluated, 4)
        self.assertEqual(skipped, 1)
        by_name = {name: (rate, count) for name, rate, count, _ in rows}
        self.assertEqual(by_name["human_agreement"], ("50.0%", "2/4"))
        self.assertEqual(by_name["halo_rate"], ("25.0%", "1/4"))
        self.assertEqual(by_name["swap_reversal_rate"], ("25.0%", "1/4"))
        self.assertEqual(by_name["abstain_rate"], ("50.0%", "2/4"))
        table = render_table(rows)
        self.assertIn("human_agreement", table)
        self.assertIn("swap_reversal_rate", table)


if __name__ == "__main__":
    unittest.main()


class TestAssetTypeWidening(unittest.TestCase):
    """GRAPH-54: 실제 쌍은 인쇄물·랜딩일 수 있으므로 자산 유형을 넓혔다."""

    def _fixture(self, asset_type: str, versions: list[str], axes: list[str]) -> dict:
        cand = {
            "description": "관찰 가능한 설명",
            "colors": ["#112233"],
            "min_size_px": 320,
            "versions": versions,
            "concept_sentence": "개념 문장",
        }
        return {
            "id": "real_00",
            "brief": "브리프",
            "asset_type": asset_type,
            "candidate_a": cand,
            "candidate_b": dict(cand),
            "gold": {"verdict": "A", "decisive_axes": axes},
            "source_note": "출처",
        }

    def test_print_and_landing_pass_validation(self):
        self.assertEqual(
            validate_fixture(
                self._fixture("print", ["single_page", "full_bleed"], ["brief_fit"]),
                "print",
            ),
            [],
        )
        self.assertEqual(
            validate_fixture(
                self._fixture("landing", ["hero_section"], ["copy_hierarchy", "cta"]),
                "landing",
            ),
            [],
        )

    def test_unknown_asset_type_and_version_still_rejected(self):
        self.assertTrue(
            validate_fixture(
                self._fixture("poster", ["single_page"], ["brief_fit"]), "poster"
            )
        )
        self.assertTrue(
            validate_fixture(
                self._fixture("print", ["Single Page"], ["brief_fit"]), "versions"
            )
        )

    def test_landing_axes_are_expected_axes(self):
        fixture = self._fixture("landing", ["hero_section"], ["cta"])
        self.assertEqual(
            expected_axes_for(fixture),
            COMMON_AXES + ASSET_PACK_AXES["landing"],
        )
