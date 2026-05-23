"""v3 Phase 1a Step 3 — routing policy engine shell tests.

The RoutingPolicy shell + RouteDecision: construction, the placeholder's
unconditional Opus, the never-raise degraded path (fallback to the per-stage
model), and decision_basis being a deep copy of whatever merged features it was
handed. The lint-wins MERGE itself is a wrapper concern (tested in
test_planner_events.py); here the policy just faithfully records what it's given.
"""
from __future__ import annotations

import unittest

from anvil.policy import (
    PHASE_1A_PLACEHOLDER,
    PHASE_1A_PLACEHOLDER_MODEL,
    RouteDecision,
    RoutingPolicy,
)


class TestRoutingPolicy(unittest.TestCase):

    def test_construction_stores_policy_version(self) -> None:
        p = RoutingPolicy(PHASE_1A_PLACEHOLDER)
        self.assertEqual(p.policy_version, "v3-phase-1a-placeholder")

    def test_decide_route_returns_route_decision_shape(self) -> None:
        d = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route("A", {"stage": "A"})
        self.assertIsInstance(d, RouteDecision)
        for f in ("route_candidate", "route_actual",
                  "route_fallback_fired", "decision_basis"):
            self.assertTrue(hasattr(d, f), f"missing field {f}")

    def test_placeholder_returns_opus_unconditionally(self) -> None:
        p = RoutingPolicy(PHASE_1A_PLACEHOLDER)
        for stage, feats in [
            ("A", {}),
            ("B", {"observed_prompt_token_count": 99999}),
            ("C", {"stage": "C", "step_count": 12, "has_vps_deploy": True}),
        ]:
            d = p.decide_route(stage, feats)
            self.assertEqual(d.route_candidate, PHASE_1A_PLACEHOLDER_MODEL)
            self.assertEqual(d.route_actual, PHASE_1A_PLACEHOLDER_MODEL)
            self.assertEqual(d.route_candidate, d.route_actual)
            self.assertEqual(d.route_actual, "claude-opus-4-7")

    def test_fallback_not_fired_on_happy_path(self) -> None:
        d = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route("A", {"stage": "A"})
        self.assertFalse(d.route_fallback_fired)

    def test_decide_route_never_raises_degraded_on_failure(self) -> None:
        # Inject failure: a non-dict, non-None features → dict(features) raises
        # inside decide_route → degraded path. route_actual falls back to the
        # wrapper-supplied per-stage model so it stays honest about what runs.
        d = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route(
            "C", 12345, fallback_model="claude-sonnet-4-6")
        self.assertTrue(d.route_fallback_fired)
        self.assertEqual(d.route_actual, "claude-sonnet-4-6")
        self.assertEqual(d.route_candidate, "claude-sonnet-4-6")
        self.assertIn("error", d.decision_basis)

    def test_degraded_without_fallback_model_uses_placeholder(self) -> None:
        # The brief's 2-arg call (no fallback_model) degrades to the placeholder.
        d = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route("A", 999)
        self.assertTrue(d.route_fallback_fired)
        self.assertEqual(d.route_actual, PHASE_1A_PLACEHOLDER_MODEL)

    def test_decision_basis_is_a_deep_copy(self) -> None:
        p = RoutingPolicy(PHASE_1A_PLACEHOLDER)
        feats = {"nested": [1, 2, 3], "stage": "A"}
        d = p.decide_route("A", feats)
        # Mutating the input after the decision must not touch the record.
        feats["nested"].append(999)
        feats["new_key"] = "x"
        self.assertEqual(d.decision_basis["nested"], [1, 2, 3])
        self.assertNotIn("new_key", d.decision_basis)


if __name__ == "__main__":
    unittest.main()
