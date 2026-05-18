"""Tests for Phase 1 Step 5 — retry/escalation wiring.

_call_anthropic is mocked at the METHOD level (not the SDK level):
patch.object(Planner, "_call_anthropic") with a side_effect list feeding
the staged fixture responses. No real Anthropic SDK call. Step 6's
integration test mocks deeper.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from anvil import planner
from anvil.brief import Brief, Step
from anvil.planner import Planner
from anvil.state import init_state

_FIX = Path(__file__).resolve().parent / "fixtures" / "planner"
_FIRST = (_FIX / "stage_b_invalid_then_valid_first.txt").read_text(encoding="utf-8")
_SECOND = (_FIX / "stage_b_invalid_then_valid_second.txt").read_text(encoding="utf-8")


def _brief_and_state():
    brief = Brief(
        brief_version=1,
        project="anvil",
        build_name="retry-test",
        target_repo="x",
        target_repo_path=Path("/tmp"),
        vps_deploy="no",
        steps=[
            Step(
                number=1,
                name="Example step",
                scope_files=["a.py", "b.py"],
                scope_operations=["write", "commit"],
                smoke="echo x",
                confirm="explicit",
            )
        ],
    )
    state = init_state(brief, "2026-05-18T00:00:00", brief_path="/nonexistent")
    return brief, state


class StageBRetryTests(unittest.TestCase):
    def setUp(self):
        self.brief, self.state = _brief_and_state()
        self.p = Planner()

    def _run(self, responses):
        with mock.patch.object(
            Planner, "_call_anthropic", side_effect=responses
        ) as m:
            result = self.p._run_stage_b_with_retry(
                self.brief, self.state, 0, []
            )
        return result, m

    def test_valid_first_no_retry(self):
        result, m = self._run([_SECOND])
        self.assertEqual(m.call_count, 1)
        self.assertNotIn("escalate", result)
        self.assertEqual(result["step_number"], 1)

    def test_invalid_first_valid_second_retries(self):
        result, m = self._run([_FIRST, _SECOND])
        self.assertEqual(m.call_count, 2)
        self.assertNotIn("escalate", result)
        self.assertEqual(result["commit_message"], "Step 1: add helper")

    def test_invalid_twice_escalates_with_both_errors(self):
        result, m = self._run([_FIRST, _FIRST])
        self.assertEqual(m.call_count, 2)
        self.assertIs(result["escalate"], True)
        self.assertEqual(result["reason"], "planner-validation-failure")
        self.assertIn("First:", result["detail"])
        self.assertIn("Second:", result["detail"])
        self.assertEqual(result["step_number"], 1)

    def test_empty_first_no_retry_escalates(self):
        result, m = self._run([""])
        self.assertEqual(m.call_count, 1)
        self.assertIs(result["escalate"], True)
        self.assertEqual(result["reason"], "planner-validation-failure")
        self.assertIn("empty after first attempt", result["detail"])

    def test_invalid_first_empty_second_escalates(self):
        result, m = self._run([_FIRST, ""])
        self.assertEqual(m.call_count, 2)
        self.assertIs(result["escalate"], True)
        self.assertIn("retry returned empty. First error:", result["detail"])

    def test_retry_prompt_contains_validation_error(self):
        _, m = self._run([_FIRST, _SECOND])
        retry_user = m.call_args_list[1].kwargs["user"]
        self.assertIn("## Previous attempt failed validation", retry_user)
        self.assertIn("<validation_error>", retry_user)
        self.assertIn("missing field: commit_message", retry_user)


if __name__ == "__main__":
    unittest.main()
