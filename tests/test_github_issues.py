"""v4 Phase 1b Step 1: tests for anvil/integrations/github_issues.py.

Every `gh` call is mocked (Q-B6 — hermetic; no live gh, no network). The mock
idiom matches tests/test_calibration_runner.py: patch the module's `subprocess`
attribute's `run`. Coverage: argv construction, JSON-result parsing, the
never-raises contract (non-zero exit / malformed JSON / missing gh), and scope
enforcement (a read-scoped create is refused WITHOUT invoking gh).
"""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from anvil.integrations import github_issues

_REPO = "github.com/GNCDGN/anvil"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """A fabricated CompletedProcess, the shape github_issues._run_gh reads."""
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(**kwargs):
    return mock.patch.object(github_issues.subprocess, "run", **kwargs)


class TestListIssues(unittest.TestCase):
    def test_happy_path_argv_and_parse(self) -> None:
        issues = [{"number": 1, "title": "x", "state": "open"}]
        captured: dict = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = list(cmd)
            return _completed(stdout=json.dumps(issues))

        with _patch_run(side_effect=fake_run):
            res = github_issues.list_issues(_REPO, scope="read")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"], issues)
        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["gh", "issue", "list"])
        self.assertIn("--repo", cmd)
        self.assertIn(_REPO, cmd)
        json_idx = cmd.index("--json")
        self.assertIn("number", cmd[json_idx + 1])
        # list omits the body field (kept small); view includes it.
        self.assertNotIn("body", cmd[json_idx + 1])

    def test_state_and_limit_flags(self) -> None:
        captured: dict = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = list(cmd)
            return _completed(stdout="[]")

        with _patch_run(side_effect=fake_run):
            res = github_issues.list_issues(
                _REPO, state="closed", limit=5, scope="read")
        self.assertTrue(res["ok"])
        cmd = captured["cmd"]
        self.assertIn("--state", cmd)
        self.assertIn("closed", cmd)
        self.assertIn("--limit", cmd)
        self.assertIn("5", cmd)

    def test_nonzero_exit_structured_error(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="boom")):
            res = github_issues.list_issues(_REPO, scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("gh exited 1", res["error"])
        self.assertIn("boom", res["error"])

    def test_malformed_json_structured_error(self) -> None:
        with _patch_run(return_value=_completed(stdout="not json{")):
            res = github_issues.list_issues(_REPO, scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("non-JSON", res["error"])

    def test_missing_gh_structured_error(self) -> None:
        with _patch_run(side_effect=FileNotFoundError()):
            res = github_issues.list_issues(_REPO, scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])


class TestViewIssue(unittest.TestCase):
    def test_happy_path_argv_and_parse(self) -> None:
        issue = {"number": 7, "title": "t", "body": "b", "state": "open"}
        captured: dict = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = list(cmd)
            return _completed(stdout=json.dumps(issue))

        with _patch_run(side_effect=fake_run):
            res = github_issues.view_issue(_REPO, 7, scope="read")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["number"], 7)
        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["gh", "issue", "view"])
        self.assertIn("7", cmd)
        json_idx = cmd.index("--json")
        self.assertIn("body", cmd[json_idx + 1])  # view includes body

    def test_nonzero_exit_structured_error(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="nope")):
            res = github_issues.view_issue(_REPO, 7, scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("gh exited 1", res["error"])


class TestCreateIssue(unittest.TestCase):
    def test_happy_path_argv_and_url(self) -> None:
        url = "https://github.com/GNCDGN/anvil/issues/42"
        captured: dict = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = list(cmd)
            return _completed(stdout=url + "\n")

        with _patch_run(side_effect=fake_run):
            res = github_issues.create_issue(
                _REPO, title="T", body="B", labels=["bug", "p1"], scope="write")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["url"], url)
        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["gh", "issue", "create"])
        self.assertIn("--title", cmd)
        self.assertIn("T", cmd)
        self.assertIn("--body", cmd)
        self.assertIn("B", cmd)
        # one --label per label, passed through (no label-creation logic).
        self.assertEqual(cmd.count("--label"), 2)
        self.assertIn("bug", cmd)
        self.assertIn("p1", cmd)

    def test_no_labels_emits_no_label_flag(self) -> None:
        captured: dict = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = list(cmd)
            return _completed(stdout="https://x/1")

        with _patch_run(side_effect=fake_run):
            res = github_issues.create_issue(
                _REPO, title="T", body="B", scope="write")
        self.assertTrue(res["ok"])
        self.assertEqual(captured["cmd"].count("--label"), 0)

    def test_nonzero_exit_structured_error(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="denied")):
            res = github_issues.create_issue(
                _REPO, title="T", body="B", scope="write")
        self.assertFalse(res["ok"])
        self.assertIn("gh exited 1", res["error"])


class TestScopeEnforcement(unittest.TestCase):
    def test_create_under_read_refused_without_gh(self) -> None:
        with _patch_run() as m:
            res = github_issues.create_issue(
                _REPO, title="T", body="B", scope="read")
        self.assertFalse(res["ok"])
        self.assertIn("create requires issues: write", res["error"])
        m.assert_not_called()

    def test_create_under_write_invokes_gh(self) -> None:
        with _patch_run(return_value=_completed(stdout="https://x/1")) as m:
            res = github_issues.create_issue(
                _REPO, title="T", body="B", scope="write")
        self.assertTrue(res["ok"])
        m.assert_called_once()

    def test_read_under_none_scope_refused_without_gh(self) -> None:
        with _patch_run() as m:
            res = github_issues.list_issues(_REPO, scope=None)
        self.assertFalse(res["ok"])
        self.assertIn("scope not declared", res["error"])
        m.assert_not_called()

    def test_list_under_read_succeeds(self) -> None:
        with _patch_run(return_value=_completed(stdout="[]")) as m:
            res = github_issues.list_issues(_REPO, scope="read")
        self.assertTrue(res["ok"])
        m.assert_called_once()

    def test_list_under_write_succeeds(self) -> None:
        # Reads proceed under a write scope too (criterion 2: reads under both).
        with _patch_run(return_value=_completed(stdout="[]")) as m:
            res = github_issues.list_issues(_REPO, scope="write")
        self.assertTrue(res["ok"])
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
