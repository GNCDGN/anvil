"""v3 Phase 1a Step 3 — routing policy engine shell (shadow mode, placeholder rule).

`RoutingPolicy.decide_route(stage, features) -> RouteDecision`. Phase 1a ships ONE
policy: `PHASE_1A_PLACEHOLDER`, returning `route_candidate == route_actual ==
PHASE_1A_PLACEHOLDER_MODEL` ("claude-opus-4-7") unconditionally — the same route
the existing code already chose. The point is to prove the engine SHELL works;
Phase 1b lands the first real rule (cheap-route Stage A) on this shell without
shipping engine + rule + infrastructure in one commit.

Wiring (planner._call_anthropic): the API call kwarg and the `model` data field
stay on `_model_for_stage(stage)` — **"model" = what the API actually ran**. Only
`route_actual` (and `route_candidate` / `route_fallback_fired` / `policy_version`)
source from the policy decision — **"route_actual" = what the router DECIDED**. In
Phase 1a these coincide on a normal sweep (per-stage default == placeholder); they
diverge only under a deliberate per-stage override (tests) or Phase 1b's first
real rule (Step3-F1).

Never-raises: `decide_route` wraps its body; on any internal failure it returns a
degraded RouteDecision with `route_fallback_fired=True` and `route_actual=
fallback_model` (the wrapper passes its per-stage attr), so a policy bug can never
tank a build and `route_actual` stays honest about what the API will run.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass

log = logging.getLogger("anvil.policy")

# Phase 1a's policy_version stamp. Distinct from events.POLICY_VERSION_PHASE_0
# ("v3-phase-0-passive"); the champion_challenger_comparison view groups by this,
# so the two generations aggregate as separate rows.
PHASE_1A_PLACEHOLDER = "v3-phase-1a-placeholder"

# v3 Phase 1b Step 2: the first active routing rule's policy_version. Under this
# version, decide_route consults the wired calibration for Stage A and lets
# route_candidate diverge to the cheap model — but route_actual stays the
# per-stage model (the API call is unchanged; shadow-only). The canary
# (Step 3) adds a third version that also changes route_actual + the API call.
PHASE_1B_STAGE_A_SHADOW = "v3-phase-1b-stage-a-shadow"

# v3 Phase 1b Step 3: the canary policy_version. Unlike the shadow rule, the
# canary ACTS — for an allowlisted task's empty-context Stage A, both
# route_candidate AND route_actual become the cheap model, so the wrapper's
# API call (which sources from decision.route_actual) actually runs the cheap
# model. champion_challenger agreement is 100% by design (candidate == actual);
# the canary's real signal is silent_miss == 0 (the parallel-Opus comparator),
# not disagreement (Step3B-F4).
PHASE_1B_STAGE_A_CANARY = "v3-phase-1b-stage-a-canary"

# The route the placeholder returns unconditionally. Deliberately a SEPARATE
# constant from planner.DEFAULT_PLANNER_MODEL and events.SHADOW_ROUTE_PHASE_0
# (all "claude-opus-4-7" today): the policy's placeholder route, the planner's
# construction default, and the Phase 0 shadow route are distinct concepts that
# diverge once Phase 1b lands real rules (Step1-F2 reasoning).
PHASE_1A_PLACEHOLDER_MODEL = "claude-opus-4-7"


@dataclass
class RouteDecision:
    """A transient routing decision. Feeds event-data dicts (route_candidate,
    route_actual, route_fallback_fired) and the shadow row's decision_basis;
    never persisted in State, so a plain @dataclass (cf. the pydantic LintResult,
    which embeds in State). decision_basis is a deep copy of the features that
    drove the candidate — an audit record, never the live merge dict."""

    route_candidate: str
    route_actual: str
    route_fallback_fired: bool
    decision_basis: dict


class RoutingPolicy:
    """Routing policy engine shell. Phase 1a's only policy is the placeholder:
    `decide_route` returns `route_candidate == route_actual ==
    PHASE_1A_PLACEHOLDER_MODEL` regardless of features. `policy_version` is
    stamped onto every routing event and shadow_decisions row the wrapper emits."""

    def __init__(self, policy_version: str, *, calibration=None) -> None:
        self.policy_version = policy_version
        # v3 Phase 1b Step 1: an optional CalibratedPolicy (from
        # anvil.calibration). Stashed as a dependency; NOT imported here (the
        # cycle calibration→policy already exists for PHASE_1A_PLACEHOLDER_MODEL,
        # so policy must not import calibration). None → identical to Phase 1a.
        self.calibration = calibration

    def decide_route_with_calibration(self, calibration) -> "RoutingPolicy":
        """v3 Phase 1b Step 1 (criterion 7): return a RoutingPolicy with the
        given CalibratedPolicy wired in. A factory despite the verb-y name (the
        criterion mandates the name). In Step 1 the returned policy still emits
        PHASE_1A_PLACEHOLDER-equivalent decisions — `decide_route` consults the
        calibration and records its recommendation, but does not act on it.
        Step 2 ships the rule that sources `route_candidate` from it."""
        return RoutingPolicy(self.policy_version, calibration=calibration)

    def decide_route(
        self, stage: str, features, *, fallback_model: str | None = None
    ) -> RouteDecision:
        """Return a RouteDecision for `stage` given `features` (merged lint +
        features_seen). Dispatches by `policy_version`:

        - PHASE_1A_PLACEHOLDER: route_candidate == route_actual ==
          PHASE_1A_PLACEHOLDER_MODEL. A wired calibration is consulted on
          Stage A (recommendation recorded under
          `decision_basis["calibration_rationale"]`) but never acted on —
          consult-not-act.
        - PHASE_1B_STAGE_A_SHADOW (v3 Phase 1b Step 2): route_actual stays the
          per-stage model (`fallback_model` — what the API runs); only
          route_candidate may diverge. For empty-context Stage A with a
          high-confidence cheap recommendation, route_candidate becomes the
          cheap model (the shadow divergence; Step3-F1 inversion preserved —
          the API call is unchanged). A1.

        Never raises. On internal failure → degraded decision with
        route_fallback_fired=True and route_actual=fallback_model (the wrapper's
        per-stage attr), so route_actual stays honest about what the API runs;
        the 2-arg call (no fallback_model) degrades to the placeholder model.
        """
        try:
            # deep-copy via dict() first so a non-dict `features` raises here
            # (caught below) rather than silently passing through.
            basis = copy.deepcopy(dict(features)) if features else {}
            if self.policy_version == PHASE_1B_STAGE_A_CANARY:
                return self._decide_stage_a_canary(
                    stage, features, basis, fallback_model
                )
            if self.policy_version == PHASE_1B_STAGE_A_SHADOW:
                return self._decide_stage_a_shadow(
                    stage, features, basis, fallback_model
                )
            return self._decide_placeholder(stage, features, basis)
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            log.warning(
                f"[policy] decide_route failed for stage={stage} "
                f"({type(exc).__name__}: {exc}); falling back to per-stage model"
            )
            safe = fallback_model or PHASE_1A_PLACEHOLDER_MODEL
            return RouteDecision(
                route_candidate=safe,
                route_actual=safe,
                route_fallback_fired=True,
                decision_basis={"error": str(exc)},
            )

    def _decide_placeholder(self, stage, features, basis) -> RouteDecision:
        """PHASE_1A_PLACEHOLDER: unconditional placeholder model. A wired
        calibration is consulted on Stage A (rationale recorded) but never acted
        on — the placeholder is always returned (consult-not-act)."""
        candidate = PHASE_1A_PLACEHOLDER_MODEL
        if self.calibration is not None and stage == "A":
            basis["calibration_rationale"] = self._rationale(
                self.calibration(features)
            )
        return RouteDecision(
            route_candidate=candidate,
            route_actual=candidate,
            route_fallback_fired=False,
            decision_basis=basis,
        )

    def _decide_stage_a_shadow(
        self, stage, features, basis, fallback_model
    ) -> RouteDecision:
        """PHASE_1B_STAGE_A_SHADOW: route_actual is always the per-stage model
        (fallback_model — what the API runs); only route_candidate may diverge.
        For Stage A with a wired calibration recommending a high-confidence cheap
        model, route_candidate becomes that cheap model (the shadow divergence).
        All other cases (non-Stage-A, no calibration, uncalibrated/degraded
        recommendation): candidate == actual == per-stage model (no leak)."""
        actual = fallback_model or PHASE_1A_PLACEHOLDER_MODEL
        candidate = actual
        if stage == "A" and self.calibration is not None:
            rec = self.calibration(features)
            basis["calibration_rationale"] = self._rationale(rec)
            if rec.confidence_band == "high" and rec.recommended_model != actual:
                candidate = rec.recommended_model
        return RouteDecision(
            route_candidate=candidate,
            route_actual=actual,
            route_fallback_fired=False,
            decision_basis=basis,
        )

    def _decide_stage_a_canary(
        self, stage, features, basis, fallback_model
    ) -> RouteDecision:
        """PHASE_1B_STAGE_A_CANARY: the canary ACTS. For Stage A with a wired
        calibration recommending a high-confidence cheap model, BOTH
        route_candidate AND route_actual become that cheap model — so the
        wrapper's API call (sourced from route_actual) actually runs it. All
        other cases (non-Stage-A, no calibration, uncalibrated): candidate ==
        actual == per-stage model (no leak — only allowlisted canary tasks
        reach this policy_version, and only empty-context Stage A acts)."""
        base = fallback_model or PHASE_1A_PLACEHOLDER_MODEL
        candidate = actual = base
        if stage == "A" and self.calibration is not None:
            rec = self.calibration(features)
            basis["calibration_rationale"] = self._rationale(rec)
            if rec.confidence_band == "high" and rec.recommended_model != base:
                candidate = actual = rec.recommended_model  # canary acts
        return RouteDecision(
            route_candidate=candidate,
            route_actual=actual,
            route_fallback_fired=False,
            decision_basis=basis,
        )

    @staticmethod
    def _rationale(rec) -> dict:
        """Pack a RouteRecommendation into the decision_basis audit record."""
        return {
            "rationale": rec.rationale,
            "recommended_model": rec.recommended_model,
            "confidence_band": rec.confidence_band,
        }
