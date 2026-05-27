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

Public surfaces (Step 1 + Step 3): `MODEL_ALIASES`, `resolve_model`,
`client_for_model`, and `call_model_for_subtask`. Step 1 shipped the first two
data/resolution surfaces in isolation; Step 3 (Amendment 5) promotes the
resolver to public `resolve_model` (the planner reads it to thread a per-step
`model:` override into `_api_model`) and adds `call_model_for_subtask` — the
Phase 2/3 sub-task entry point with its own lightweight never-raises+retry
wrapper (Amendment 3).

Note on the SDK: an `anthropic.Anthropic` client is model-agnostic — the
model is a per-call parameter on `messages.create`/`messages.stream`, not a
property of the client object. So `client_for_model` returns the shared
configured client; the *resolved model string* a caller passes at call time
comes from `resolve_model`. The planner does NOT call `client_for_model` per
stage (Amendment 5) — it keeps its own configured client and threads the
resolved override through `_api_model`; `client_for_model` is consumed only by
`call_model_for_subtask` here. Keeping client construction here — rather than
per-model client classes — mirrors planner.py and leaves room for v5
evidence-gated routing to extend the module without touching call sites.
"""
from __future__ import annotations

import logging
import os
import time

import anthropic

# Q-A1 disposition (Step 0 notes Q-A1-F1): MODEL_RATES is the single source of
# truth for "which models exist" (V3P1C-4). routing.py validates its alias
# targets against it at module load (below). v4 Phase 1a housekeeping lifted
# MODEL_RATES from tools/harness_v2.py to anvil/events.py (the canonical
# per-model-data home), resolving the Step 1 finding: importing it here no
# longer pulls harness_v2's duckdb/openpyxl into routing's — or Step 3's
# planner — import graph.
from anvil.events import MODEL_RATES

log = logging.getLogger("anvil.routing")

# Canonical default when no model is selected. Opus, matching the Planner
# Stage B historical default (planner.py `DEFAULT_PLANNER_MODEL`) — a `None`
# or absent `model:` keeps current v3 behaviour.
DEFAULT_MODEL = "claude-opus-4-7"

# Alias name → version string. Per Q-A1, targets must all exist in
# MODEL_RATES. Sonnet was deliberately absent in Phase 1a (brief Amendment 1):
# `claude-sonnet-4-6` had no MODEL_RATES entry and no Phase 1a/2/3 consumer, so
# the alias was to be restored "when a Sonnet rate and a consumer both exist".
# v4 Phase 3a restores it (Step 0 Q-A5 / DC4): the rate now exists
# (events.MODEL_RATES, $3/$15 per Mtok) and the consumer is named — Phase 3c
# routes the screen-aware vision interpreter to Sonnet via call_model_for_subtask.
# Available-but-not-consumed in 3a (nothing routes "sonnet" until 3c), the v4
# substrate-ahead-of-consumer pattern.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
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
    "Register the model in anvil/events.py MODEL_RATES, or fix the alias."
)

# Lazily-constructed, cached shared client. Constructed once on first use, not
# at import, so importing routing.py is side-effect-free beyond the invariant.
_default_client: anthropic.Anthropic | None = None


# Unknown model names already warned this process — the warning fires once per
# distinct name per run (debouncing), so a hot loop requesting a stale model
# doesn't flood the log. Module-global (process-scoped), reset only on reimport.
_warned_unknown: set[str] = set()


def resolve_model(name: str | None) -> str:
    """Resolve an alias or version string to a known model version string.

    - `None` / empty → `DEFAULT_MODEL` (the v3 no-`model:` default).
    - alias (in `MODEL_ALIASES`) → its version-string target.
    - known version string (in `MODEL_RATES`) → itself.
    - anything else → `DEFAULT_MODEL` + a structured warning (never raises;
      V3P1C-4 `unknown_cost_models()` warn-and-fallback precedent). The warning
      fires once per distinct unknown name per process-run (debounced).

    Public since Step 3 (Amendment 5): the planner reads it to resolve a
    per-step `model:` override before threading it through `_api_model`, and
    `call_model_for_subtask` resolves its `model_name` arg through it.
    """
    if not name:  # None or ""
        return DEFAULT_MODEL
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    if name in MODEL_RATES:
        return name
    if name not in _warned_unknown:
        _warned_unknown.add(name)
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
    resolve_model(name)  # validate + warn-on-unknown side effect; client shared
    return _client()


# v4 Phase 1a Step 3 default cap for the sub-task entry point, matching
# planner.py's `_call_anthropic` max_tokens.
_SUBTASK_MAX_TOKENS = 8192


def call_model_for_subtask(
    model_name: str, system_prompt: str, user_message: str
) -> str:
    """Generic single-shot model call for Phase 2/3 internal sub-tasks (e.g. a
    browser-observation re-check, a vision-frame interpretation) — the seam's
    third public surface (Q-A6: lives here, not in planner.py).

    Resolves `model_name` via `resolve_model`, gets the shared client via
    `client_for_model`, calls the Anthropic SDK, and returns the concatenated
    assistant text.

    Lightweight never-raises+retry wrapper (Amendment 3): the SAME shape as
    planner.py's `_call_anthropic` — retry once on `APITimeoutError` /
    `RateLimitError` after `sleep(min(60, retry-after))`; a broad `Exception`
    is logged and returns a structured error — but WITHOUT importing it or its
    planner-internal coupling (stage routing, brief-block caching, event
    emission, instance `self._client`). Reference shape only; not imported.

    No brief-block caching (Q-A3): `system_prompt` is passed as a plain string
    with no `cache_control`; callers that need caching layer it on top.

    Returns the response text on success, or a grep-able structured error
    string on terminal failure (`"[call_model_for_subtask error: <reason>]"`)
    — never raises, so Phase 2/3 consumers can detect failure without a
    try/except.
    """
    resolved = resolve_model(model_name)
    client = client_for_model(model_name)

    def _attempt() -> str:
        resp = client.messages.create(
            model=resolved,
            max_tokens=_SUBTASK_MAX_TOKENS,
            system=system_prompt,  # plain string — no cache_control (Q-A3)
            messages=[{"role": "user", "content": user_message}],
        )
        return "".join(
            b.text for b in resp.content
            if getattr(b, "type", None) == "text"
        )

    def _retry_after(exc: Exception) -> int:
        resp = getattr(exc, "response", None)
        hdr = getattr(resp, "headers", None)
        try:
            return int(hdr.get("retry-after")) if hdr else 1
        except (TypeError, ValueError):
            return 1

    try:
        try:
            return _attempt()
        except (anthropic.APITimeoutError, anthropic.RateLimitError) as e:
            time.sleep(min(60, _retry_after(e)))
            return _attempt()
    except Exception as e:  # noqa: BLE001 — never-raise contract
        log.error(
            "[routing] call_model_for_subtask model=%r failed (%s); "
            "returning structured error", resolved, e,
        )
        return f"[call_model_for_subtask error: {str(e)[:300]}]"
