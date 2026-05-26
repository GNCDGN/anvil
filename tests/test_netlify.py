"""v4 Phase 1c Step 2: tests for anvil/integrations/netlify.py.

Every `netlify` call is mocked (Q-C2/Q-C6 — hermetic; the CLI is absent on the
Mac regardless). Every test uses a tmp deploy-history path (no live
state/deploy-history.json writes). Covers argv (netlify deploy / --prod /
--site), URL parsing, the never-raises ladder (non-zero / missing CLI / timeout
/ malformed), and the first-deploy confirmation gate (refuse-before-CLI;
confirmed-first proceeds + records; subsequent proceeds; CLI failure does not
record).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anvil.integrations import netlify

_CWD = "/tmp/project"
_URL = "https://my-site.netlify.app"
_PROJ = "anvil"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["netlify"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch_run(**kwargs):
    return mock.patch.object(netlify.subprocess, "run", **kwargs)


class _NetlifyTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-netlify-"))
        self.hpath = self._tmp / "deploy-history.json"

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_prior_success(self, target: str = "netlify") -> None:
        self.hpath.write_text(
            json.dumps([{"project": _PROJ, "target": target,
                         "result": "success", "url": "https://prior"}]),
            encoding="utf-8",
        )


class TestDeploy(_NetlifyTestBase):
    """Happy / error paths, all confirmed=True (first deploy passes the gate)."""

    def test_preview_happy_path(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            captured["cwd"] = kw.get("cwd")
            return _completed(stdout=f"Draft URL: {_URL}\n")

        with _patch_run(side_effect=fake_run):
            res = netlify.deploy(_CWD, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["url"], _URL)
        self.assertEqual(res["result"]["status"], "deployed")
        argv = captured["argv"]
        self.assertEqual(argv[:2], ["netlify", "deploy"])
        self.assertNotIn("--prod", argv)
        self.assertEqual(captured["cwd"], _CWD)

    def test_prod_flag(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _completed(stdout=_URL)

        with _patch_run(side_effect=fake_run):
            res = netlify.deploy(_CWD, prod=True, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertTrue(res["ok"])
        self.assertIn("--prod", captured["argv"])

    def test_site_targeting_flag(self) -> None:
        captured: dict = {}

        def fake_run(argv, *a, **kw):
            captured["argv"] = list(argv)
            return _completed(stdout=_URL)

        with _patch_run(side_effect=fake_run):
            res = netlify.deploy(_CWD, site="my-site-id", confirmed=True,
                                 project=_PROJ, history_path=self.hpath)
        self.assertTrue(res["ok"])
        argv = captured["argv"]
        self.assertIn("--site", argv)
        self.assertIn("my-site-id", argv)

    def test_nonzero_exit_structured_error(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="deploy failed")):
            res = netlify.deploy(_CWD, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("netlify exited 1", res["error"])

    def test_missing_cli_structured_error(self) -> None:
        with _patch_run(side_effect=FileNotFoundError()):
            res = netlify.deploy(_CWD, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    def test_timeout_structured_error(self) -> None:
        with _patch_run(side_effect=subprocess.TimeoutExpired(cmd="netlify", timeout=300)):
            res = netlify.deploy(_CWD, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("timed out", res["error"])

    def test_malformed_output_no_url(self) -> None:
        with _patch_run(return_value=_completed(stdout="no url here\n")):
            res = netlify.deploy(_CWD, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertIn("no deploy URL", res["error"])


class TestGate(_NetlifyTestBase):
    def test_first_deploy_without_confirmed_refused_no_cli(self) -> None:
        with _patch_run() as m_run, \
                mock.patch.object(netlify.deploy_history, "record_deploy") as m_rec:
            res = netlify.deploy(_CWD, project=_PROJ, history_path=self.hpath)
        self.assertFalse(res["ok"])
        self.assertTrue(res["requires_confirmation"])
        self.assertIn("deploy-confirmation-required", res["error"])
        m_run.assert_not_called()
        m_rec.assert_not_called()

    def test_first_deploy_with_confirmed_proceeds_and_records(self) -> None:
        with _patch_run(return_value=_completed(stdout=_URL)) as m_run, \
                mock.patch.object(netlify.deploy_history, "record_deploy",
                                  return_value={"ok": True}) as m_rec:
            res = netlify.deploy(_CWD, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertTrue(res["ok"])
        m_run.assert_called_once()
        m_rec.assert_called_once()
        # recorded with target=netlify, result=success
        _, kwargs = m_rec.call_args[0], m_rec.call_args
        args = m_rec.call_args[0]
        self.assertIn("netlify", args)
        self.assertIn("success", args)

    def test_subsequent_deploy_proceeds_without_confirmed(self) -> None:
        self._seed_prior_success()  # a prior netlify success → not first
        with _patch_run(return_value=_completed(stdout=_URL)) as m_run, \
                mock.patch.object(netlify.deploy_history, "record_deploy",
                                  return_value={"ok": True}) as m_rec:
            res = netlify.deploy(_CWD, project=_PROJ, history_path=self.hpath)  # confirmed defaults False
        self.assertTrue(res["ok"])
        m_run.assert_called_once()
        m_rec.assert_called_once()

    def test_cli_failure_on_confirmed_first_does_not_record(self) -> None:
        with _patch_run(return_value=_completed(returncode=1, stderr="boom")) as m_run, \
                mock.patch.object(netlify.deploy_history, "record_deploy") as m_rec:
            res = netlify.deploy(_CWD, confirmed=True, project=_PROJ,
                                 history_path=self.hpath)
        self.assertFalse(res["ok"])
        m_run.assert_called_once()
        m_rec.assert_not_called()  # only successes record


if __name__ == "__main__":
    unittest.main()
