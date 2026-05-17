"""Step 6 tests — stub Planner + real validate_plan_scope.

No network, no LLM (Phase 0 stub). Stub plans are loaded from the committed
fixture and checked against the trivial brief's declared scope; the validator
is exercised positively (trivial) and negatively (invalid brief / crafted
out-of-scope plans) to prove it catches real Phase-1 violations.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from anvil.brief import Brief, Step, parse_brief
from anvil.errors import PlannerError
from anvil.planner import Plan, Planner, validate_plan_scope

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRIVIAL = FIXTURES / "trivial-test-brief.md"
INVALID = FIXTURES / "invalid-brief.md"
STUB = FIXTURES / "stub-plans.json"


class TestStubPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.trivial = parse_brief(TRIVIAL)
        self.invalid = parse_brief(INVALID)
        self.planner = Planner(stub_plans_path=STUB)

    def test_stub_plans_load_and_match_schema(self) -> None:
        plans = self.planner._load_stub_plans()
        self.assertEqual(len(plans), 3)
        models = [Plan.model_validate(p) for p in plans]  # raises if bad
        self.assertEqual([m.step_number for m in models], [1, 2, 3])

    def test_each_step_plan_in_scope_against_trivial(self) -> None:
        for idx in range(len(self.trivial.steps)):
            step = self.trivial.steps[idx]
            plan = self.planner.plan_step(self.trivial, None, idx)
            self.assertIsInstance(plan, Plan)
            self.assertEqual(plan.step_number, step.number)
            self.assertTrue(
                validate_plan_scope(plan, step),
                f"stub plan {plan.step_number} unexpectedly out of scope: "
                f"files={plan.files_to_touch} ops={plan.operations} vs "
                f"files={step.scope_files} ops={step.scope_operations}",
            )
        # Step 3 specifically must NOT claim 'write' (declared scope is
        # read/smoke-test/commit) — guards the item-2 conformance promise.
        p3 = self.planner.plan_step(self.trivial, None, 2)
        self.assertNotIn("write", p3.operations)
        self.assertEqual(set(p3.operations), {"read", "smoke-test", "commit"})

    def test_plan_step_out_of_range_raises(self) -> None:
        with self.assertRaises(PlannerError):
            self.planner.plan_step(self.trivial, None, 99)
        with self.assertRaises(PlannerError):
            self.planner.plan_step(self.trivial, None, -1)

    def test_no_matching_stub_plan_raises(self) -> None:
        # A brief whose only step is numbered 7 — no stub plan has that.
        b = Brief(
            brief_version=1, project="x", build_name="x", target_repo="x",
            target_repo_path=Path("/tmp"), vps_deploy="no",
            steps=[Step(
                number=7, name="Phantom", scope_files=["a.txt"],
                scope_operations=["write"], smoke="echo pass",
                confirm="explicit",
            )],
        )
        with self.assertRaises(PlannerError) as ctx:
            self.planner.plan_step(b, None, 0)
        self.assertIn("step_number 7", str(ctx.exception))

    def test_validate_plan_scope_catches_real_violations(self) -> None:
        good = self.planner.plan_step(self.trivial, None, 0)  # step 1 plan

        # 1. Same plan vs the INVALID brief's step 1 (scope.files
        #    ["../outside.txt"], ops ["write","teleport"]): "test.txt" is not
        #    in that file scope -> False.
        self.assertFalse(validate_plan_scope(good, self.invalid.steps[0]))

        # 2. Crafted plan touching an out-of-scope file vs trivial step 1.
        bad_file = good.model_copy(update={"files_to_touch": ["evil.py"]})
        self.assertFalse(validate_plan_scope(bad_file, self.trivial.steps[0]))

        # 3. Crafted plan with an out-of-scope operation vs trivial step 1.
        bad_op = good.model_copy(update={"operations": ["write", "deploy"]})
        self.assertFalse(validate_plan_scope(bad_op, self.trivial.steps[0]))

        # 4. Sanity: the unmodified good plan still validates True.
        self.assertTrue(validate_plan_scope(good, self.trivial.steps[0]))


if __name__ == "__main__":
    unittest.main()
