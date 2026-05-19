"""Phase 4 Step 5 tests — Orchestrator._draft_and_confirm_artefacts.

Hermetic: FakePlanner / FakeTelegram / tmp_path vault. Mocks vault_ops
and checkpoint module functions for failure injection.

Covers:
  - happy path: draft succeeds → preview → go → both writes → no escalation
  - setup-log-path-not-found: escalation with abort-only options
  - draft failure: escalation with go/abort; go path defers
  - abort reply at preview: no writes, no escalation, state stays done
  - idempotency: existing checkpoint → skip, no escalation
  - checkpoint-write-failed: escalation with go/abort
  - escalation_count increment: tick per _escalate call
  - state.coder_mode propagation: covered by Step 1 tests already
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from anvil import checkpoint as _checkpoint, vault_ops as _vault_ops
from anvil.orchestrator import Orchestrator
from anvil.state import State, StepState


def _fake_config(vault_path: Path):
    """Lightweight config — not the real Config dataclass, just what
    Orchestrator reads. Has every attribute the step-9 helper touches."""
    return SimpleNamespace(
        anthropic_api_key="sk-test",
        telegram_bot_token="t",
        telegram_chat_id="c",
        vault_path=vault_path,
        planner_model="claude-opus-4-7",
        planner_timeout=120,
        coder_timeout=600,
        claude_binary=None,
        coder_mode="manual",
        vps_host=None,
        vps_user="root",
        checkpoint_active_path=vault_path / "01-Projects/second-brain/checkpoints/active",
    )


def _fake_brief():
    return SimpleNamespace(
        project="anvil",
        build_name="Phase 4 — vault writes",
        target_repo_path=Path("/tmp/fake-repo"),
        steps=[],
        end_to_end_test=None,
        vps_deploy="no",
    )


def _fresh_state(brief_path: str = "/vault/01-Projects/code-workspace/anvil/builds/2026-05-19-anvil-phase-4/brief.md") -> State:
    return State(
        brief_path=brief_path,
        started_at="2026-05-19T14:22:00+01:00",
        status="done",
        current_step=1,
        steps=[],
        coder_mode="manual",
    )


class _Reply:
    """Stand-in for telegram.Update reply object — has a .text attribute."""
    def __init__(self, text: str) -> None:
        self.text = text


class TestDraftAndConfirmArtefacts(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-step5-"))
        # Build a minimal vault structure with a setup-log present
        self.vault = self.tmpdir / "vault"
        self.project_dir = self.vault / "01-Projects/code-workspace/anvil"
        self.project_dir.mkdir(parents=True)
        self.setup_log = self.project_dir / "setup-log.md"
        self.setup_log.write_text("# setup-log\n", encoding="utf-8")
        self.builds_dir = self.project_dir / "builds/2026-05-19-anvil-phase-4"
        self.builds_dir.mkdir(parents=True)
        self.brief_path = self.builds_dir / "brief.md"
        self.brief_path.write_text("brief content", encoding="utf-8")
        self.cp_dir = self.vault / "01-Projects/second-brain/checkpoints/active"
        self.cp_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_orch(self, planner_response=None, planner=None) -> Orchestrator:
        config = _fake_config(self.vault)
        if planner is None:
            planner = MagicMock()
            if planner_response is not None:
                planner.draft_completion_artefacts.return_value = planner_response
        telegram = MagicMock()
        git = MagicMock()
        git.head_hash.return_value = "abc1234567"
        orch = Orchestrator(
            config,
            planner=planner,
            telegram=telegram,
            git=git,
            run_smoke=MagicMock(),
        )
        return orch

    def test_happy_path_both_writes_succeed(self) -> None:
        draft = {
            "setup_log_entry": "## 2026-05-19 — anvil Phase 4 shipped\n\nbody",
            "checkpoint": "# Phase 4 shipped\n\n## What changed\n\nbody",
        }
        orch = self._build_orch(planner_response=draft)
        orch.telegram.wait_for_reply.return_value = _Reply("go")

        state = _fresh_state(str(self.brief_path))
        orch._state = state

        orch._draft_and_confirm_artefacts(_fake_brief(), self.brief_path, state)

        # Both files exist; setup-log appended; checkpoint created
        self.assertIn("## 2026-05-19 — anvil Phase 4 shipped",
                      self.setup_log.read_text(encoding="utf-8"))
        cp_files = list(self.cp_dir.glob("*.md"))
        self.assertEqual(len(cp_files), 1, f"expected one checkpoint, got: {cp_files}")
        # No escalations
        self.assertEqual(state.escalation_count, 0)

    def test_setup_log_path_missing_logs_and_skips(self) -> None:
        """Setup-log missing → soft skip (log + return), no escalation.

        Phase 4 Step 5b: changed from escalation to soft-skip. A missing
        setup-log is a pre-flight condition, not an active failure;
        proportional with the existing checkpoint-exists idempotent skip.
        """
        orch = self._build_orch(planner_response={"setup_log_entry": "x", "checkpoint": "y"})
        # Remove the setup-log so the derived path doesn\'t exist
        self.setup_log.unlink()

        state = _fresh_state(str(self.brief_path))
        orch._state = state

        orch._draft_and_confirm_artefacts(_fake_brief(), self.brief_path, state)

        # No escalation
        self.assertEqual(state.escalation_count, 0)
        # Planner was NOT called (we bailed before draft_and_preview)
        orch.planner.draft_completion_artefacts.assert_not_called()
        # No telegram send (preview never reached)
        orch.telegram.send.assert_not_called()

    def test_draft_failure_escalates_go_defers(self) -> None:
        """Planner returns escalation → escalate; user says go → defer, no writes."""
        escalation = {
            "escalate": True,
            "reason": "completion-artefacts-draft-failed",
            "detail": "both attempts failed",
            "step_number": 0,
        }
        orch = self._build_orch(planner_response=escalation)
        orch.telegram.wait_for_reply.return_value = _Reply("go")

        state = _fresh_state(str(self.brief_path))
        orch._state = state

        orch._draft_and_confirm_artefacts(_fake_brief(), self.brief_path, state)

        # Escalation fired
        self.assertEqual(state.escalation_count, 1)
        # No checkpoint written
        cp_files = list(self.cp_dir.glob("*.md"))
        self.assertEqual(len(cp_files), 0)
        # Setup-log unchanged (still just the seed line)
        self.assertEqual(self.setup_log.read_text(encoding="utf-8"), "# setup-log\n")

    def test_abort_reply_at_preview_no_writes(self) -> None:
        """User replies abort at preview → defer to manual, no writes, no escalation."""
        draft = {
            "setup_log_entry": "## entry\n\nbody",
            "checkpoint": "# Title\n\n## body",
        }
        orch = self._build_orch(planner_response=draft)
        orch.telegram.wait_for_reply.return_value = _Reply("abort")

        state = _fresh_state(str(self.brief_path))
        orch._state = state

        orch._draft_and_confirm_artefacts(_fake_brief(), self.brief_path, state)

        # Preview is NOT an escalation — count stays 0
        self.assertEqual(state.escalation_count, 0)
        # No writes
        cp_files = list(self.cp_dir.glob("*.md"))
        self.assertEqual(len(cp_files), 0)
        self.assertEqual(self.setup_log.read_text(encoding="utf-8"), "# setup-log\n")

    def test_idempotent_skip_when_checkpoint_exists(self) -> None:
        """Re-run with existing checkpoint → skip silently, no escalation."""
        # Pre-create the checkpoint file at the derived path
        existing_cp = self.cp_dir / "2026-05-19-phase-4-vault-writes-shipped.md"
        existing_cp.write_text("EXISTING", encoding="utf-8")

        orch = self._build_orch(planner_response={"setup_log_entry": "x", "checkpoint": "y"})

        state = _fresh_state(str(self.brief_path))
        orch._state = state

        orch._draft_and_confirm_artefacts(_fake_brief(), self.brief_path, state)

        # No escalation, no planner call, existing file preserved
        self.assertEqual(state.escalation_count, 0)
        orch.planner.draft_completion_artefacts.assert_not_called()
        self.assertEqual(existing_cp.read_text(encoding="utf-8"), "EXISTING")

    def test_checkpoint_write_failure_escalates(self) -> None:
        """Setup-log writes but checkpoint fails → checkpoint-write-failed escalation."""
        draft = {
            "setup_log_entry": "## entry\n\nbody",
            "checkpoint": "# Title\n\n## body",
        }
        orch = self._build_orch(planner_response=draft)
        # First reply: go (preview), second reply: abort (escalation)
        orch.telegram.wait_for_reply.side_effect = [_Reply("go"), _Reply("abort")]

        state = _fresh_state(str(self.brief_path))
        orch._state = state

        # Inject failure on write_checkpoint
        def _bad_write_checkpoint(*a, **k):
            return (False, "checkpoint write failed: simulated")
        with patch.object(_vault_ops, "write_checkpoint", _bad_write_checkpoint):
            orch._draft_and_confirm_artefacts(_fake_brief(), self.brief_path, state)

        # Escalation fired
        self.assertEqual(state.escalation_count, 1)
        # Setup-log entry actually written (partial-write)
        self.assertIn("## entry", self.setup_log.read_text(encoding="utf-8"))

    def test_escalation_count_ticks_per_escalate(self) -> None:
        """Confirm escalation_count increments on every _escalate call."""
        orch = self._build_orch(planner_response={"setup_log_entry": "x", "checkpoint": "y"})

        state = _fresh_state(str(self.brief_path))
        orch._state = state

        self.assertEqual(state.escalation_count, 0)
        orch._escalate(state, "test-reason-1", "detail", options=("go", "abort"))
        self.assertEqual(state.escalation_count, 1)
        orch._escalate(state, "test-reason-2", "detail", options=("abort",))
        self.assertEqual(state.escalation_count, 2)


if __name__ == "__main__":
    unittest.main()
