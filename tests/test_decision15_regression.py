"""Phase 2 Step 2 — decision #15 regression test.

The existing tests at the top of this file cover _plan_step in isolation.
Those pass. The bug at decision #15 was upstream: handle_brief always
called init_state(), which produces a fresh State with every step's
plan=None — silently invalidating _plan_step's reuse-guard on the resume
path.

These regression tests exercise the *full* path: Orchestrator.resume() →
handle_brief() → _plan_step(), asserting that on resume with a persisted
plan, the Planner is never called.

Hermetic: ANVIL_STATE_DIR -> fresh tmp dir; brief mocked; Telegram, smoke
runner, and git_ops injected; planner mock asserts not_called on the
positive cases.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anvil.brief import Brief, Step
from anvil.orchestrator import Orchestrator
from anvil.planner import Plan
from anvil.state import State, StepState, init_state, write_state


_VALID_PLAN_S1 = {
    "step_number": 1,
    "step_name": "Step one",
    "files_to_touch": ["a.py"],
    "operations": ["write", "commit"],
    "approach": "do it",
    "smoke_test": "echo s1",
    "expected_outcome": "ok",
    "commit_message": "Step 1: one",
    "scope_boundaries": {"in_scope": "a.py", "out_of_scope": "rest"},
    "confidence": "high",
    "escalation_triggers": [],
}

_VALID_PLAN_S2 = {
    **_VALID_PLAN_S1,
    "step_number": 2,
    "step_name": "Step two",
    "smoke_test": "echo s2",
    "commit_message": "Step 2: two",
}


def _two_step_brief() -> Brief:
    return Brief(
        brief_version=1, project="anvil", build_name="resume-regression",
        target_repo="x", target_repo_path=Path("/tmp"), vps_deploy="no",
        steps=[
            Step(
                number=1, name="Step one",
                scope_files=["a.py"], scope_operations=["write", "commit"],
                smoke="echo s1", confirm="explicit",
            ),
            Step(
                number=2, name="Step two",
                scope_files=["a.py"], scope_operations=["write", "commit"],
                smoke="echo s2", confirm="explicit",
            ),
        ],
    )


def _resumed_state_two_steps(brief: Brief, *,
                              s1_status="done", s1_plan=None,
                              s2_status="pending", s2_plan=None) -> State:
    """Build a State that simulates: step 1 completed in a prior session
    (with persisted plan), step 2 paused-mid-Telegram-wait with persisted
    plan. This is the exact shape the resume path needs to honour."""
    state = init_state(brief, "2026-05-18T00:00:00",
                       brief_path="/tmp/resume-regression.md",
                       coder_mode="manual")
    state.steps[0].status = s1_status
    state.steps[0].plan = s1_plan
    state.steps[0].commit = "abc1234" if s1_status == "done" else None
    state.steps[0].smoke = "pass" if s1_status == "done" else None
    state.steps[1].status = s2_status
    state.steps[1].plan = s2_plan
    state.current_step = 2 if s1_status == "done" else 1
    state.status = "paused-by-user"
    state.run_log = "/tmp/fake-run-log.md"
    return state


class _StubBriefParse:
    """Context manager that monkey-patches parse_brief / validate_or_reject
    / resolve_context_paths so handle_brief works against a brief we
    control without needing a real brief file on disk."""

    def __init__(self, brief: Brief):
        self.brief = brief
        self._patches: list = []

    def __enter__(self):
        for target, ret in [
            ("anvil.orchestrator.parse_brief", lambda _path: self.brief),
            ("anvil.orchestrator.validate_or_reject", lambda _b: None),
            ("anvil.orchestrator.resolve_context_paths",
             lambda b, _vp: b),
        ]:
            p = mock.patch(target, side_effect=ret)
            self._patches.append(p)
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class _ReplyQueue:
    """Telegram mock that returns a queue of canned replies."""

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)

    def wait_for_reply(self, timeout=None):
        if not self._replies:
            return SimpleNamespace(text="abort")
        return SimpleNamespace(text=self._replies.pop(0))


class Decision15RegressionTests(unittest.TestCase):
    """Full-path resume regression tests.

    These tests fail on the pre-fix orchestrator (handle_brief always
    calls init_state, clobbering the persisted plan) and pass after the
    Phase 2 Step 2 fix.
    """

    def setUp(self):
        self._prev = os.environ.get("ANVIL_STATE_DIR")
        self._dir = Path(tempfile.mkdtemp(prefix="anvil-test-d15-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._dir)
        self.brief = _two_step_brief()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev
        shutil.rmtree(self._dir, ignore_errors=True)

    def _build_orch(self, planner_mock, telegram_mock, *,
                    commit_returns="def5678"):
        git_mock = mock.Mock()
        git_mock.commit_step.return_value = commit_returns
        smoke_mock = mock.Mock(return_value=(True, "ok"))
        config = SimpleNamespace(
            anthropic_api_key="x", planner_model="x", planner_timeout=60,
            vault_path=Path("/tmp/no-vault"),
        )
        return Orchestrator(
            config,
            planner=planner_mock, telegram=telegram_mock,
            git=git_mock, run_smoke=smoke_mock,
        )

    def test_resume_with_persisted_plan_skips_planner_for_remaining_step(self):
        """Step 1 done, step 2 has persisted plan: handle_brief on resume
        reuses the persisted plan for step 2 and does not call Planner."""
        state = _resumed_state_two_steps(
            self.brief,
            s1_status="done",
            s1_plan=dict(_VALID_PLAN_S1),
            s2_status="pending",
            s2_plan=dict(_VALID_PLAN_S2),
        )
        write_state(state)

        planner = mock.Mock()
        # Reply queue: 'done' for manual coder step 2 execution, then 'go'
        # for the post-commit explicit confirmation. Step 1 is skipped.
        tg = _ReplyQueue(["done", "go"])

        orch = self._build_orch(planner, tg)

        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(
                Path("/tmp/resume-regression.md"),
                resumed_state=state,
            )

        self.assertEqual(rc, 0, "expected clean completion on resume")
        planner.plan_step.assert_not_called()
        # Step 1 must remain marked done and untouched
        self.assertEqual(orch._state.steps[0].status, "done")
        self.assertEqual(orch._state.steps[0].commit, "abc1234")
        # Step 2 must have completed via the manual flow
        self.assertEqual(orch._state.steps[1].status, "done")

    def test_resume_does_not_re_execute_completed_steps(self):
        """Step 1 done with persisted plan, step 2 done with persisted plan:
        handle_brief on resume should complete without touching anything —
        no Planner call, no manual-coder Telegram round-trip."""
        state = _resumed_state_two_steps(
            self.brief,
            s1_status="done",
            s1_plan=dict(_VALID_PLAN_S1),
            s2_status="done",
            s2_plan=dict(_VALID_PLAN_S2),
        )
        state.steps[1].commit = "def5678"
        state.steps[1].smoke = "pass"
        write_state(state)

        planner = mock.Mock()
        tg = _ReplyQueue([])  # no replies expected; assert never asked

        orch = self._build_orch(planner, tg)

        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(
                Path("/tmp/resume-regression.md"),
                resumed_state=state,
            )

        self.assertEqual(rc, 0)
        planner.plan_step.assert_not_called()
        # Telegram should not have been asked to wait for replies: the only
        # send is the completion message at wrap.
        self.assertEqual(len(tg.sent), 1, f"unexpected sends: {tg.sent}")

    def test_fresh_run_unchanged_by_resumed_state_kwarg_default(self):
        """A normal (non-resumed) handle_brief call still goes through
        init_state and the full flow — resumed_state defaults to None."""
        planner = mock.Mock()
        planner.plan_step.side_effect = [
            Plan(**_VALID_PLAN_S1), Plan(**_VALID_PLAN_S2),
        ]
        # Reply queue: done+go for step 1, done+go for step 2
        tg = _ReplyQueue(["done", "go", "done", "go"])
        orch = self._build_orch(planner, tg)

        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(Path("/tmp/resume-regression.md"))

        self.assertEqual(rc, 0)
        # Both steps planned by Planner on a fresh run
        self.assertEqual(planner.plan_step.call_count, 2)


if __name__ == "__main__":
    unittest.main()
