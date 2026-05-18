"""Phase 1 Step 7 — orchestrator _plan_step resume-reuse + persistence.

Hermetic: ANVIL_STATE_DIR -> fresh tmp dir. The Orchestrator is built
with an injected mock planner + mock telegram and a SimpleNamespace
config (no real Planner is constructed when planner is injected).
"""
from __future__ import annotations

import json
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
from anvil.state import init_state, read_state, write_state

_VALID_PLAN = {
    "step_number": 1,
    "step_name": "Example step",
    "files_to_touch": ["a.py"],
    "operations": ["write", "commit"],
    "approach": "do it",
    "smoke_test": "echo x",
    "expected_outcome": "ok",
    "commit_message": "Step 1: x",
    "scope_boundaries": {"in_scope": "a.py", "out_of_scope": "rest"},
    "confidence": "high",
    "escalation_triggers": [],
}


def _brief_and_state():
    brief = Brief(
        brief_version=1, project="anvil", build_name="resume",
        target_repo="x", target_repo_path=Path("/tmp"), vps_deploy="no",
        steps=[Step(
            number=1, name="Example step", scope_files=["a.py", "b.py"],
            scope_operations=["write", "commit"], smoke="echo x",
            confirm="explicit",
        )],
    )
    state = init_state(brief, "2026-05-18T00:00:00", brief_path="/nonexistent")
    return brief, state


class PlannerResumeTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ANVIL_STATE_DIR")
        self._dir = Path(tempfile.mkdtemp(prefix="anvil-test-resume-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._dir)
        self.brief, self.state = _brief_and_state()
        self.planner = mock.Mock()
        self.orch = Orchestrator(
            SimpleNamespace(),
            planner=self.planner,
            telegram=mock.Mock(),
        )

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_persisted_plan_reused_without_planner(self):
        self.state.steps[0].plan = dict(_VALID_PLAN)
        result = self.orch._plan_step(self.brief, self.state, 0)
        self.assertIsInstance(result, Plan)
        self.assertEqual(result.step_number, 1)
        self.planner.plan_step.assert_not_called()

    def test_persisted_escalation_routes_without_planner(self):
        esc = {
            "escalate": True, "reason": "missing-decision",
            "detail": "d", "step_number": 1,
        }
        self.state.steps[0].plan = esc
        result = self.orch._plan_step(self.brief, self.state, 0)
        self.assertEqual(result, esc)
        self.planner.plan_step.assert_not_called()

    def test_plan_none_calls_planner_and_persists(self):
        self.state.steps[0].plan = None
        self.planner.plan_step.return_value = Plan(**_VALID_PLAN)
        result = self.orch._plan_step(self.brief, self.state, 0)
        self.assertIsInstance(result, Plan)
        self.planner.plan_step.assert_called_once()
        self.assertEqual(
            self.state.steps[0].plan, Plan(**_VALID_PLAN).model_dump()
        )
        # persisted to disk
        self.assertEqual(read_state().steps[0].plan["step_number"], 1)

    def test_phase0_v1_state_loads_plan_none_falls_through(self):
        raw = self.state.model_dump()
        raw["schema_version"] = 1
        for st in raw["steps"]:
            st.pop("plan", None)
            st.pop("coder_output", None)
        (self._dir / "current-run.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
        loaded = read_state()
        self.assertTrue(all(s.plan is None for s in loaded.steps))
        self.planner.plan_step.return_value = Plan(**_VALID_PLAN)
        result = self.orch._plan_step(self.brief, loaded, 0)
        self.assertIsInstance(result, Plan)
        self.planner.plan_step.assert_called_once()


if __name__ == "__main__":
    unittest.main()
