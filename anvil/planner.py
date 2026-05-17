"""Planner (implementation-notes Component 3 / design "Plan output schema").

Phase 0: a STUB Planner. There is no LLM call — `plan_step` reads
`tests/fixtures/stub-plans.json` and returns the hardcoded plan whose
`step_number` matches the brief step at the requested index. Phase 1 replaces
this body with the real two-stage Opus-driven Planner; the `Plan` model and
`validate_plan_scope` below are kept as-is for Phase 1.

`validate_plan_scope` is REAL validation (not a stub): it enforces that a
plan's `files_to_touch` ⊆ the brief step's declared `scope.files` and its
`operations` ⊆ declared `scope.operations`. The stub plans happen to pass it;
it must still catch genuine out-of-scope plans when the real Planner runs.

Component-3-faithfulness notes (Phase-0 stub adaptations, not deviations):
- Component 3's `Planner.__init__(api_key, model, timeout)` targets the real
  Phase 1 Planner. The Phase 0 stub needs none of those; they are accepted
  but optional, and `stub_plans_path` is added (defaults to the fixture).
  Phase 1 removes the stub path and uses api_key/model/timeout.
- `step_idx` is 0-based (the orchestrator loops `range(len(brief.steps))`);
  `Plan.step_number` is 1-based. `plan_step` maps via
  `brief.steps[step_idx].number` and raises `PlannerError` if no stub plan
  matches that number.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from anvil.errors import PlannerError

_DEFAULT_STUB_PLANS = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "stub-plans.json"
)


class ScopeBoundaries(BaseModel):
    in_scope: str
    out_of_scope: str


class Plan(BaseModel):
    step_number: int
    step_name: str
    files_to_touch: list[str]
    operations: list[str]
    approach: str
    smoke_test: str
    expected_outcome: str
    commit_message: str
    scope_boundaries: ScopeBoundaries
    confidence: Literal["high", "medium", "low"]
    escalation_triggers: list[str] = []


def validate_plan_scope(plan: Plan, step) -> bool:
    """REAL validation. True iff the plan stays within the brief step's
    declared scope:
      - every path in plan.files_to_touch is in step.scope_files
      - every op in plan.operations is in step.scope_operations
    Returns False on any out-of-scope file or operation. Used by the
    orchestrator: `if not validate_plan_scope(...): escalate(...)`.
    """
    files_ok = set(plan.files_to_touch).issubset(set(step.scope_files))
    ops_ok = set(plan.operations).issubset(set(step.scope_operations))
    return files_ok and ops_ok


class Planner:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        *,
        stub_plans_path: Path | None = None,
    ) -> None:
        # Phase 0 stub ignores api_key/model/timeout (no LLM). Retained in
        # the signature so Phase 1 can drop in the real Planner unchanged.
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._stub_plans_path = Path(stub_plans_path or _DEFAULT_STUB_PLANS)

    def _load_stub_plans(self) -> list[dict]:
        try:
            data = json.loads(self._stub_plans_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PlannerError(
                f"stub plans not found at {self._stub_plans_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise PlannerError(
                f"stub plans is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise PlannerError("stub-plans.json must be a JSON list of plans")
        return data

    def plan_step(self, brief, state, step_idx: int) -> Plan:
        """Phase 0: return the hardcoded plan whose step_number matches the
        brief step at step_idx (0-based). Raises PlannerError if step_idx is
        out of range or no stub plan matches that step number."""
        if step_idx < 0 or step_idx >= len(brief.steps):
            raise PlannerError(
                f"step_idx {step_idx} out of range "
                f"(brief has {len(brief.steps)} steps)"
            )
        target_number = brief.steps[step_idx].number
        for raw in self._load_stub_plans():
            if raw.get("step_number") == target_number:
                try:
                    return Plan.model_validate(raw)
                except Exception as exc:  # pydantic ValidationError
                    raise PlannerError(
                        f"stub plan for step {target_number} fails the Plan "
                        f"schema: {exc}"
                    ) from exc
        raise PlannerError(
            f"no stub plan for step_number {target_number} "
            f"(step_idx {step_idx})"
        )
