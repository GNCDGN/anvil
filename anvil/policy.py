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

    def __init__(self, policy_version: str) -> None:
        self.policy_version = policy_version

    def decide_route(
        self, stage: str, features, *, fallback_model: str | None = None
    ) -> RouteDecision:
        """Return a RouteDecision for `stage` given `features` (the merged
        lint + features_seen dict). Phase 1a: unconditionally the placeholder
        model, with route_actual == route_candidate (no fallback fired).

        Never raises. On any internal failure returns a degraded decision with
        route_fallback_fired=True and route_actual=fallback_model (the wrapper's
        per-stage attr) so route_actual stays honest about what the API runs on
        the fallback path; the brief's 2-arg call (no fallback_model) degrades to
        the placeholder model.
        """
        try:
            # deep-copy via dict() first so a non-dict `features` raises here
            # (caught below) rather than silently passing through.
            basis = copy.deepcopy(dict(features)) if features else {}
            candidate = PHASE_1A_PLACEHOLDER_MODEL
            return RouteDecision(
                route_candidate=candidate,
                route_actual=candidate,
                route_fallback_fired=False,
                decision_basis=basis,
            )
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
