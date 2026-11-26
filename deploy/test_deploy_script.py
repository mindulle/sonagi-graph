#!/usr/bin/env python3
"""배포 스크립트의 순수 함수 테스트.

`SONAGI_GRAPH_DEPLOY_LIB_ONLY=1 source`로 함수만 읽고, 임시 git 저장소와
langgraph.json으로 태그→sha 파싱, 기대 그래프 수 계산, 배포 클론 체크아웃을
검증한다. 실제 배포(gh/docker/langgraph)는 호출하지 않는다.
"""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("sonagi-graph-deploy.sh")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "deploy-test",
    "GIT_AUTHOR_EMAIL": "deploy-test@example.com",
    "GIT_COMMITTER_NAME": "deploy-test",
    "GIT_COMMITTER_EMAIL": "deploy-test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def bash_function(call, env_extra=None, cwd=None, log_path=None):
    """스크립트를 source한 뒤 `call`을 실행한다. stdout/stderr/rc를 돌려준다."""
    env = os.environ.copy()
    env.update(GIT_ENV)
    env["SONAGI_GRAPH_DEPLOY_LOG"] = str(log_path or os.devnull)
    env["SONAGI_GRAPH_DEPLOY_LIB_ONLY"] = "1"
    env.update(env_extra or {})
    script = (
        f"export SONAGI_GRAPH_DEPLOY_LIB_ONLY=1\n"
        f"source {shlex.quote(str(SCRIPT))}\n"
        f"{call}\n"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


def git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
        check=True,
    )


def write_graphs(repo, count):
    graphs = {f"graph_{index}": f"./graphs/g{index}.py:graph" for index in range(count)}
    (Path(repo) / "langgraph.json").write_text(
        json.dumps({"dependencies": ["."], "graphs": graphs}),
        encoding="utf-8",
    )


class TagShaTest(unittest.TestCase):
    def test_tag_to_sha(self):
        result = bash_function("tag_sha image-dd5c92b266a3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "dd5c92b266a3")

    def test_uppercase_is_lowercased(self):
        result = bash_function("tag_sha image-DD5C92B266A3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "dd5c92b266a3")

    def test_full_sha_is_accepted(self):
        full = "a" * 40
        result = bash_function(f"tag_sha image-{full}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), full)

    def test_bad_tags_fail_with_message(self):
        for value in ("image-", "image-nothex", "image-abc", "image-" + "a" * 41):
            with self.subTest(value=value):
                result = bash_function(f"tag_sha {shlex.quote(value)}")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("sha", result.stderr)

    def test_image_ref_roundtrip(self):
        ref = "ghcr.io/mindulle/sonagi-graph:sha-dd5c92b266a3"
        result = bash_function(
            f"tag_from_image_ref {shlex.quote(ref)}",
            env_extra={"SONAGI_GRAPH_IMAGE": "ghcr.io/mindulle/sonagi-graph"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "image-dd5c92b266a3")


class ExpectedGraphsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()

    def test_counts_langgraph_json(self):
        write_graphs(self.repo, 12)
        result = bash_function(
            "derive_expected_graphs",
            env_extra={"SONAGI_GRAPH_REPO": str(self.repo)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "12")

    def test_env_override_wins(self):
        write_graphs(self.repo, 3)
        result = bash_function(
            "derive_expected_graphs",
            env_extra={
                "SONAGI_GRAPH_REPO": str(self.repo),
                "SONAGI_GRAPH_EXPECTED_GRAPHS": "12",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "12")

    def test_bad_override_fails(self):
        write_graphs(self.repo, 3)
        result = bash_function(
            "derive_expected_graphs",
            env_extra={
                "SONAGI_GRAPH_REPO": str(self.repo),
                "SONAGI_GRAPH_EXPECTED_GRAPHS": "twelve",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SONAGI_GRAPH_EXPECTED_GRAPHS", result.stderr)

    def test_missing_config_fails_clearly(self):
        result = bash_function(
            "derive_expected_graphs",
            env_extra={"SONAGI_GRAPH_REPO": str(self.repo)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("langgraph.json", result.stderr)

    def test_broken_json_fails_clearly(self):
        (self.repo / "langgraph.json").write_text("{not json", encoding="utf-8")
        result = bash_function(
            "derive_expected_graphs",
            env_extra={"SONAGI_GRAPH_REPO": str(self.repo)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("langgraph.json", result.stderr)

    def test_graphs_must_be_object(self):
        (self.repo / "langgraph.json").write_text(
            json.dumps({"graphs": ["not", "a", "mapping"]}), encoding="utf-8"
        )
        result = bash_function(
            "derive_expected_graphs",
            env_extra={"SONAGI_GRAPH_REPO": str(self.repo)},
        )
        self.assertNotEqual(result.returncode, 0)


class PrepareCheckoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.work = root / "work"
        self.deploy = root / "deploy"
        self.agent = root / "agent"

        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=master", str(self.origin)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.work)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )
        (self.work / "docker-compose.deploy.yml").write_text("services: {}\n")
        (self.work / ".gitignore").write_text(".env\n")
        (self.work / ".env").write_text("EXAMPLE=1\n")
        write_graphs(self.work, 11)
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "eleven")
        git(self.work, "push", "origin", "master")
        self.sha_eleven = git(self.work, "rev-parse", "HEAD").stdout.strip()

        write_graphs(self.work, 12)
        git(self.work, "add", "langgraph.json")
        git(self.work, "commit", "-m", "twelve")
        git(self.work, "push", "origin", "master")
        self.sha_twelve = git(self.work, "rev-parse", "HEAD").stdout.strip()

        subprocess.run(
            ["git", "clone", str(self.origin), str(self.deploy)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )
        subprocess.run(
            ["git", "clone", str(self.origin), str(self.agent)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )

    def head(self, repo):
        return git(repo, "rev-parse", "HEAD").stdout.strip()

    def checkout(self, tag):
        return bash_function(
            f"prepare_checkout {shlex.quote(tag)}",
            env_extra={"SONAGI_GRAPH_REPO": str(self.deploy)},
        )

    def test_checkout_exact_commit(self):
        result = self.checkout(f"image-{self.sha_eleven[:12]}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(self.deploy), self.sha_eleven)
        self.assertIn(f"image-{self.sha_eleven[:12]}", result.stderr)
        # detached HEAD
        symbolic = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=self.deploy,
            capture_output=True,
            text=True,
            env={**os.environ, **GIT_ENV},
        )
        self.assertNotEqual(symbolic.returncode, 0)

    def test_checkout_moves_between_releases(self):
        self.assertEqual(self.checkout(f"image-{self.sha_twelve[:12]}").returncode, 0)
        self.assertEqual(self.head(self.deploy), self.sha_twelve)
        self.assertEqual(self.checkout(f"image-{self.sha_eleven[:12]}").returncode, 0)
        self.assertEqual(self.head(self.deploy), self.sha_eleven)
        data = json.loads((self.deploy / "langgraph.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["graphs"]), 11)

    def test_full_sha_tag(self):
        result = self.checkout(f"image-{self.sha_twelve}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.head(self.deploy), self.sha_twelve)

    def test_unknown_commit_fails_and_keeps_head(self):
        before = self.head(self.deploy)
        result = self.checkout("image-abcdef123456")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(self.deploy), before)

    def test_env_file_survives_checkout(self):
        (self.deploy / ".env").write_text("SECRET=1\n", encoding="utf-8")
        self.assertEqual(self.checkout(f"image-{self.sha_eleven[:12]}").returncode, 0)
        self.assertEqual(
            (self.deploy / ".env").read_text(encoding="utf-8"), "SECRET=1\n"
        )

    def test_agent_checkout_is_not_touched(self):
        self.assertEqual(self.checkout(f"image-{self.sha_twelve[:12]}").returncode, 0)
        self.assertEqual(self.head(self.agent), self.sha_twelve)
        self.assertEqual(self.checkout(f"image-{self.sha_eleven[:12]}").returncode, 0)
        self.assertEqual(self.head(self.agent), self.sha_twelve)


class SourceContractTest(unittest.TestCase):
    def test_graph_count_is_not_hardcoded(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('EXPECTED_GRAPHS="${SONAGI_GRAPH_EXPECTED_GRAPHS:-}"', text)
        self.assertNotIn("EXPECTED_GRAPHS:-11", text)

    def test_compose_project_is_pinned(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT"', text)

    def test_rollback_prepares_checkout(self):
        text = SCRIPT.read_text(encoding="utf-8")
        rollback = text.split("ROLLBACK_TAG=", 1)[1]
        self.assertIn('prepare_checkout "$ROLLBACK_TAG"', rollback)


class ServiceUnitTest(unittest.TestCase):
    def test_execstart_is_a_stable_copy_outside_the_clone(self):
        unit = (SCRIPT.parent / "sonagi-graph-deploy.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ExecStart=/usr/local/lib/sonagi-graph-deploy/sonagi-graph-deploy.sh", unit
        )
        self.assertIn("WorkingDirectory=/home/ubuntu/sonagi-graph-deploy", unit)
        self.assertIn(
            "Environment=SONAGI_GRAPH_REPO=/home/ubuntu/sonagi-graph-deploy", unit
        )
        # 배포 클론 안 스크립트를 직접 실행하면 낡은 커밋의 스크립트로
        # 다음 회차가 시작할 수 있다.
        self.assertNotIn("ExecStart=/home/ubuntu/sonagi-graph-deploy/deploy/", unit)

    def test_installer_refreshes_the_stable_copy(self):
        installer = (SCRIPT.parent / "install.sh").read_text(encoding="utf-8")
        self.assertIn('TOOL_DIR="/usr/local/lib/sonagi-graph-deploy"', installer)
        self.assertIn('"$TOOL_DIR/sonagi-graph-deploy.sh"', installer)
        self.assertIn('"$TOOL_DIR/deploy_state.py"', installer)


if __name__ == "__main__":
    sys.exit(unittest.main())
