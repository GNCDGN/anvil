"""v2 Phase 1 Step 6 — calibration_runner.py + auto-reply/prefix tests.

Covers the load-bearing pieces:
  - Five vault briefs parse + validate (rule 1..12).
  - Dry-run mode prints the plan + has zero side effects.
  - Auto-reply env propagation through Orchestrator (escalation +
    explicit-confirm short-circuits).
  - CALIBRATION_TELEGRAM_PREFIX env propagates to voice.format_*.
  - Budget cap aborts T5-real when cumulative + estimate exceeds cap.
  - Target repo bootstrap (git init + initial commit + idempotent).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Circular-import workaround.
import anvil.checkpoint  # noqa: F401

from anvil import voice
from anvil.config import Config
from anvil.orchestrator import Orchestrator
from anvil.planner import Plan

# Add repo root to sys.path so the test can import the tools package.
ANVIL_REPO = Path(__file__).resolve().parent.parent
if str(ANVIL_REPO) not in sys.path:
    sys.path.insert(0, str(ANVIL_REPO))
from tools import calibration_runner  # noqa: E402


class TestCalibrationBriefs(unittest.TestCase):
    """Each of the five vault briefs parses + validates."""

    def setUp(self) -> None:
        # The five target_repo_paths must exist + be git repos for
        # validate_or_reject rule 3. calibration_runner.parse_brief_only
        # already bootstraps; we just exercise it.
        pass

    def test_all_five_briefs_parse(self) -> None:
        for task in calibration_runner.DEFAULT_TASKS:
            ok, err = calibration_runner.parse_brief_only(task)
            self.assertTrue(ok, f"{task} failed: {err}")


class TestDryRun(unittest.TestCase):
    """--dry-run lists the plan, validates briefs, runs no subprocess."""

    def setUp(self) -> None:
        # Pre-bootstrap the target repos so dry-run's validate_or_reject
        # rule 3 (target_repo_path is a git repo) finds something even
        # when we spy on subprocess.run later. Idempotent.
        for t in calibration_runner.DEFAULT_TASKS:
            calibration_runner.bootstrap_target_repo(t)

    def test_dry_run_no_anvil_cli_subprocess(self) -> None:
        """No `python -m anvil.cli run` invocation during dry-run."""
        real_run = subprocess.run
        captured: list[list[str]] = []

        def spy(cmd, *a, **kw):
            captured.append(list(cmd) if isinstance(cmd, (list, tuple)) else [cmd])
            return real_run(cmd, *a, **kw)

        with mock.patch.object(calibration_runner.subprocess, "run",
                               side_effect=spy):
            rc = calibration_runner.sweep(
                tasks=("T1", "T2"), modes=("mock", "real"),
                dry_run=True,
            )
        self.assertEqual(rc, 0)
        anvil_cli = [c for c in captured
                     if any("anvil.cli" in str(part) for part in c)]
        self.assertEqual(anvil_cli, [],
                         f"dry-run invoked anvil.cli: {anvil_cli}")

    def test_dry_run_lists_full_plan(self) -> None:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            calibration_runner.sweep(dry_run=True)
        out = buf.getvalue()
        # Expect all 10 (task, mode) entries in the plan.
        for task in calibration_runner.DEFAULT_TASKS:
            self.assertIn(task, out)
        self.assertIn("mode=mock", out)
        self.assertIn("mode=real", out)
        self.assertIn("dry-run PASS", out)


class _OrchEnvTestBase(unittest.TestCase):
    """Shared setUp/tearDown for env-propagation tests through Orchestrator."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-cal-env-"))
        self._prev_state = os.environ.get("ANVIL_STATE_DIR")
        self._prev_root = os.environ.get("ANVIL_ROOT")
        os.environ["ANVIL_STATE_DIR"] = str(self._tmp / "state")
        os.environ["ANVIL_ROOT"] = str(self._tmp)
        # Clean any inherited override.
        for k in ("AUTO_REPLY_FOR_CALIBRATION", "CALIBRATION_TELEGRAM_PREFIX"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, prev in (("ANVIL_STATE_DIR", self._prev_state),
                        ("ANVIL_ROOT", self._prev_root)):
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        for k in ("AUTO_REPLY_FOR_CALIBRATION", "CALIBRATION_TELEGRAM_PREFIX"):
            os.environ.pop(k, None)
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestAutoReplyEnvPropagation(_OrchEnvTestBase):
    """AUTO_REPLY_FOR_CALIBRATION short-circuits _await_user_decision."""

    def _orch(self):
        cfg = Config(
            anthropic_api_key="x", telegram_bot_token="t",
            telegram_chat_id="1", vault_path=self._tmp / "v",
            anvil_root=ANVIL_REPO, anvil_defer_window_seconds=300,
            planner_model="claude-opus-4-7", planner_timeout=120,
            coder_timeout=600,
        )
        return Orchestrator(cfg, coder_mode="manual",
                            planner=mock.MagicMock(),
                            telegram=mock.MagicMock(),
                            git=mock.MagicMock())

    def test_auto_reply_go_skips_telegram_wait(self) -> None:
        os.environ["AUTO_REPLY_FOR_CALIBRATION"] = "go"
        orch = self._orch()
        orch._pending_options = ("go", "abort")
        state = SimpleNamespace(current_step=1, status="running")
        # Add a transition shim so the orchestrator's transition() works.
        with mock.patch("anvil.orchestrator.transition", lambda s, *a, **k: s):
            result = orch._await_user_decision(state)
        self.assertTrue(result)
        orch.telegram.wait_for_reply.assert_not_called()

    def test_auto_reply_abort_returns_false(self) -> None:
        os.environ["AUTO_REPLY_FOR_CALIBRATION"] = "abort"
        orch = self._orch()
        orch._pending_options = ("go", "abort")
        state = SimpleNamespace(current_step=1, status="running")
        with mock.patch("anvil.orchestrator.transition", lambda s, *a, **k: s):
            result = orch._await_user_decision(state)
        self.assertFalse(result)
        orch.telegram.wait_for_reply.assert_not_called()


class TestCalibrationPrefix(_OrchEnvTestBase):
    """CALIBRATION_TELEGRAM_PREFIX overrides the [ANVIL] prefix in voice."""

    def test_prefix_overrides_step_completion(self) -> None:
        os.environ["CALIBRATION_TELEGRAM_PREFIX"] = "[ANVIL-calibration]"
        # Build a minimal Plan + state for format_step_completion.
        plan = Plan(
            step_number=1, step_name="X",
            files_to_touch=["x.py"], operations=["write"],
            approach="do it", smoke_test="echo",
            expected_outcome="ok", commit_message="x",
            scope_boundaries={"in_scope": "x.py", "out_of_scope": ""},
            confidence="high", escalation_triggers=[],
        )
        state = SimpleNamespace(current_step=1)
        msg = voice.format_step_completion(state, plan, "abc", "pass")
        self.assertTrue(msg.startswith("[ANVIL-calibration]"))

    def test_default_prefix_is_anvil(self) -> None:
        # Env unset → default "[ANVIL]"
        plan = Plan(
            step_number=1, step_name="X",
            files_to_touch=["x.py"], operations=["write"],
            approach="do it", smoke_test="echo",
            expected_outcome="ok", commit_message="x",
            scope_boundaries={"in_scope": "x.py", "out_of_scope": ""},
            confidence="high", escalation_triggers=[],
        )
        state = SimpleNamespace(current_step=1)
        msg = voice.format_step_completion(state, plan, "abc", "pass")
        self.assertTrue(msg.startswith("[ANVIL]"),
                        f"expected default [ANVIL] prefix, got: {msg[:30]}")


class TestBudgetCap(unittest.TestCase):
    """Real-mode runs abort when cumulative + estimate exceeds budget_cap."""

    def test_t5_real_aborts_when_cumulative_at_28(self) -> None:
        # Stub cumulative_real_spend → 28.00; T5 estimate = 3.75; cap = 30.
        # 28 + 3.75 = 31.75 > 30 → abort.
        captured: list[str] = []

        def fake_append_budget(line: str) -> None:
            captured.append(line)

        with mock.patch.object(calibration_runner, "cumulative_real_spend",
                               return_value=28.00), \
             mock.patch.object(calibration_runner, "run_one") as run_mock, \
             mock.patch.object(calibration_runner, "append_budget_log",
                               side_effect=fake_append_budget):
            rc = calibration_runner.sweep(
                tasks=("T5",), modes=("real",),
                dry_run=False, budget_cap=30.00,
            )
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()
        self.assertTrue(any("budget cap would be exceeded" in c
                            for c in captured),
                        f"no budget message in {captured}")


class TestTargetRepoBootstrap(unittest.TestCase):
    """bootstrap_target_repo creates a git repo with an initial commit;
    idempotent on re-invocation."""

    def setUp(self) -> None:
        # Move the target_repo_path to a tmp dir for this test only by
        # patching target_repo_path_for.
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-cal-boot-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_bootstrap_creates_git_repo_with_head(self) -> None:
        target = self._tmp / "T9"
        with mock.patch.object(calibration_runner, "target_repo_path_for",
                               return_value=target):
            calibration_runner.bootstrap_target_repo("T9")
        self.assertTrue((target / ".git").is_dir())
        # HEAD must resolve (initial commit landed).
        r = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.strip())  # non-empty SHA

    def test_bootstrap_is_idempotent(self) -> None:
        target = self._tmp / "T9"
        with mock.patch.object(calibration_runner, "target_repo_path_for",
                               return_value=target):
            calibration_runner.bootstrap_target_repo("T9")
            sha1 = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            # Re-invoke.
            calibration_runner.bootstrap_target_repo("T9")
            sha2 = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
        self.assertEqual(sha1, sha2)  # no new commit on idempotent call


if __name__ == "__main__":
    unittest.main()
