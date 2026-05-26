"""v4 Phase 1c Step 1: tests for anvil/integrations/vercel.py.

Every `vercel` call is mocked (Q-C1/Q-C6 — hermetic; no live deploy, no network;
the CLI is absent on the Mac regardless). The mock idiom matches
tests/test_github_issues.py: patch the module's `subprocess` attribute's `run`.
Coverage: argv construction (preview / --prod / project-targeting via --cwd),
URL parsing, and the never-raises contract (non-zero exit / missing CLI /
timeout / malformed output). NO scope/gate tests — deploy connectors have no
scope axis (Q-C8) and the confirmation gate is Step 2 (Amendment 1).
"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from anvil.integrations import vercel

_CWD = "/tmp/project"
_URL = "https://my-project-abc123.vercel.app"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["vercel"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(**kwargs):
    return mock.patch.object(vercel.subprocess, "run", **kwargs)


class TestDeploy(unittest.TestCase):
    def test_preview_happy_path(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            captured["cwd"] = kw.get("cwd")
            return _completed(stdout=f"Inspect: ...\n{_URL}\n")

        with _patch_run(side_effect=fake_run):
            res = vercel.deploy(_CWD)
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["url"], _URL)
        self.assertEqual(res["result"]["status"], "deployed")
        argv = captured["argv"]
        self.assertEqual(argv[0], "vercel")
        self.assertNotIn("--prod", argv)  # preview, not prod
        self.assertIn("--yes", argv)  # non-interactive
        self.assertEqual(captured["cwd"], _CWD)

    def test_prod_flag(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _completed(stdout=_URL)

        with _patch_run(side_effect=fake_run):
            res = vercel.deploy(_CWD, prod=True)
        self.assertTrue(res["ok"])
        self.assertIn("--prod", captured["argv"])

    def test_project_targeting_via_cwd_flag(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _completed(stdout=_URL)

        with _patch_run(side_effect=fake_run):
            res = vercel.deploy(_CWD, project="/tmp/other-project")
        self.assertTrue(res["ok"])
        argv = captured["argv"]
        self.assertIn("--cwd", argv)
        self.assertIn("/tmp/other-project", argv)

    def test_nonzero_exit_structured_error(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="build failed")):
            res = vercel.deploy(_CWD)
        self.assertFalse(res["ok"])
        self.assertIn("vercel exited 1", res["error"])
        self.assertIn("build failed", res["error"])

    def test_missing_cli_structured_error(self) -> None:
        with _patch_run(side_effect=FileNotFoundError()):
            res = vercel.deploy(_CWD)
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_timeout_structured_error(self) -> None:
        with _patch_run(side_effect=subprocess.TimeoutExpired(cmd="vercel", timeout=300)):
            res = vercel.deploy(_CWD)
        self.assertFalse(res["ok"])
        self.assertIn("timed out", res["error"])

    def test_malformed_output_no_url(self) -> None:
        # Zero exit but no http(s) URL on stdout → structured error.
        with _patch_run(return_value=_completed(stdout="some non-url noise\n")):
            res = vercel.deploy(_CWD)
        self.assertFalse(res["ok"])
        self.assertIn("no deploy URL", res["error"])


if __name__ == "__main__":
    unittest.main()
