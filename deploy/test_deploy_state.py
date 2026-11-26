#!/usr/bin/env python3
"""deploy_state.py의 digest 파싱과 상태 전이 테스트 (표준 라이브러리만)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy_state  # noqa: E402


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


class NormalizeDigestTest(unittest.TestCase):
    def test_plain_digest(self):
        self.assertEqual(deploy_state.normalize_digest(DIGEST_A), DIGEST_A)

    def test_repo_at_digest(self):
        value = f"ghcr.io/mindulle/sonagi-graph@{DIGEST_A}"
        self.assertEqual(deploy_state.normalize_digest(value), DIGEST_A)

    def test_whitespace_and_uppercase(self):
        value = "  " + DIGEST_A.upper() + "\n"
        self.assertEqual(deploy_state.normalize_digest(value), DIGEST_A)

    def test_rejects_short_or_missing(self):
        for value in ("", "sha256:abc", "latest", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    deploy_state.normalize_digest(value)


class PlanTest(unittest.TestCase):
    def test_first_deploy(self):
        self.assertEqual(deploy_state.plan({}, DIGEST_A), ("deploy", "첫 배포"))

    def test_same_digest_skips(self):
        state = {"digest": DIGEST_A}
        action, _ = deploy_state.plan(state, DIGEST_A)
        self.assertEqual(action, "skip")

    def test_changed_digest_deploys(self):
        state = {"digest": DIGEST_A}
        action, reason = deploy_state.plan(state, DIGEST_B)
        self.assertEqual(action, "deploy")
        self.assertIn(DIGEST_A, reason)
        self.assertIn(DIGEST_B, reason)

    def test_failed_digest_is_blocked(self):
        state = {"digest": DIGEST_A, "failed_digest": DIGEST_B}
        self.assertEqual(deploy_state.plan(state, DIGEST_B)[0], "blocked")

    def test_force_overrides_skip_and_block(self):
        state = {"digest": DIGEST_A, "failed_digest": DIGEST_A}
        self.assertEqual(deploy_state.plan(state, DIGEST_A, force=True)[0], "deploy")

    def test_new_digest_clears_block(self):
        state = {"digest": DIGEST_A, "failed_digest": DIGEST_B}
        self.assertEqual(deploy_state.plan(state, DIGEST_C)[0], "deploy")


class StateTransitionTest(unittest.TestCase):
    def test_first_success_has_no_previous(self):
        state = deploy_state.record_success({}, DIGEST_A, "img", "stable", at="t1")
        self.assertEqual(state["digest"], DIGEST_A)
        self.assertIsNone(state["previous_digest"])
        self.assertIsNone(state["failed_digest"])
        self.assertEqual(state["deployed_at"], "t1")

    def test_second_success_moves_current_to_previous(self):
        first = deploy_state.record_success({}, DIGEST_A, "img", "stable", at="t1")
        second = deploy_state.record_success(first, DIGEST_B, "img", "stable", at="t2")
        self.assertEqual(second["digest"], DIGEST_B)
        self.assertEqual(second["previous_digest"], DIGEST_A)
        self.assertEqual(second["previous_deployed_at"], "t1")

    def test_force_same_digest_keeps_previous(self):
        first = deploy_state.record_success({}, DIGEST_A, "img", "stable", at="t1")
        second = deploy_state.record_success(first, DIGEST_B, "img", "stable", at="t2")
        again = deploy_state.record_success(second, DIGEST_B, "img", "stable", at="t3")
        self.assertEqual(again["digest"], DIGEST_B)
        self.assertEqual(again["previous_digest"], DIGEST_A)

    def test_success_clears_previous_failure(self):
        failed = {
            "digest": DIGEST_A,
            "failed_digest": DIGEST_B,
            "failed_at": "t1",
            "last_error": "boom",
        }
        state = deploy_state.record_success(failed, DIGEST_C, "img", "stable", at="t2")
        self.assertIsNone(state["failed_digest"])
        self.assertIsNone(state["failed_at"])
        self.assertIsNone(state["last_error"])

    def test_failure_keeps_current_deployment(self):
        state = deploy_state.record_success({}, DIGEST_A, "img", "stable", at="t1")
        failed = deploy_state.record_failure(state, DIGEST_B, "health failed", at="t2")
        self.assertEqual(failed["digest"], DIGEST_A)
        self.assertEqual(failed["previous_digest"], None)
        self.assertEqual(failed["failed_digest"], DIGEST_B)
        self.assertEqual(failed["failed_at"], "t2")
        self.assertEqual(failed["last_error"], "health failed")

    def test_failure_truncates_error(self):
        state = deploy_state.record_failure({}, DIGEST_A, "x" * 1000, at="t1")
        self.assertEqual(len(state["last_error"]), 500)


class FileStateTest(unittest.TestCase):
    def test_missing_file_is_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(deploy_state.load_state(Path(tmp) / "missing.json"), {})

    def test_corrupt_file_is_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(deploy_state.load_state(path), {})

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = deploy_state.record_success({}, DIGEST_A, "img", "stable", at="t1")
            deploy_state.save_state(path, state)
            self.assertEqual(deploy_state.load_state(path), state)
            # 임시 파일이 남지 않는다.
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])
            json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
