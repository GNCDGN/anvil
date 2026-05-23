"""v3 Phase 1a Step 2 — advisory brief-and-step lint with structured-feature output.

`lint_brief(brief) -> LintResult` runs in `Orchestrator.handle_brief` after
`validate_or_reject` and before the step loop. It is PURE ADVISORY: it never
mutates the brief, never auto-fixes, never gates execution. It emits two things:

- `advisory_warnings: list[str]` — human-readable warnings surfaced to Genco
  (same posture as `brief.parse_warnings`).
- `structured_features: dict` — a FLAT, machine-readable dict the Step 3 routing
  policy engine consumes. Step 3 merges it into Phase 0's `features_seen` with
  lint winning on key collision (e.g. `context_paths_count`), so the shape stays
  flat by design.

Never-raises: any failure inside a rule or the assembly returns a degraded
LintResult (a "lint failed" advisory + all seven structured_features at safe
defaults), so the lint can never tank a build — the same never-raise contract
every ANVIL component boundary holds.

confidence_band (Phase 1a): only "high" or "unsupported-shape". "medium" is
reserved for Phase 1b's calibration substrate, which will have the data to
ground a partial-confidence verdict; an invariant test asserts no Phase 1a path
emits "medium".
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from anvil import events as _events

log = logging.getLogger("anvil.lint")

# v2-canonical scope.operations set — mirrors brief._VALID_OPERATIONS (the rule-8
# validator). scope_operations_unsupported is DEFENSIVE (Step2-F1): a validated
# brief can never carry an op outside this set (validate_or_reject rule 8 already
# rejects them), so the rule is dead on the live path. Kept for lint calls on
# un-validated briefs (tests, future callers that bypass validation).
_V2_CANONICAL_OPS = {"read", "write", "smoke-test", "commit", "shell"}

# Operations the T5 deploy calibration actually exercised (subset of canonical;
# no "shell"). Part of the T5-shape predicate below.
_T5_STEP_OPS = {"read", "write", "smoke-test", "commit"}

_STEP_COUNT_HIGH = 6          # advisory threshold (step_count_high rule)
_STEP_COUNT_UNSUPPORTED = 10  # confidence_band escalation threshold

_BAND_UNSUPPORTED = "unsupported-shape"


class LintResult(BaseModel):
    """Advisory lint output. `advisory_warnings` is human-facing; `structured_features`
    is the flat machine-readable dict the routing policy engine consumes."""

    advisory_warnings: list[str] = Field(default_factory=list)
    structured_features: dict = Field(default_factory=dict)

    @property
    def confidence_band(self) -> str | None:
        """Convenience read of structured_features['confidence_band'] (the band
        lives inside structured_features per the brief's feature list)."""
        return self.structured_features.get("confidence_band")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _estimate_brief_token_count(brief) -> int:
    """chars/4 estimate of the brief's content, derived from STRUCTURED FIELDS.

    The Brief object discards the raw markdown body at parse time (Step2-F2), and
    lint_brief(brief) receives no path — so this reassembles a deterministic body
    string from goal + per-step fields + context_links and applies
    events._estimate_tokens (the same chars/4 heuristic used for candidate block
    sizing). An estimate-of-an-estimate, honestly named.
    """
    parts: list[str] = [getattr(brief, "goal", "") or ""]
    for s in getattr(brief, "steps", []) or []:
        parts.append(getattr(s, "name", "") or "")
        parts.append(getattr(s, "notes", "") or "")
        parts.extend(getattr(s, "scope_files", []) or [])
        parts.extend(getattr(s, "scope_operations", []) or [])
        parts.append(getattr(s, "smoke", "") or "")
    parts.extend(getattr(brief, "context_links", []) or [])
    body = "\n".join(parts)
    return _events._estimate_tokens(body)


def _is_t5_shape(brief) -> bool:
    """True iff the brief matches the T5 deploy calibration shape exactly.

    T5 is the ONLY deploy shape v2/v3 calibrated (V2P5-1, V3P0 data): exactly 3
    auto-confirm steps, write/smoke-test ops, no end_to_end_test. Any deviation
    in ANY direction means the lint has no calibrated basis to estimate this
    deploy chain — deploy_chain_unsupported_shape fires and escalates the band.
    """
    steps = getattr(brief, "steps", []) or []
    return (
        getattr(brief, "vps_deploy", "no") == "yes"
        and getattr(brief, "end_to_end_test", None) is None
        and len(steps) == 3
        and all(
            set(getattr(s, "scope_operations", []) or []) <= _T5_STEP_OPS
            for s in steps
        )
        and all(getattr(s, "confirm", None) == "auto" for s in steps)
    )


# ---------------------------------------------------------------------------
# Rules — each returns (advisory_warnings, structured_feature_fragment).
# A fragment may escalate confidence_band to "unsupported-shape"; it never
# downgrades. Most rules contribute warnings only (empty fragment).
# ---------------------------------------------------------------------------

def _rule_scope_files_likely_dont_exist(brief) -> tuple[list[str], dict]:
    """Consume brief.parse_warnings (path-not-found, computed in brief.py) and
    augment each with a per-warning severity band: closest_match present →
    'may-reconcile' (the Coder may resolve it at execute time); absent →
    'high-confidence-missing'. (Distinct from the overall confidence_band.)"""
    warnings: list[str] = []
    for w in getattr(brief, "parse_warnings", []) or []:
        if not isinstance(w, dict) or w.get("kind") != "path-not-found":
            continue
        cm = w.get("closest_match")
        band = "may-reconcile" if cm else "high-confidence-missing"
        cm_text = f"'{cm}'" if cm else "none"
        warnings.append(
            f"scope.files: step {w.get('step_number')} path "
            f"'{w.get('path')}' likely does not exist "
            f"[{band}; closest match: {cm_text}]"
        )
    return warnings, {}


def _rule_confirm_explicit_density(brief) -> tuple[list[str], dict]:
    """Fire when EVERY step is confirm:explicit (100% — brief-literal). Signals a
    human pause at every step (high interaction count)."""
    steps = getattr(brief, "steps", []) or []
    warnings: list[str] = []
    if steps and all(getattr(s, "confirm", None) == "explicit" for s in steps):
        warnings.append(
            f"confirm density: all {len(steps)} steps are confirm:explicit "
            "— expect a human pause at every step."
        )
    return warnings, {}


def _rule_deploy_chain_unsupported_shape(brief) -> tuple[list[str], dict]:
    """Fire when a deploy brief deviates from the T5 calibration shape in ANY
    direction. T5 is the only calibrated deploy shape; escalates the overall
    confidence_band to unsupported-shape."""
    warnings: list[str] = []
    features: dict = {}
    if getattr(brief, "vps_deploy", "no") == "yes" and not _is_t5_shape(brief):
        warnings.append(
            "deploy chain: vps_deploy=yes but the brief does not match the T5 "
            "calibration shape (exactly 3 auto-confirm steps, write/smoke-test "
            "ops, no end_to_end_test) — the lint has no calibrated basis to "
            "estimate this deploy chain."
        )
        features["confidence_band"] = _BAND_UNSUPPORTED
    return warnings, features


def _rule_step_count_high(brief) -> tuple[list[str], dict]:
    """Advisory at >6 steps (outside the T1–T6 corpus, which are ≤3 steps);
    escalates the band to unsupported-shape at >10 steps."""
    steps = getattr(brief, "steps", []) or []
    n = len(steps)
    warnings: list[str] = []
    features: dict = {}
    if n > _STEP_COUNT_HIGH:
        warnings.append(
            f"step count: {n} steps (>{_STEP_COUNT_HIGH}) is outside the "
            "calibrated corpus (T1–T6 are ≤3 steps)."
        )
    if n > _STEP_COUNT_UNSUPPORTED:
        features["confidence_band"] = _BAND_UNSUPPORTED
    return warnings, features


def _rule_scope_operations_unsupported(brief) -> tuple[list[str], dict]:
    """DEFENSIVE (Step2-F1): flag any scope.operations value outside the
    v2-canonical set. validate_or_reject rule 8 already rejects these, so this
    never fires on a validated brief; kept for lint calls on un-validated briefs.
    Escalates the band to unsupported-shape if it ever does fire."""
    warnings: list[str] = []
    features: dict = {}
    bad: set[str] = set()
    for s in getattr(brief, "steps", []) or []:
        bad |= set(getattr(s, "scope_operations", []) or []) - _V2_CANONICAL_OPS
    if bad:
        warnings.append(
            f"scope.operations: unsupported operation(s) {sorted(bad)} "
            f"outside the v2-canonical set {sorted(_V2_CANONICAL_OPS)}."
        )
        features["confidence_band"] = _BAND_UNSUPPORTED
    return warnings, features


_RULES = (
    _rule_scope_files_likely_dont_exist,
    _rule_confirm_explicit_density,
    _rule_deploy_chain_unsupported_shape,
    _rule_step_count_high,
    _rule_scope_operations_unsupported,
)


def _safe_default_features() -> dict:
    """All seven structured_features at safe defaults — the degraded never-raise
    result, so Step 3's policy engine always finds the named keys."""
    return {
        "brief_token_estimate": 0,
        "step_count": 0,
        "total_scope_files": 0,
        "has_vps_deploy": False,
        "has_end_to_end_test": False,
        "context_paths_count": 0,
        "confidence_band": _BAND_UNSUPPORTED,
    }


def lint_brief(brief) -> LintResult:
    """Advisory lint of a Brief. Never raises (degraded result on failure).

    Returns a LintResult with advisory_warnings (human-facing) and a FLAT
    structured_features dict (machine-readable). PURE advisory: does not mutate
    the brief, auto-fix, or gate execution.
    """
    try:
        steps = getattr(brief, "steps", []) or []
        features: dict = {
            "brief_token_estimate": _estimate_brief_token_count(brief),
            "step_count": len(steps),
            "total_scope_files": sum(
                len(getattr(s, "scope_files", []) or []) for s in steps
            ),
            "has_vps_deploy": getattr(brief, "vps_deploy", "no") == "yes",
            "has_end_to_end_test": (
                getattr(brief, "end_to_end_test", None) is not None
            ),
            "context_paths_count": len(getattr(brief, "context_paths", []) or []),
            # Default band; rules may escalate to unsupported-shape. Phase 1a
            # never emits "medium" (reserved for Phase 1b calibration).
            "confidence_band": "high",
        }
        warnings: list[str] = []
        for rule in _RULES:
            w, frag = rule(brief)
            warnings.extend(w)
            if frag.get("confidence_band") == _BAND_UNSUPPORTED:
                features["confidence_band"] = _BAND_UNSUPPORTED
            for k, v in frag.items():
                # confidence_band handled above (escalate-only); merge any other
                # forward-added fragment keys verbatim.
                if k != "confidence_band":
                    features[k] = v
        return LintResult(advisory_warnings=warnings, structured_features=features)
    except Exception as e:  # noqa: BLE001 — never-raise contract
        log.warning(
            f"[lint] lint_brief failed ({type(e).__name__}: {e}); "
            "returning degraded result"
        )
        return LintResult(
            advisory_warnings=[f"lint failed: {e}"],
            structured_features=_safe_default_features(),
        )
