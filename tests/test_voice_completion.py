"""Phase 3 Step 6 tests — completion message deploy block."""
from __future__ import annotations

import unittest
from pathlib import Path

from anvil.brief import Brief, Step
from anvil.state import State, StepState
from anvil import voice


def _make_brief(service_name=None) -> Brief:
    return Brief(
        brief_version=1, project="test", build_name="test-build",
        target_repo="github.com/test/test", target_repo_path=Path("/tmp/test"),
        vps_deploy="yes" if service_name else "no",
        service_name=service_name,
        vps_target_path="/home/test/test" if service_name else None,
        steps=[Step(
            number=1, name="trivial", scope_files=[],
            scope_operations=["read"], smoke="true", confirm="auto",
        )],
    )


def _make_state(deploy=None) -> State:
    return State(
        brief_path="/tmp/test/brief.md",
        started_at="2026-05-19T10:00:00+01:00",
        finished_at="2026-05-19T10:15:00+01:00",
        status="done",
        steps=[StepState(n=1, name="trivial", status="done", commit="abc", smoke="pass")],
        run_log="/tmp/test/run.log",
        deploy=deploy,
    )


class TestCompletionMessage(unittest.TestCase):
    def test_no_deploy_no_block(self) -> None:
        """state.deploy is None -> no Deploy: block in message."""
        brief = _make_brief()
        state = _make_state(deploy=None)
        msg = voice.format_completion(brief, state)
        self.assertIn("Build complete", msg)
        self.assertNotIn("Deploy:", msg)

    def test_deploy_complete_renders_block(self) -> None:
        """state.deploy populated with stage=complete -> block rendered."""
        brief = _make_brief(service_name="test.service")
        state = _make_state(deploy={
            "stage": "complete", "ok": True, "output": "",
            "vps_head_sha": "fed987654321abc", "service_status": "active",
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("Deploy:", msg)
        self.assertIn("complete", msg)
        self.assertIn("fed9876", msg)  # first 7 chars of sha
        self.assertIn("active", msg)
        self.assertIn("test.service", msg)

    def test_deploy_failed_renders_block_with_failed(self) -> None:
        """state.deploy with ok=False renders 'failed' marker."""
        brief = _make_brief(service_name="test.service")
        state = _make_state(deploy={
            "stage": "pull", "ok": False, "output": "non-fast-forward",
            "vps_head_sha": None, "service_status": None,
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("pull", msg)
        self.assertIn("failed", msg)

    def test_deploy_sha_truncated_to_7(self) -> None:
        """vps_head_sha is rendered as first 7 chars."""
        brief = _make_brief(service_name="test.service")
        state = _make_state(deploy={
            "stage": "complete", "ok": True, "output": "",
            "vps_head_sha": "abcdef1234567890123456789",
            "service_status": "active",
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("abcdef1", msg)


class TestCompletionVaultWrites(unittest.TestCase):
    """Phase 4 Step 6: Vault writes block in completion message."""

    def _state_with_vwo(self, vwo) -> State:
        return State(
            brief_path="/tmp/test/brief.md",
            started_at="2026-05-19T10:00:00+01:00",
            finished_at="2026-05-19T10:15:00+01:00",
            status="done",
            steps=[StepState(n=1, name="trivial", status="done",
                              commit="abc", smoke="pass")],
            run_log="/tmp/test/run.log",
            vault_writes_outcome=vwo,
        )

    def test_no_vwo_no_block(self) -> None:
        """vault_writes_outcome is None → no Vault writes: in message."""
        brief = _make_brief()
        state = self._state_with_vwo(None)
        msg = voice.format_completion(brief, state)
        self.assertNotIn("Vault writes", msg)

    def test_vwo_success_renders_basenames(self) -> None:
        """ok=True → block lists both basenames."""
        brief = _make_brief()
        state = self._state_with_vwo({
            "setup_log_path": "/vault/anvil/setup-log.md",
            "checkpoint_path": "/vault/checkpoints/2026-05-19-anvil-phase-4-shipped.md",
            "ok": True,
            "error": None,
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("Vault writes:", msg)
        self.assertIn("setup-log.md", msg)
        self.assertIn("2026-05-19-anvil-phase-4-shipped.md", msg)
        self.assertNotIn("deferred to manual", msg)

    def test_vwo_failure_renders_deferred(self) -> None:
        """ok=False → block reads deferred to manual with error."""
        brief = _make_brief()
        state = self._state_with_vwo({
            "setup_log_path": "/vault/anvil/setup-log.md",
            "checkpoint_path": "/vault/checkpoints/x.md",
            "ok": False,
            "error": "checkpoint write failed: simulated",
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("Vault writes: deferred to manual", msg)
        self.assertIn("checkpoint write failed", msg)


if __name__ == "__main__":
    unittest.main()
