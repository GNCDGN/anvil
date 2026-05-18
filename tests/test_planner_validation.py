"""Tests for _validate_plan_structure — Phase 1 Step 4.

One test per validation rule (design Part 3, eight checks), a happy
path, and the escalation short-circuit (valid passes, malformed raises).
brief_step is a synthetic anvil.brief.Step; committed fixtures align to
it. Rules without a committed fixture (out-of-scope operation,
escalation_triggers shape, scope_boundaries shape) are built inline by
mutating the parsed valid plan.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from anvil import planner
from anvil.brief import Step

_FIX = Path(__file__).resolve().parent / "fixtures" / "planner"


def _step():
    return Step(
        number=1,
        name="t",
        scope_files=["a.py", "b.py"],
        scope_operations=["write", "commit"],
        smoke="echo x",
        confirm="explicit",
    )


def _fixture(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


def _valid() -> dict:
    return _fixture("stage_b_valid_plan.txt")


class ValidatePlanStructureTests(unittest.TestCase):
    def test_happy_path_valid_plan_returns_none(self):
        self.assertIsNone(
            planner._validate_plan_structure(_valid(), _step())
        )

    def test_missing_field_raises(self):
        with self.assertRaises(planner.PlanValidationError) as cm:
            planner._validate_plan_structure(
                _fixture("stage_b_missing_field.txt"), _step()
            )
        self.assertIn("missing field: commit_message", str(cm.exception))

    def test_step_number_mismatch_raises(self):
        with self.assertRaises(planner.PlanValidationError) as cm:
            planner._validate_plan_structure(
                _fixture("stage_b_step_mismatch.txt"), _step()
            )
        self.assertIn("step_number mismatch", str(cm.exception))

    def test_out_of_scope_file_raises(self):
        with self.assertRaises(planner.PlanValidationError) as cm:
            planner._validate_plan_structure(
                _fixture("stage_b_out_of_scope_file.txt"), _step()
            )
        self.assertIn("out-of-scope file: c.py", str(cm.exception))

    def test_out_of_scope_operation_raises(self):
        plan = _valid()
        plan["operations"] = ["deploy"]
        with self.assertRaises(planner.PlanValidationError) as cm:
            planner._validate_plan_structure(plan, _step())
        self.assertIn("out-of-scope operation: deploy", str(cm.exception))

    def test_invalid_confidence_raises(self):
        with self.assertRaises(planner.PlanValidationError) as cm:
            planner._validate_plan_structure(
                _fixture("stage_b_invalid_confidence.txt"), _step()
            )
        self.assertIn("invalid confidence: 100%", str(cm.exception))

    def test_escalation_triggers_wrong_shape_raises(self):
        plan = _valid()
        plan["escalation_triggers"] = "nope"
        with self.assertRaises(planner.PlanValidationError) as cm:
            planner._validate_plan_structure(plan, _step())
        self.assertIn("escalation_triggers must be list[str]", str(cm.exception))

    def test_scope_boundaries_wrong_shape_raises(self):
        plan = _valid()
        plan["scope_boundaries"] = ["x"]
        with self.assertRaises(planner.PlanValidationError):
            planner._validate_plan_structure(plan, _step())

    def test_valid_escalation_returns_none(self):
        self.assertIsNone(
            planner._validate_plan_structure(
                _fixture("stage_b_escalation.txt"), _step()
            )
        )

    def test_malformed_escalation_raises(self):
        plan = {"escalate": True, "detail": "x", "step_number": 1}
        with self.assertRaises(planner.PlanValidationError) as cm:
            planner._validate_plan_structure(plan, _step())
        self.assertIn("reason", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
