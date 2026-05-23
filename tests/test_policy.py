"""v3 Phase 1a Step 3 — routing policy engine shell tests.

The RoutingPolicy shell + RouteDecision: construction, the placeholder's
unconditional Opus, the never-raise degraded path (fallback to the per-stage
model), and decision_basis being a deep copy of whatever merged features it was
handed. The lint-wins MERGE itself is a wrapper concern (tested in
test_planner_events.py); here the policy just faithfully records what it's given.
"""
from __future__ import annotations

import unittest

from anvil.calibration import CHEAP_STAGE_A_MODEL, RoutingCalibration
from anvil.policy import (
    PHASE_1A_PLACEHOLDER,
    PHASE_1A_PLACEHOLDER_MODEL,
    PHASE_1B_STAGE_A_SHADOW,
    RouteDecision,
    RoutingPolicy,
)


def _haiku_calibration():
    """A CalibratedPolicy that recommends Haiku (high) for empty-context Stage A."""
    return RoutingCalibration(
        [{"context_paths_count": 0, "paths_returned": 0}]).policy


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


class TestRoutingPolicyCalibration(unittest.TestCase):
    """v3 Phase 1b Step 1: RoutingPolicy gains an optional calibration. In Step 1
    it is CONSULTED (recommendation recorded in decision_basis) but not acted on —
    decide_route still returns the placeholder (consult-not-act)."""

    def _calibration(self):
        # A calibrated policy that recommends Haiku for empty-context Stage A.
        return RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy

    def test_factory_returns_policy_with_calibration_stashed(self) -> None:
        cal = self._calibration()
        p = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route_with_calibration(cal)
        self.assertIsInstance(p, RoutingPolicy)
        self.assertIs(p.calibration, cal)
        self.assertEqual(p.policy_version, PHASE_1A_PLACEHOLDER)

    def test_calibration_none_is_back_compat(self) -> None:
        # No calibration → identical to Phase 1a (placeholder, no rationale key).
        d = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route(
            "A", {"context_paths_count": 0})
        self.assertEqual(d.route_candidate, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(d.route_actual, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertNotIn("calibration_rationale", d.decision_basis)

    def test_phase_1a_placeholder_consults_but_returns_placeholder(self) -> None:
        # v3 Phase 1b Step 2: this consult-not-act invariant is scoped to
        # PHASE_1A_PLACEHOLDER. PHASE_1B_STAGE_A_SHADOW deliberately acts on the
        # recommendation (route_candidate diverges) — tested separately.
        p = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route_with_calibration(
            self._calibration())
        d = p.decide_route("A", {"context_paths_count": 0})
        # Consult-not-act: candidate/actual are still the placeholder Opus...
        self.assertEqual(d.route_candidate, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(d.route_actual, PHASE_1A_PLACEHOLDER_MODEL)
        # ...but the calibration's Haiku recommendation IS recorded.
        cr = d.decision_basis["calibration_rationale"]
        self.assertEqual(cr["recommended_model"], CHEAP_STAGE_A_MODEL)
        self.assertEqual(cr["confidence_band"], "high")

    def test_calibration_rationale_is_stage_a_gated(self) -> None:
        p = RoutingPolicy(PHASE_1A_PLACEHOLDER).decide_route_with_calibration(
            self._calibration())
        self.assertIn(
            "calibration_rationale",
            p.decide_route("A", {"context_paths_count": 0}).decision_basis)
        self.assertNotIn(
            "calibration_rationale",
            p.decide_route("B", {"context_paths_count": 0}).decision_basis)

    def test_decide_route_degrades_if_calibration_raises(self) -> None:
        # decide_route's never-raise holds even if a (future, buggy) calibration
        # callable raises — the wrapper degrades to the fallback model.
        def _boom(features):
            raise RuntimeError("boom")

        p = RoutingPolicy(PHASE_1A_PLACEHOLDER, calibration=_boom)
        d = p.decide_route("A", {"context_paths_count": 0},
                           fallback_model="claude-opus-4-7")
        self.assertTrue(d.route_fallback_fired)
        self.assertIn("error", d.decision_basis)


class TestPhase1bStageAShadow(unittest.TestCase):
    """v3 Phase 1b Step 2: PHASE_1B_STAGE_A_SHADOW acts on the calibration for
    Stage A — route_candidate diverges to Haiku — while route_actual stays the
    per-stage Opus (the API call is unchanged; Step3-F1 inversion preserved)."""

    def _shadow(self):
        return RoutingPolicy(PHASE_1B_STAGE_A_SHADOW, calibration=_haiku_calibration())

    def test_constant_and_construction(self) -> None:
        self.assertEqual(PHASE_1B_STAGE_A_SHADOW, "v3-phase-1b-stage-a-shadow")
        self.assertEqual(self._shadow().policy_version, "v3-phase-1b-stage-a-shadow")

    def test_route_candidate_haiku_on_empty_context_stage_a(self) -> None:
        d = self._shadow().decide_route(
            "A", {"context_paths_count": 0}, fallback_model="claude-opus-4-7")
        self.assertEqual(d.route_candidate, CHEAP_STAGE_A_MODEL)
        cr = d.decision_basis["calibration_rationale"]
        self.assertEqual(cr["recommended_model"], CHEAP_STAGE_A_MODEL)

    def test_route_actual_stays_fallback_divergence_invariant(self) -> None:
        # The load-bearing A1 invariant: route_actual = the per-stage model
        # (what the API runs), so candidate != actual is the divergence.
        d = self._shadow().decide_route(
            "A", {"context_paths_count": 0}, fallback_model="claude-opus-4-7")
        self.assertEqual(d.route_actual, "claude-opus-4-7")
        self.assertNotEqual(d.route_candidate, d.route_actual)
        self.assertFalse(d.route_fallback_fired)

    def test_route_candidate_opus_on_uncalibrated_stage_a(self) -> None:
        # Non-empty context is uncalibrated → no divergence (both Opus), but
        # the unsupported-shape recommendation is still recorded.
        d = self._shadow().decide_route(
            "A", {"context_paths_count": 3}, fallback_model="claude-opus-4-7")
        self.assertEqual(d.route_candidate, "claude-opus-4-7")
        self.assertEqual(d.route_actual, "claude-opus-4-7")
        self.assertEqual(
            d.decision_basis["calibration_rationale"]["confidence_band"],
            "unsupported-shape")

    def test_no_leak_to_stage_b_and_c(self) -> None:
        for stage in ("B", "C"):
            d = self._shadow().decide_route(
                stage, {"context_paths_count": 0}, fallback_model="claude-opus-4-7")
            self.assertEqual(d.route_candidate, "claude-opus-4-7")
            self.assertEqual(d.route_actual, "claude-opus-4-7")
            self.assertNotIn("calibration_rationale", d.decision_basis)

    def test_placeholder_version_does_not_act_no_cross_version_leak(self) -> None:
        # The SAME calibration wired into a PHASE_1A_PLACEHOLDER policy must NOT
        # diverge — same inputs, no Haiku candidate (the rule is version-scoped).
        d = RoutingPolicy(
            PHASE_1A_PLACEHOLDER, calibration=_haiku_calibration()).decide_route(
            "A", {"context_paths_count": 0}, fallback_model="claude-opus-4-7")
        self.assertEqual(d.route_candidate, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(d.route_actual, PHASE_1A_PLACEHOLDER_MODEL)


if __name__ == "__main__":
    unittest.main()
