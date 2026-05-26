"""v4 Phase 1c: tests for anvil/integrations/vercel.py.

Step 1 shipped the bare wrapper; Step 2 retrofitted the first-deploy
confirmation gate (Amendment 1). These tests are gate-aware: the happy/error
paths pass `confirmed=True` (a first deploy) and a tmp `history_path`, and a
`TestGate` class exercises the gate behaviour (mirroring test_netlify.py).
Every `vercel` call is mocked (Q-C1/Q-C6 — hermetic; CLI absent on the Mac).

Step-2 semantics change (noted): `project` is now the deploy-history key, not a
`--cwd` target — the CLI targets the subprocess `cwd`. `test_project_is_history_
key_not_cli_flag` documents the dropped `--cwd` mapping.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anvil.integrations import vercel

_CWD = "/tmp/project"
_URL = "https://my-project-abc123.vercel.app"
_PROJ = "anvil"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["vercel"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(**kwargs):
    return mock.patch.object(vercel.subprocess, "run", **kwargs)


class _VercelTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-vercel-"))
        self.hpath = self._tmp / "deploy-history.json"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_prior_success(self, target: str = "vercel") -> None:
        self.hpath.write_text(
            json.dumps([{"project": _PROJ, "target": target,
                         "result": "success", "url": "https://prior"}]),
            encoding="utf-8",
        )


class TestDeploy(_VercelTestBase):
    """Happy / error paths, all confirmed=True (first deploy passes the gate)."""

    def test_preview_happy_path(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            captured["cwd"] = kw.get("cwd")
            return _completed(stdout=f"Inspect: ...\n{_URL}\n")

        with _patch_run(side_effect=fake_run):
            res = vercel.deploy(_CWD, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["url"], _URL)
        self.assertEqual(res["result"]["status"], "deployed")
        argv = captured["argv"]
        self.assertEqual(argv[0], "vercel")
        self.assertNotIn("--prod", argv)
        self.assertIn("--yes", argv)
        self.assertEqual(captured["cwd"], _CWD)

    def test_prod_flag(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _completed(stdout=_URL)

        with _patch_run(side_effect=fake_run):
            res = vercel.deploy(_CWD, prod=True, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertTrue(res["ok"])
        self.assertIn("--prod", captured["argv"])

    def test_project_is_history_key_not_cli_flag(self) -> None:
        # Step-2 retrofit: `project` is the deploy-history key, NOT a --cwd
        # target (the Step 1 --cwd mapping was dropped; the CLI targets cwd).
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _completed(stdout=_URL)

        with _patch_run(side_effect=fake_run):
            res = vercel.deploy(_CWD, project=_PROJ, confirmed=True,
                                history_path=self.hpath)
        self.assertTrue(res["ok"])
        self.assertNotIn("--cwd", captured["argv"])  # no longer mapped
        # the history file was keyed by project (a record was written)
        data = json.loads(self.hpath.read_text(encoding="utf-8"))
        self.assertEqual(data[-1]["project"], _PROJ)
        self.assertEqual(data[-1]["target"], "vercel")

    def test_nonzero_exit_structured_error(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="build failed")):
            res = vercel.deploy(_CWD, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("vercel exited 1", res["error"])
        self.assertIn("build failed", res["error"])

    def test_missing_cli_structured_error(self) -> None:
        with _patch_run(side_effect=FileNotFoundError()):
            res = vercel.deploy(_CWD, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_timeout_structured_error(self) -> None:
        with _patch_run(side_effect=subprocess.TimeoutExpired(cmd="vercel", timeout=300)):
            res = vercel.deploy(_CWD, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("timed out", res["error"])

    def test_malformed_output_no_url(self) -> None:
        with _patch_run(return_value=_completed(stdout="some non-url noise\n")):
            res = vercel.deploy(_CWD, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("no deploy URL", res["error"])


class TestGate(_VercelTestBase):
    def test_first_deploy_without_confirmed_refused_no_cli(self) -> None:
        with _patch_run() as m_run, \
                mock.patch.object(vercel.deploy_history, "record_deploy") as m_rec:
            res = vercel.deploy(_CWD, project=_PROJ, history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertTrue(res["requires_confirmation"])
        self.assertIn("deploy-confirmation-required", res["error"])
        m_run.assert_not_called()
        m_rec.assert_not_called()

    def test_first_deploy_with_confirmed_proceeds_and_records(self) -> None:
        with _patch_run(return_value=_completed(stdout=_URL)) as m_run, \
                mock.patch.object(vercel.deploy_history, "record_deploy",
                                  return_value={"ok": True}) as m_rec:
            res = vercel.deploy(_CWD, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertTrue(res["ok"])
        m_run.assert_called_once()
        m_rec.assert_called_once()
        args = m_rec.call_args[0]
        self.assertIn("vercel", args)
        self.assertIn("success", args)

    def test_subsequent_deploy_proceeds_without_confirmed(self) -> None:
        self._seed_prior_success()
        with _patch_run(return_value=_completed(stdout=_URL)) as m_run, \
                mock.patch.object(vercel.deploy_history, "record_deploy",
                                  return_value={"ok": True}) as m_rec:
            res = vercel.deploy(_CWD, project=_PROJ, history_path=self.hpath)
        self.assertTrue(res["ok"])
        m_run.assert_called_once()
        m_rec.assert_called_once()

    def test_cli_failure_on_confirmed_first_does_not_record(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="boom")) as m_run, \
                mock.patch.object(vercel.deploy_history, "record_deploy") as m_rec:
            res = vercel.deploy(_CWD, confirmed=True, project=_PROJ,
                                history_path=self.hpath)
        self.assertFalse(res["ok"])
        m_run.assert_called_once()
        m_rec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
