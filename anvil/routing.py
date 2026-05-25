"""Routing — v4 Phase 1a Step 1: the model-selection seam (design
[[v4-planning/02-phase-1-design]] Q1; brief Step 1).

A single source of truth for "which model is which alias" and "give me a
client for this model name". This is the seam Phase 2/3 capabilities consume
for vision-economics cost-shaping (e.g. a browser-observation re-check
requesting `haiku`), and the seam Step 3 threads into the Planner Stage A/B
call sites.

The framing carried from the v4 design and the Phase 1 design §"Frame":
this is **operator-declared cost-shaping, not v3-style evidence-gated
equivalence routing**. There is no claim that one model produces output
equivalent to another for any task — the operator (via the brief's per-step
`model:` field, Step 2) names the model, and this seam routes the call. No
calibration corpus, no comparator, no silent_miss grading.

Step 1 ships two public surfaces in isolation — `MODEL_ALIASES` and
`client_for_model` — with no call sites consuming them yet (Step 3 wires
planner.py). `call_model_for_subtask` and its lightweight never-raises+retry
wrapper are Step 3 (brief Amendment 3), not here.

Note on the SDK: an `anthropic.Anthropic` client is model-agnostic — the
model is a per-call parameter on `messages.create`/`messages.stream`, not a
property of the client object. So `client_for_model` returns the shared
configured client; the *resolved model string* a caller passes at call time
comes from the resolver (`_resolve`, private in Step 1; Step 3 promotes it to
a public resolver when the call sites need it). Keeping client construction
here — rather than per-model client classes — mirrors planner.py and leaves
room for v5 evidence-gated routing to extend the module without touching
call sites.
"""
from __future__ import annotations

import logging
import os

import anthropic

# Q-A1 disposition (Step 0 notes Q-A1-F1): MODEL_RATES in tools/harness_v2.py
# is the existing single source of truth for "which models exist" (V3P1C-4).
# routing.py validates its alias targets against it at module load (below).
# Both anvil/ and tools/ are packages; tests and the orchestrator run from the
# repo root, so this import resolves cleanly. (Step 1 finding: this pulls
# harness_v2's module-level duckdb/openpyxl imports into routing's — and hence
# Step 3's planner — import graph; acceptable for Phase 1a, flagged for Step 2.)
from tools.harness_v2 import MODEL_RATES

log = logging.getLogger("anvil.routing")

# Canonical default when no model is selected. Opus, matching the Planner
# Stage B historical default (planner.py `DEFAULT_PLANNER_MODEL`) — a `None`
# or absent `model:` keeps current v3 behaviour.
DEFAULT_MODEL = "claude-opus-4-7"

# Alias name → version string. Per Q-A1, targets must all exist in
# MODEL_RATES. Sonnet is deliberately absent in Phase 1a (brief Amendment 1):
# `claude-sonnet-4-6` has no MODEL_RATES entry (v3 never used Sonnet) and no
# Phase 1a/2/3 consumer named it; the alias is restored in a later phase when
# a Sonnet rate and a consumer both exist.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5-20251001",
}

# Module-load invariant (V3P1C-4 mirror-invariant pattern): every alias target
# must be a known model in MODEL_RATES, or the seam is mis-wired. Assert at
# import so a future config-author who points an alias at an unregistered model
# fails loud at test time, not silently at runtime. `test_routing.py` exercises
# this same condition explicitly.
_unregistered_alias_targets = [
    target for target in MODEL_ALIASES.values() if target not in MODEL_RATES
]
assert not _unregistered_alias_targets, (
    "MODEL_ALIASES targets absent from MODEL_RATES "
    f"{sorted(MODEL_RATES)}: {_unregistered_alias_targets}. "
    "Register the model in tools/harness_v2.py MODEL_RATES, or fix the alias."
)

# Lazily-constructed, cached shared client. Constructed once on first use, not
# at import, so importing routing.py is side-effect-free beyond the invariant.
_default_client: anthropic.Anthropic | None = None


def _resolve(name: str | None) -> str:
    """Resolve an alias or version string to a known model version string.

    - `None` / empty → `DEFAULT_MODEL` (the v3 no-`model:` default).
    - alias (in `MODEL_ALIASES`) → its version-string target.
    - known version string (in `MODEL_RATES`) → itself.
    - anything else → `DEFAULT_MODEL` + a structured warning (never raises;
      V3P1C-4 `unknown_cost_models()` warn-and-fallback precedent).

    Private in Step 1; Step 3 promotes it to the public resolver the call
    sites use for the `model=` parameter.
    """
    if not name:  # None or ""
        return DEFAULT_MODEL
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    if name in MODEL_RATES:
        return name
    log.warning(
        "[routing] unknown model=%r — falling back to default=%r "
        "(known_aliases=%s known_versions=%s)",
        name, DEFAULT_MODEL, sorted(MODEL_ALIASES), sorted(MODEL_RATES),
    )
    return DEFAULT_MODEL


def _client() -> anthropic.Anthropic:
    """Return the cached shared Anthropic client, constructing it once.

    Mirrors planner.py's `anthropic.Anthropic(api_key=...)` construction. The
    key is read from the environment (the orchestrator loads `.env`).
    anthropic 0.102.0 constructs without raising even when the key is absent —
    no API call is made here — so the seam never raises at lookup time; a
    missing key surfaces only if/when a caller actually invokes the client
    (Step 3+).
    """
    global _default_client
    if _default_client is None:
        _default_client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY")
        )
    return _default_client


def client_for_model(name: str | None = None) -> anthropic.Anthropic:
    """Return a configured Anthropic client for the named model.

    `name` is an alias (`opus`, `haiku`), a bare version string
    (`claude-opus-4-7`, ...), or `None`/absent for the default. Unknown names
    fall back to the default with a structured warning — never raises (brief
    Step 1 criteria 1 + 3). The SDK client is model-agnostic, so the same
    configured client is returned for every name; the resolved model string a
    caller passes at call time comes from the resolver. Resolving here (rather
    than only at the call site) is what surfaces the unknown-name warning.
    """
    _resolve(name)  # validate + warn-on-unknown side effect; client is shared
    return _client()
