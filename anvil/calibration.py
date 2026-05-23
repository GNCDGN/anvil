"""v3 Phase 1b Step 1 — routing calibration substrate (shadow-only).

`RoutingCalibration` consumes historical shadow-trace data to derive a Stage A
cheap-vs-Opus decision predicate, frozen (eagerly, at construction) into a
`CalibratedPolicy` callable. Shadow-only in Step 1: nothing in production
consumes the predicate yet (`RoutingPolicy.decide_route_with_calibration` wires
one in, but `decide_route` still returns the placeholder — consult-not-act).
Step 2 ships the rule that acts on it.

**Empirically gated (Step1B-F1).** The single-feature predicate
(`context_paths_count == 0 → cheap model`) ships its high-confidence
recommendation ONLY if every historical empty-context Stage A call also
returned zero paths (`paths_returned == 0`) — i.e. Opus selected nothing, so a
cheap model selecting nothing is provably equivalent. If even one empty-context
call selected a path, the whole feature degrades to uncalibrated (Opus +
`unsupported-shape` unconditionally). This is what makes the substrate a
*calibration*, not a hardcoded rule.

Derivation joins two event kinds on `(run_id, mode, step_idx)`:
  - `shadow_decisions` → `context_paths_count` (from the merged
    `shadow_decision_basis` JSON)
  - `planner.stage_a.parsed` → `paths_returned` (the actual Opus selection)

The brief's input-stream spec named `planner.stage_a.api_end`; the selection
actually lives on `planner.stage_a.parsed` (Step1B-F1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from anvil.policy import PHASE_1A_PLACEHOLDER_MODEL

log = logging.getLogger("anvil.calibration")

# Step1B-F2: the cheap Stage A model is pinned to the DATED ID, not the
# "claude-haiku-4-5" alias. Phase 1b's empirical grounding is validated against
# THIS model's behaviour (the empty-context → empty-selection equivalence); if
# Anthropic re-points the alias, the constant change forces an explicit
# calibration re-run rather than silently changing the routed model. Contrast
# DEFAULT_PLANNER_MODEL / PHASE_1A_PLACEHOLDER_MODEL = "claude-opus-4-7" (alias):
# Opus is the canonical reference, not an empirically-validated cheap route.
CHEAP_STAGE_A_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class RouteRecommendation:
    """A calibrated recommendation for a single Stage A call. Transient — feeds
    `decision_basis["calibration_rationale"]`; a plain @dataclass like
    RouteDecision. `confidence_band` reuses the lint vocabulary; Phase 1b emits
    only "high" or "unsupported-shape" ("medium" reserved, as in lint)."""

    recommended_model: str
    confidence_band: Literal["high", "medium", "unsupported-shape"]
    rationale: dict


class CalibratedPolicy:
    """A frozen calibrated predicate, produced by `RoutingCalibration`. Called
    per Stage A feature dict → `RouteRecommendation`. Stage-agnostic: it operates
    on features only; `RoutingPolicy.decide_route` owns the Stage-A gating.
    Never raises (degraded recommendation on internal failure)."""

    def __init__(self, predicate_state: dict) -> None:
        self.predicate_state = predicate_state

    def __call__(self, features) -> RouteRecommendation:
        try:
            ps = self.predicate_state
            if isinstance(features, dict):
                ctx = features.get("context_paths_count")
            else:
                ctx = getattr(features, "context_paths_count", None)
            if ctx == 0 and ps.get("empty_context_calibrated"):
                return RouteRecommendation(
                    recommended_model=CHEAP_STAGE_A_MODEL,
                    confidence_band="high",
                    rationale={
                        "feature": "context_paths_count",
                        "feature_value": 0,
                        "calibrated": True,
                        "n_empty_context_samples":
                            ps.get("n_empty_context_samples"),
                        "evidence": (
                            "all historical empty-context Stage A calls "
                            "returned 0 paths (Opus selected nothing)"
                        ),
                    },
                )
            return RouteRecommendation(
                recommended_model=PHASE_1A_PLACEHOLDER_MODEL,
                confidence_band="unsupported-shape",
                rationale={
                    "feature": "context_paths_count",
                    "feature_value": ctx,
                    "calibrated": False,
                    "reason": (
                        "no calibration data for this shape"
                        if ps.get("empty_context_calibrated")
                        else "empty-context feature not calibrated"
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            log.warning(
                f"[calibration] CalibratedPolicy failed "
                f"({type(exc).__name__}: {exc}); degrading to Opus"
            )
            return RouteRecommendation(
                recommended_model=PHASE_1A_PLACEHOLDER_MODEL,
                confidence_band="unsupported-shape",
                rationale={"error": str(exc)},
            )


# Join: shadow_decisions (context_paths_count) ⋈ planner.stage_a.parsed
# (paths_returned) on (run_id, mode, step_idx). Stage A only. Verified 1:1 on
# the Phase 1a exit-sweep corpus (18 rows, no fan-out).
_STAGE_A_JOIN_SQL = """
SELECT
    CAST(json_extract(sd.shadow_decision_basis, '$.context_paths_count') AS BIGINT)
        AS context_paths_count,
    CAST(json_extract(e.data, '$.paths_returned') AS BIGINT) AS paths_returned
FROM shadow_decisions sd
JOIN events e
  ON sd.run_id = e.run_id
 AND sd.mode = e.mode
 AND COALESCE(sd.step_idx, -1) = COALESCE(e.step_idx, -1)
WHERE sd.stage = 'A'
  AND e.kind = 'planner.stage_a.parsed'
"""


class RoutingCalibration:
    """Derives a `CalibratedPolicy` from historical Stage A shadow samples.

    Eager: the derivation runs once at construction and freezes into
    `self.policy`. Each sample is a dict with at least `context_paths_count` and
    `paths_returned` (the actual Opus selection). The predicate is empirically
    gated (Step1B-F1). Empty / malformed input → degraded predicate (Opus +
    unsupported-shape unconditionally)."""

    def __init__(self, samples: list[dict]) -> None:
        self.samples = list(samples or [])
        self.predicate_state = self._derive(self.samples)
        self.policy = CalibratedPolicy(self.predicate_state)

    @staticmethod
    def _derive(samples: list[dict]) -> dict:
        """Pure derivation: empirically gate the empty-context predicate. Ship
        the high-confidence Haiku recommendation only if there is at least one
        empty-context (context_paths_count == 0) Stage A sample AND every such
        sample selected zero paths (paths_returned == 0). Malformed samples
        (missing keys) are excluded from the gate rather than crashing it."""
        empty = [
            s for s in samples
            if isinstance(s, dict)
            and s.get("context_paths_count") == 0
            and s.get("paths_returned") is not None
        ]
        n_empty = len(empty)
        n_zero = sum(1 for s in empty if s.get("paths_returned") == 0)
        calibrated = n_empty > 0 and n_zero == n_empty
        return {
            "empty_context_calibrated": calibrated,
            "n_empty_context_samples": n_empty,
            "n_empty_context_zero_paths": n_zero,
        }

    @classmethod
    def from_connection(cls, con) -> "RoutingCalibration":
        """Extract Stage A samples from a DuckDB connection (joining
        `shadow_decisions` with `planner.stage_a.parsed`), then derive. Never
        raises — a query failure (e.g. a DB missing the tables) derives from an
        empty corpus, yielding a degraded predicate."""
        try:
            rows = con.execute(_STAGE_A_JOIN_SQL).fetchall()
            samples = [
                {"context_paths_count": r[0], "paths_returned": r[1]}
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            log.warning(
                f"[calibration] from_connection query failed "
                f"({type(exc).__name__}: {exc}); deriving from empty corpus"
            )
            samples = []
        return cls(samples)

    @classmethod
    def from_db(cls, path) -> "RoutingCalibration":
        """v3 Phase 1b Step 2: open a DuckDB at `path` read-only, extract Stage A
        samples, derive. Keeps callers (the orchestrator) from importing duckdb
        or the harness. Never raises — a missing file or DB error derives from an
        empty corpus (degraded predicate), so a misconfigured ANVIL_CALIBRATION_DB
        can never block a build."""
        try:
            import duckdb
            con = duckdb.connect(str(path), read_only=True)
            try:
                return cls.from_connection(con)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            log.warning(
                f"[calibration] from_db({path}) failed "
                f"({type(exc).__name__}: {exc}); deriving from empty corpus"
            )
            return cls([])
