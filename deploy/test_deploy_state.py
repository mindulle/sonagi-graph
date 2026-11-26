#!/usr/bin/env python3
"""deploy_state.py의 릴리스 태그 파싱과 상태 전이 테스트 (표준 라이브러리만)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy_state  # noqa: E402


TAG_A = "image-aaaaaaaaaaaa"
TAG_B = "image-bbbbbbbbbbbb"
TAG_C = "image-cccccccccccc"
REF_A = "ghcr.io/mindulle/sonagi-graph:sha-aaaaaaaaaaaa"
REF_B = "ghcr.io/mindulle/sonagi-graph:sha-bbbbbbbbbbbb"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


class NormalizeTagTest(unittest.TestCase):
    def test_plain_tag(self):
        self.assertEqual(deploy_state.normalize_tag(TAG_A), TAG_A)

    def test_uppercase_and_whitespace(self):
        self.assertEqual(deploy_state.normalize_tag(" " + TAG_A.upper() + "\n"), TAG_A)

    def test_short_and_long_sha(self):
        self.assertEqual(
            deploy_state.normalize_tag("image-" + "a" * 7), "image-" + "a" * 7
        )
        self.assertEqual(
            deploy_state.normalize_tag("image-" + "a" * 40), "image-" + "a" * 40
        )

    def test_rejects_bad_tags(self):
        for value in (
            "",
            "image-",
            "image-abc",
            "image-zzzzzzzz",
            "latest",
            "v1.2.3",
            None,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    deploy_state.normalize_tag(value)


class PlanTest(unittest.TestCase):
    def test_first_deploy(self):
        self.assertEqual(deploy_state.plan({}, TAG_A), ("deploy", "첫 배포"))

    def test_same_tag_skips(self):
        self.assertEqual(deploy_state.plan({"tag": TAG_A}, TAG_A)[0], "skip")

    def test_changed_tag_deploys(self):
        action, reason = deploy_state.plan({"tag": TAG_A}, TAG_B)
        self.assertEqual(action, "deploy")
        self.assertIn(TAG_A, reason)
        self.assertIn(TAG_B, reason)

    def test_failed_tag_is_blocked(self):
        state = {"tag": TAG_A, "failed_tag": TAG_B}
        self.assertEqual(deploy_state.plan(state, TAG_B)[0], "blocked")

    def test_force_overrides_skip_and_block(self):
        state = {"tag": TAG_A, "failed_tag": TAG_A}
        self.assertEqual(deploy_state.plan(state, TAG_A, force=True)[0], "deploy")

    def test_new_tag_clears_block(self):
        state = {"tag": TAG_A, "failed_tag": TAG_B}
        self.assertEqual(deploy_state.plan(state, TAG_C)[0], "deploy")


class StateTransitionTest(unittest.TestCase):
    def test_first_success_has_no_previous(self):
        state = deploy_state.record_success({}, TAG_A, REF_A, DIGEST_A, at="t1")
        self.assertEqual(state["tag"], TAG_A)
        self.assertEqual(state["image_ref"], REF_A)
        self.assertEqual(state["digest"], DIGEST_A)
        self.assertIsNone(state["previous_tag"])
        self.assertIsNone(state["failed_tag"])
        self.assertEqual(state["history"], [TAG_A])
        self.assertEqual(state["deployed_at"], "t1")

    def test_second_success_moves_current_to_previous(self):
        first = deploy_state.record_success({}, TAG_A, REF_A, DIGEST_A, at="t1")
        second = deploy_state.record_success(first, TAG_B, REF_B, DIGEST_B, at="t2")
        self.assertEqual(second["tag"], TAG_B)
        self.assertEqual(second["previous_tag"], TAG_A)
        self.assertEqual(second["previous_image_ref"], REF_A)
        self.assertEqual(second["previous_digest"], DIGEST_A)
        self.assertEqual(second["previous_deployed_at"], "t1")
        self.assertEqual(second["history"], [TAG_B, TAG_A])

    def test_force_same_tag_keeps_previous(self):
        first = deploy_state.record_success({}, TAG_A, REF_A, DIGEST_A, at="t1")
        second = deploy_state.record_success(first, TAG_B, REF_B, DIGEST_B, at="t2")
        again = deploy_state.record_success(second, TAG_B, REF_B, DIGEST_B, at="t3")
        self.assertEqual(again["tag"], TAG_B)
        self.assertEqual(again["previous_tag"], TAG_A)
        self.assertEqual(again["history"][:2], [TAG_B, TAG_A])

    def test_success_clears_previous_failure(self):
        failed = {
            "tag": TAG_A,
            "failed_tag": TAG_B,
            "failed_at": "t1",
            "last_error": "boom",
        }
        state = deploy_state.record_success(failed, TAG_C, "ref", "digest", at="t2")
        self.assertIsNone(state["failed_tag"])
        self.assertIsNone(state["failed_at"])
        self.assertIsNone(state["last_error"])

    def test_failure_keeps_current_deployment(self):
        state = deploy_state.record_success({}, TAG_A, REF_A, DIGEST_A, at="t1")
        failed = deploy_state.record_failure(state, TAG_B, "health failed", at="t2")
        self.assertEqual(failed["tag"], TAG_A)
        self.assertIsNone(failed["previous_tag"])
        self.assertEqual(failed["failed_tag"], TAG_B)
        self.assertEqual(failed["failed_at"], "t2")
        self.assertEqual(failed["last_error"], "health failed")

    def test_failure_truncates_error(self):
        state = deploy_state.record_failure({}, TAG_A, "x" * 1000, at="t1")
        self.assertEqual(len(state["last_error"]), 500)

    def test_history_is_capped_and_deduped(self):
        state = {}
        for index in range(12):
            tag = "image-" + f"{index:012x}"
            state = deploy_state.record_success(
                state, tag, f"ref-{index}", f"digest-{index}"
            )
        self.assertEqual(len(state["history"]), 10)
        self.assertEqual(state["history"][0], "image-00000000000b")
        # 같은 태그 재배포는 history를 늘리지 않는다.
        again = deploy_state.record_success(state, state["tag"], "ref", "digest")
        self.assertEqual(len(again["history"]), 10)
        self.assertEqual(len(set(again["history"])), 10)


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
            state = deploy_state.record_success({}, TAG_A, REF_A, DIGEST_A, at="t1")
            deploy_state.save_state(path, state)
            self.assertEqual(deploy_state.load_state(path), state)
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "state.json"]
            self.assertEqual(leftovers, [])
            json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
