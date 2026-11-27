"""Offline unit tests for scripts/calibrate_critique.py and fixtures/*.json."""

import copy
import unittest
from pathlib import Path

from scripts.calibrate_critique import (
    compute_metrics,
    expected_axes_for,
    load_fixtures,
    render_table,
    validate_fixture,
)


class TestCalibrateCritique(unittest.TestCase):
    def test_controlled_fixtures_pass_schema(self):
        fixtures, errors = load_fixtures(Path("fixtures"))
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(fixtures), 8)

    def test_empty_gold_passes_validation_and_is_skipped(self):
        fixtures, _ = load_fixtures(Path("fixtures"))
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
                "swap": {"verdict": "B" if subset[sample_ids[0]]["gold"]["verdict"] == "A" else "A"},
            },
            sample_ids[1]: {
                "id": sample_ids[1],
                "verdict": "A",
                "axis_verdicts": {ax: "A" for ax in axes},
                "swap": {"verdict": "B"},
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
