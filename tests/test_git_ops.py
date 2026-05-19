"""Step 7 tests — git_ops against a throwaway /tmp repo ONLY.

Note-2 guard: setUp asserts the test repo is under tempfile.gettempdir()
and is NOT ~/Downloads/anvil, so the build's own git history can never be
contaminated. tearDown removes it. No real remote / no network (push is
tested only for the no-remote → False path).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from anvil.errors import GitError
from anvil.git_ops import (
    commit_step,
    files_changed_since,
    is_clean,
    push,
    revert_to,
)
from anvil.planner import Plan

ANVIL_REPO = Path(__file__).resolve().parent.parent  # ~/Downloads/anvil


def _plan(**over) -> Plan:
    base = dict(
        step_number=1,
        step_name="Create a file",
        files_to_touch=["test.txt"],
        operations=["write", "smoke-test", "commit"],
        approach="A" * 250,  # long, to exercise the 200-char truncation
        smoke_test="test -f test.txt && echo pass",
        expected_outcome="test.txt exists",
        commit_message="Step 1: Create a file",
        scope_boundaries={"in_scope": "test.txt", "out_of_scope": "all else"},
        confidence="high",
        escalation_triggers=[],
    )
    base.update(over)
    return Plan.model_validate(base)


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestGitOps(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="anvil-test-gitops-"))
        # --- Note-2 guard: never the ANVIL working repo ---
        self.assertTrue(
            str(self.repo).startswith(tempfile.gettempdir()),
            f"test repo {self.repo} is NOT under the system temp dir",
        )
        self.assertNotEqual(
            self.repo.resolve(), ANVIL_REPO.resolve(),
            "refusing to run git_ops tests against ~/Downloads/anvil",
        )
        _run(self.repo, "init", "-q")
        # Seed an initial commit so HEAD exists for diff/revert tests.
        (self.repo / "seed.txt").write_text("seed\n")
        subprocess.run(
            ["git", "-C", str(self.repo),
             "-c", "user.name=t", "-c", "user.email=t@t",
             "add", "-A"], check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo),
             "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "-m", "seed"], check=True, capture_output=True,
        )
        self.base = _run(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_is_clean(self) -> None:
        self.assertTrue(is_clean(self.repo))
        (self.repo / "test.txt").write_text("hello anvil\n")
        self.assertFalse(is_clean(self.repo))

    def test_commit_step_makes_commit_with_component7_message(self) -> None:
        (self.repo / "test.txt").write_text("hello anvil\n")
        sha = commit_step(
            self.repo, _plan(), 0,
            brief_name="Phase 0 — trivial round-trip",
            commit_message_hint="Step 1: Create a file",
            run_log_filename="2026-05-17-2200-trivial.md",
        )
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        body = _run(self.repo, "log", "-1", "--format=%B")
        self.assertIn("Step 1: Create a file — Step 1: Create a file", body)
        self.assertIn("Plan summary: " + "A" * 200 + "…", body)
        self.assertNotIn("A" * 201, body)  # truncated at 200
        self.assertIn("Brief: Phase 0 — trivial round-trip", body)
        self.assertIn("ANVIL run: 2026-05-17-2200-trivial.md", body)
        self.assertTrue(is_clean(self.repo))

    def test_commit_step_default_footer_when_optionals_none(self) -> None:
        (self.repo / "test.txt").write_text("x\n")
        commit_step(self.repo, _plan(), 0)  # no kw optionals
        body = _run(self.repo, "log", "-1", "--format=%B")
        self.assertIn("— auto", body)                  # commit_message_hint None
        self.assertIn("Brief: (unknown brief)", body)   # brief_name None
        self.assertIn("ANVIL run: (no run log)", body)  # run_log_filename None

    def test_commit_step_nothing_to_commit_returns_empty(self) -> None:
        before = _run(self.repo, "rev-parse", "HEAD")
        # Clean tree → nothing staged.
        self.assertEqual(commit_step(self.repo, _plan(), 0), "")
        # touch-only (mtime bump, no content change) is still no commit:
        (self.repo / "seed.txt").touch()
        self.assertEqual(commit_step(self.repo, _plan(), 0), "")
        self.assertEqual(_run(self.repo, "rev-parse", "HEAD"), before)

    def test_files_changed_since(self) -> None:
        (self.repo / "a.txt").write_text("a\n")
        commit_step(self.repo, _plan(step_name="add a"), 0)
        (self.repo / "b.txt").write_text("b\n")
        commit_step(self.repo, _plan(step_number=2, step_name="add b"), 1)
        changed = files_changed_since(self.repo, self.base)
        self.assertEqual(sorted(changed), ["a.txt", "b.txt"])

    def test_push_no_remote_returns_false(self) -> None:
        ok, _ = push(self.repo); self.assertFalse(ok)  # no 'origin' — False, no raise

    def test_revert_to(self) -> None:
        (self.repo / "c.txt").write_text("c\n")
        commit_step(self.repo, _plan(step_name="add c"), 0)
        self.assertTrue((self.repo / "c.txt").exists())
        self.assertTrue(revert_to(self.repo, self.base))
        self.assertFalse((self.repo / "c.txt").exists())
        self.assertEqual(_run(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertTrue(is_clean(self.repo))

    def test_git_error_on_bad_repo(self) -> None:
        with self.assertRaises(GitError):
            is_clean(Path(tempfile.gettempdir()) / "anvil-not-a-repo-xyzzy")


if __name__ == "__main__":
    unittest.main()
