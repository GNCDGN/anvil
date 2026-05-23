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
    """Each DEFAULT_TASKS vault brief parses + validates.

    v2 Phase 2 Step 4 follow-up: DEFAULT_TASKS gained T6 (write-new), so
    this now covers six briefs. parse_brief_only bootstraps each task's
    throwaway target repo before validating (rule 3 needs the target to
    exist + be a git repo)."""

    def test_all_six_briefs_parse(self) -> None:
        self.assertIn("T6", calibration_runner.DEFAULT_TASKS)
        for task in calibration_runner.DEFAULT_TASKS:
            ok, err = calibration_runner.parse_brief_only(task)
            self.assertTrue(ok, f"{task} failed: {err}")


class TestRunIdAndDirShape(unittest.TestCase):
    """v2 Phase 2 Step 1: run_id and run-dir both carry the mode segment
    so mock and real of the same task do not share state. This pairs
    with harness_v2's composite (run_id, mode) idempotency key — see
    test_harness_v2.TestPerTaskComparison."""

    def test_run_id_for_includes_mode_suffix(self) -> None:
        self.assertEqual(
            calibration_runner.run_id_for("T1", "mock"),
            "T1-doc-edit-mock",
        )
        self.assertEqual(
            calibration_runner.run_id_for("T1", "real"),
            "T1-doc-edit-real",
        )

    def test_run_dir_for_includes_mode_suffix(self) -> None:
        d_mock = calibration_runner.run_dir_for("T2", "mock")
        d_real = calibration_runner.run_dir_for("T2", "real")
        self.assertEqual(d_mock.name, "T2-two-step-mock")
        self.assertEqual(d_real.name, "T2-two-step-real")
        # Mock and real are siblings under the same runs/ root.
        self.assertEqual(d_mock.parent, d_real.parent)
        # The runs/ root lives under ANVIL_REPO/state/runs.
        self.assertEqual(d_mock.parent.name, "runs")

    def test_build_env_run_id_override_carries_mode(self) -> None:
        env_mock = calibration_runner.build_env("T3", "mock")
        env_real = calibration_runner.build_env("T3", "real")
        self.assertEqual(env_mock["ANVIL_RUN_ID_OVERRIDE"],
                         "T3-out-of-scope-mock")
        self.assertEqual(env_real["ANVIL_RUN_ID_OVERRIDE"],
                         "T3-out-of-scope-real")

    def test_build_env_sets_current_task_label_mode_independent(self) -> None:
        # v3 Phase 1b Step 3: ANVIL_CURRENT_TASK is the mode-independent task
        # label the orchestrator matches against the ANVIL_CANARY_TASKS allowlist.
        self.assertEqual(
            calibration_runner.build_env("T1", "mock")["ANVIL_CURRENT_TASK"],
            "T1-doc-edit")
        self.assertEqual(
            calibration_runner.build_env("T1", "real")["ANVIL_CURRENT_TASK"],
            "T1-doc-edit")


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


class TestAutoReplyArtefactPreview(_OrchEnvTestBase):
    """v2 Phase 1 Step 7 prep: the third short-circuit site (artefact-
    preview wait at orchestrator.py:~1101) is wired and bypasses
    telegram.wait_for_reply when AUTO_REPLY_FOR_CALIBRATION is set.
    """

    def test_artefact_preview_wait_short_circuits(self) -> None:
        # Reach the wait by entering _draft_and_confirm_artefacts past
        # the soft-skip paths. The cheapest entry: ensure setup_log_path
        # exists (so the soft-skip doesn't fire), checkpoint_path doesn't
        # exist (so the idempotent skip doesn't fire), draft_and_preview
        # returns a valid draft, and the telegram.send is benign.
        # Build the minimum shape inline.
        from anvil import checkpoint, vault_ops

        # Synthetic vault layout: VAULT/01-Projects/.../project/setup-log.md
        vault = self._tmp / "v"
        project_dir = (
            vault / "01-Projects" / "p" / "project"
        )
        builds_dir = project_dir / "builds" / "2026-05-20-x"
        builds_dir.mkdir(parents=True)
        brief_path = builds_dir / "brief.md"
        brief_path.write_text("---\n---\n", encoding="utf-8")
        (project_dir / "setup-log.md").write_text("seed\n", encoding="utf-8")

        cfg = Config(
            anthropic_api_key="x", telegram_bot_token="t",
            telegram_chat_id="1", vault_path=vault,
            anvil_root=ANVIL_REPO, anvil_defer_window_seconds=300,
            planner_model="claude-opus-4-7", planner_timeout=120,
            coder_timeout=600,
        )
        tg = mock.MagicMock()
        orch = Orchestrator(cfg, coder_mode="manual",
                            planner=mock.MagicMock(),
                            telegram=tg, git=mock.MagicMock())

        # Stub draft_and_preview to return a benign draft.
        draft = {
            "setup_log_entry": "## 2026-05-20\n\nEntry.\n",
            "checkpoint": "# CP\n\nBody.\n",
        }
        os.environ["AUTO_REPLY_FOR_CALIBRATION"] = "go"

        # Synthetic brief + state.
        brief = SimpleNamespace(
            target_repo_path=self._tmp / "repo",
            build_name="x",
            project="p",
            service_name=None,
        )
        state = SimpleNamespace(
            steps=[SimpleNamespace(status="done", commit=None)],
            current_step=1,
            status="done",
            started_at="2026-05-20T00:00:00",
            finished_at="2026-05-20T00:00:00",
            run_log=None,
            deploy=None,
            escalation_count=0,
            pending_action=None,
            vault_writes_outcome=None,
        )

        with mock.patch.object(checkpoint, "draft_and_preview",
                               return_value=(draft, "")), \
             mock.patch.object(checkpoint, "execute_writes",
                               return_value=(True, "")), \
             mock.patch.object(checkpoint, "compose_checkpoint_frontmatter",
                               return_value={}), \
             mock.patch("anvil.orchestrator.write_state"):
            orch._draft_and_confirm_artefacts(brief, brief_path, state)

        # The artefact-preview send fired, the wait did NOT.
        tg.send.assert_called_once()
        tg.wait_for_reply.assert_not_called()


class TestAutoReplyTelemetry(unittest.TestCase):
    """When CALIBRATION_TELEGRAM_PREFIX is set AND the env short-circuit
    fires, a `[calibration] auto-replied ...` log line surfaces."""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-cal-tel-"))
        self._prev_root = os.environ.get("ANVIL_ROOT")
        os.environ["ANVIL_ROOT"] = str(self._tmp)
        os.environ.pop("AUTO_REPLY_FOR_CALIBRATION", None)
        os.environ.pop("CALIBRATION_TELEGRAM_PREFIX", None)

    def tearDown(self) -> None:
        if self._prev_root is None:
            os.environ.pop("ANVIL_ROOT", None)
        else:
            os.environ["ANVIL_ROOT"] = self._prev_root
        for k in ("AUTO_REPLY_FOR_CALIBRATION", "CALIBRATION_TELEGRAM_PREFIX"):
            os.environ.pop(k, None)
        shutil.rmtree(self._tmp, ignore_errors=True)

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

    def test_telemetry_logs_when_calibration_prefix_set(self) -> None:
        os.environ["AUTO_REPLY_FOR_CALIBRATION"] = "go"
        os.environ["CALIBRATION_TELEGRAM_PREFIX"] = "[ANVIL-calibration]"
        orch = self._orch()
        orch._pending_options = ("go", "abort")
        state = SimpleNamespace(current_step=1, status="running")
        with mock.patch("anvil.orchestrator.transition", lambda s, *a, **k: s), \
             self.assertLogs("anvil.orchestrator", level="INFO") as captured:
            orch._await_user_decision(state)
        joined = "\n".join(captured.output)
        self.assertIn("[calibration] auto-replied 'go'", joined)
        self.assertIn("_await_user_decision", joined)

    def test_telemetry_silent_when_prefix_unset(self) -> None:
        # AUTO_REPLY set but no CALIBRATION_TELEGRAM_PREFIX → no log line.
        os.environ["AUTO_REPLY_FOR_CALIBRATION"] = "go"
        os.environ.pop("CALIBRATION_TELEGRAM_PREFIX", None)
        orch = self._orch()
        orch._pending_options = ("go", "abort")
        state = SimpleNamespace(current_step=1, status="running")
        with mock.patch("anvil.orchestrator.transition", lambda s, *a, **k: s):
            # assertNoLogs available in 3.10+; use captureWarnings fallback.
            import logging
            handler = logging.Handler()
            captured: list[str] = []
            handler.emit = lambda r: captured.append(r.getMessage())  # type: ignore[assignment]
            logger = logging.getLogger("anvil.orchestrator")
            logger.addHandler(handler)
            try:
                orch._await_user_decision(state)
            finally:
                logger.removeHandler(handler)
        # No "[calibration]" message in captured logs.
        self.assertFalse(any("[calibration]" in m for m in captured),
                         f"telemetry leaked without prefix: {captured}")


class TestBootstrapGpgConflict(unittest.TestCase):
    """bootstrap_target_repo surfaces a pre-commit-blocking config as a
    clear RuntimeError (Step 6 outcome finding 5)."""

    def test_bootstrap_raises_on_dry_run_failure(self) -> None:
        from unittest.mock import patch as up
        tmp = Path(tempfile.mkdtemp(prefix="anvil-cal-gpg-"))
        try:
            # Real bootstrap up to the dry-run check, then inject a
            # non-zero returncode for the --dry-run commit only.
            real_run = subprocess.run

            def selective(cmd, *a, **kw):
                # The Step 7 preflight commit is identified by its
                # commit-message string. Inject a GPG-signing failure
                # for that specific commit, let everything else (init,
                # baseline commit) run for real.
                if (
                    isinstance(cmd, list)
                    and "commit" in cmd
                    and any("calibration bootstrap dry-run" in str(c) for c in cmd)
                ):
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=1, stdout="",
                        stderr="error: gpg failed to sign the data\n",
                    )
                return real_run(cmd, *a, **kw)

            with up.object(calibration_runner, "target_repo_path_for",
                           return_value=tmp / "T9"), \
                 up("subprocess.run", side_effect=selective):
                with self.assertRaises(RuntimeError) as cm:
                    calibration_runner.bootstrap_target_repo("T9")
            self.assertIn("git config", str(cm.exception))
            self.assertIn("gpg", str(cm.exception).lower())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBudgetCap(unittest.TestCase):
    """Real-mode runs abort when cumulative + estimate exceeds budget_cap."""

    def test_t5_real_aborts_when_cumulative_at_28(self) -> None:
        # Stub cumulative_real_spend → 28.00; T5 estimate = 3.75; cap = 30.
        # 28 + 3.75 = 31.75 > 30 → abort. Patch the pre-sweep warning
        # so the test doesn't wait 5 seconds.
        captured: list[str] = []

        def fake_append_budget(line: str) -> None:
            captured.append(line)

        with mock.patch.object(calibration_runner, "cumulative_real_spend",
                               return_value=28.00), \
             mock.patch.object(calibration_runner, "run_one") as run_mock, \
             mock.patch.object(calibration_runner, "append_budget_log",
                               side_effect=fake_append_budget), \
             mock.patch.object(calibration_runner,
                               "_print_pre_sweep_warning"):
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

    def test_bootstrap_yields_identical_baseline_files_across_calls(self) -> None:
        """v2 Phase 1 Step 7 triage: bootstrap now wipes + re-seeds to
        guarantee deterministic _git_files_touched results across re-runs
        (e.g. mock then real of the same task). The HEAD SHA differs per
        call because git's commit timestamp varies; the baseline FILE
        SET is what we assert on."""
        target = self._tmp / "T9"
        with mock.patch.object(calibration_runner, "target_repo_path_for",
                               return_value=target):
            calibration_runner.bootstrap_target_repo("T9")
            files_1 = sorted(
                str(p.relative_to(target))
                for p in target.rglob("*")
                if p.is_file() and ".git" not in p.parts
            )
            calibration_runner.bootstrap_target_repo("T9")
            files_2 = sorted(
                str(p.relative_to(target))
                for p in target.rglob("*")
                if p.is_file() and ".git" not in p.parts
            )
        self.assertEqual(files_1, files_2)


if __name__ == "__main__":
    unittest.main()
