"""Phase 2 Step 7 — git_ops.head_hash() tests.

Hermetic /tmp git fixtures, same posture as tests/test_git_ops.py.
Three cases cover the helper's contract:
  (a) clean repo with one commit → head_hash returns that SHA
  (b) repo with multiple commits → head_hash returns the latest
  (c) non-git path → head_hash returns None (never raises)

The Step 9 orchestrator wiring uses head_hash to populate state.commit
when commit_step returns "" (manual-mode case). These tests cover the
helper in isolation; orchestrator integration lands at Step 9.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from anvil.git_ops import head_hash


def _init_repo(repo: Path, files: dict[str, str] | None = None) -> str:
    """Initialise a git repo, optionally with some files, and commit. Returns
    the resulting commit SHA so the test can compare against it."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   check=True, capture_output=True)
    for name, content in (files or {"keep": ""}).items():
        (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   capture_output=True)
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


class HeadHashTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-d14-"))
        # Hermetic guard — never the build's own repo.
        self.assertNotIn("Downloads", str(self._tmp))

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_a_clean_repo_one_commit_returns_sha(self):
        repo = self._tmp / "repo"
        sha = _init_repo(repo, {"a.txt": "hello\n"})
        result = head_hash(repo)
        self.assertEqual(result, sha)
        self.assertEqual(len(result), 40)  # full SHA

    def test_b_multiple_commits_returns_latest(self):
        repo = self._tmp / "repo"
        first_sha = _init_repo(repo, {"a.txt": "1\n"})
        (repo / "b.txt").write_text("2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-qm", "second"],
            cwd=repo, check=True, capture_output=True,
        )
        result = head_hash(repo)
        self.assertNotEqual(result, first_sha)
        self.assertEqual(len(result), 40)

    def test_c_non_git_path_returns_none(self):
        # An empty directory is not a git repo.
        non_repo = self._tmp / "not-a-repo"
        non_repo.mkdir()
        result = head_hash(non_repo)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
