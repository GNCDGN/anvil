"""Phase 1 Step 6 — end-to-end Planner integration, mocked at the SDK
level (patch anvil.planner.anthropic.Anthropic, deeper than Step 5's
method-level mock).

The mock client's messages.stream(...) returns a fake context manager
whose get_final_message() yields the staged fixture text plus a usage
object. side_effect sequences Stage A then Stage B (and retry).
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anvil.brief import Brief, Step
from anvil.planner import Plan
from anvil.state import init_state

_FIX = Path(__file__).resolve().parent / "fixtures" / "planner"
_VALID = (_FIX / "stage_b_valid_plan.txt").read_text(encoding="utf-8")
_INVALID = (_FIX / "stage_b_invalid_then_valid_first.txt").read_text(encoding="utf-8")
_ESCALATION = (_FIX / "stage_b_escalation.txt").read_text(encoding="utf-8")


def _brief_and_state():
    brief = Brief(
        brief_version=1,
        project="anvil",
        build_name="integration",
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


class _FakeCM:
    """A messages.stream(...) context manager whose get_final_message()
    returns one canned response + usage."""

    def __init__(self, text: str):
        self._text = text

    def __enter__(self):
        msg = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )
        return SimpleNamespace(get_final_message=lambda: msg)

    def __exit__(self, *exc):
        return False


class PlannerIntegrationTests(unittest.TestCase):
    def _run(self, responses):
        """Patch the SDK, stage `responses` across stream() calls, run
        plan_step. Returns (result, stream_call_count)."""
        with mock.patch("anvil.planner.anthropic.Anthropic") as MockA:
            client = MockA.return_value
            client.with_options.return_value = client
            client.messages.stream.side_effect = [
                _FakeCM(r) for r in responses
            ]
            from anvil.planner import Planner

            p = Planner(api_key="x", model="claude-opus-4-7", timeout=120)
            brief, state = _brief_and_state()
            result = p.plan_step(brief, state, 0)
            return result, client.messages.stream.call_count

    def test_stage_a_empty_then_valid_plan(self):
        result, calls = self._run(["", _VALID])
        self.assertIsInstance(result, Plan)
        self.assertEqual(result.step_number, 1)
        self.assertEqual(calls, 2)  # 1 Stage A + 1 Stage B

    def test_stage_b_invalid_then_retry_valid(self):
        result, calls = self._run(["", _INVALID, _VALID])
        self.assertIsInstance(result, Plan)
        self.assertEqual(result.commit_message, "Step 1: add helper")
        self.assertEqual(calls, 3)  # 1 Stage A + 2 Stage B (retry)

    def test_stage_b_invalid_twice_escalates(self):
        result, calls = self._run(["", _INVALID, _INVALID])
        self.assertIsInstance(result, dict)
        self.assertIs(result["escalate"], True)
        self.assertEqual(result["reason"], "planner-validation-failure")
        self.assertEqual(calls, 3)

    def test_stage_b_model_escalates_directly_no_retry(self):
        result, calls = self._run(["", _ESCALATION])
        self.assertIsInstance(result, dict)
        self.assertIs(result["escalate"], True)
        self.assertEqual(result["reason"], "missing-decision")
        self.assertEqual(calls, 2)  # no retry on a model escalation


if __name__ == "__main__":
    unittest.main()
