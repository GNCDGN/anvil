"""Structured-event emission layer (v2 Phase 1 Step 1).

The observability seam Steps 2–3 wire into Planner, Coder, Orchestrator,
state, git_ops, ssh_ops, and telegram. Every event lands as one JSON line
in `<ANVIL_ROOT>/state/runs/<run_id>/events.jsonl`. The Step 4 harness
reads that JSONL into DuckDB; the calibration sweep (Step 7) drives the
event volume; Step 8 grades against the captured artefacts.

Design pillars:

- **Module-global run state.** `_run_id`, `_anchor_monotonic`, `_drop_count`
  are module-level. `begin_run(run_id)` sets the first two and emits
  `run.start`; `end_run()` emits `run.end` and resets all three. Reading
  `current_run_id()` returns the sentinel `"unknown-run"` if no run is
  active — emits before `begin_run` land cleanly under that sentinel
  (notes.md Finding 3 decision).
- **Never-raises contract.** `emit` catches OSError, UnicodeError, and
  generic Exception; on failure it increments `_drop_count` and returns
  False. No emit failure ever propagates to the caller — instrumentation
  is best-effort, never load-bearing.
- **`_real_write` + `_real_append` captures.** `Path.write_text` and a
  module-level `_real_append` helper (append-mode `open`) are captured
  at module import time, before any code uses them. Production code
  uses `_real_append` via the `_append_line` helper for the O(1)
  emit hot path; `_real_write` is retained for any future caller
  needing atomic-replace semantics. Tests patch
  `anvil.events._real_append` (and `_real_write` for whole-file
  callers) to inject failure modes. Same shape as
  `anvil/vault_ops.py:29` and `anvil/ssh_ops.py:17`.
- **Validated kind catalogue.** `VALID_KINDS` is a frozenset of 45 dotted
  event kinds (notes.md Finding 1 + brief Step 1 spec; the brief
  estimated 33, the actual derivation lands at 45 once Stage A/B
  sub-events, retry pairs, and the four-stage SSH chain are enumerated).
  Emits with unknown kinds are logged once and dropped — they increment
  `_drop_count` but do not raise.
- **Minimal log noise.** No per-emit INFO line (would dwarf signal in
  `anvil.log`). One `[events] begin_run …` at `begin_run`, one
  `[events] end_run … drops=<n>` at `end_run`. That is the entire INFO
  footprint of this module.

The Stage C `planner.stage_c.api_end` kind covers `draft_completion_artefacts`
per notes.md Finding 1 constraint 2 (the third invocation site of
`_call_anthropic`). Stage C has no separate `api_start` kind — the wrapper
signature stays stable and the caller-side `api_start` emit only matters
for Stage A/B where prompt_chars varies between calls; Stage C reuses
the artefact-prompt verbatim.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

# Module-scope captures — production uses these via the helpers below;
# tests patch the attributes to inject failure modes. Captured before
# any code below uses them directly. Mirrors vault_ops._real_write
# (line 29) and ssh_ops._real_run (line 17).
#
# `_real_write` is the kept-for-back-compat handle (whole-file rewrite
# semantics, exposed for any future emit that needs atomic replace).
# `_real_append` is the hot path: append one line at a time, O(1) per
# emit, used by `_append_line`. Step 1 used read-modify-write because
# only `_real_write` existed; Step 2 prep added the second capture so
# Step 7's calibration sweep doesn't pay quadratic bytes in event count.
_real_write = Path.write_text


def _real_append(path: Path, text: str) -> None:
    """Append `text` to `path` in O(1) per call (no read-modify-write).

    Production code uses this via `_append_line`. Tests patch
    `anvil.events._real_append` to inject IOError shapes that the
    read-modify-write path could not naturally exercise.
    """
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)

log = logging.getLogger("anvil.events")

# ---------------------------------------------------------------------------
# Event-kind catalogue (50 kinds)
# ---------------------------------------------------------------------------

VALID_KINDS: frozenset[str] = frozenset({
    # Run lifecycle (3)
    "run.start", "run.end", "run.resume",
    # Brief (2)
    "brief.parsed", "brief.validated",
    # Step loop (2)
    "step.start", "step.end",
    # Planner Stage A (5)
    "planner.stage_a.start",
    "planner.stage_a.prompt_assembled",
    "planner.stage_a.api_start",
    "planner.stage_a.api_end",
    "planner.stage_a.parsed",
    # Planner Stage B (6)
    "planner.stage_b.start",
    "planner.stage_b.files_loaded",
    "planner.stage_b.prompt_assembled",
    "planner.stage_b.api_start",
    "planner.stage_b.api_end",
    "planner.stage_b.parsed",
    # Planner Stage C (1) — draft_completion_artefacts
    "planner.stage_c.api_end",
    # Planner validation + retry + escalation (5)
    "planner.validation.pass",
    "planner.validation.fail",
    "planner.retry.start",
    "planner.retry.end",
    "planner.escalate",
    # Coder (6)
    "coder.preflight.start",
    "coder.preflight.reconciled",
    "coder.preflight.escalate",
    "coder.subprocess.start",
    "coder.subprocess.end",
    "coder.scope_verify",
    # Smoke (2)
    "smoke.start", "smoke.end",
    # Git (4)
    "git.commit.start", "git.commit.end",
    "git.push.start", "git.push.end",
    # SSH/Deploy (2 — one pair, stages distinguished by `data.stage`)
    "ssh.stage.start", "ssh.stage.end",
    # Telegram (4)
    "telegram.send.start", "telegram.send.end",
    "telegram.poll.start", "telegram.poll.reply",
    # State (1)
    "state.write",
    # Escalation (2)
    "escalation.raised", "escalation.resolved",
    # v3 Phase 0 Step 2 (V3P0-3): shadow-decision recorder (1)
    "shadow.decision",
    # v3 Phase 0 Step 3 (V3P0-4 / V3P0-5): silent-miss comparator
    # scaffolding + Stage A parser-drop telemetry (4)
    "stage_a.shadow_compare.begin",
    "stage_a.shadow_compare.end",
    "stage_a.silent_miss.detected",
    "stage_a.parser_drop",
    # v3 Phase 1b Step 3 (V3P1B-3): the parallel Opus baseline call on a
    # canary Stage A call — its own cost-bearing api_end kind so it doesn't
    # double-count the primary Stage A call or perturb the shadow 1:1
    # invariant. First v3 VALID_KINDS bump (Step3B-F1). (1)
    "planner.stage_a.canary_baseline.api_end",
})
assert len(VALID_KINDS) == 51, f"VALID_KINDS count drift: {len(VALID_KINDS)}"

# v3 Phase 1b Step 3: the canary baseline event kind, referenced by the Planner
# emit + the harness operations view (so the baseline's cost is ledgered).
CANARY_BASELINE_KIND = "planner.stage_a.canary_baseline.api_end"

# ---------------------------------------------------------------------------
# v3 Phase 0 Step 1 — routing observability (V3P0-1)
#
# Five additive fields recorded on the four model-call event kinds
# (planner.stage_{a,b,c}.api_end, coder.subprocess.end) so Phase 1's
# active routing can be graded candidate-vs-actual. Phase 0 ships NO
# routing logic: route_candidate always equals route_actual, no fallback
# ever fires, and policy_version is the literal passive placeholder. The
# fields are recorded, never acted on. `routing_observability()` is the
# single shared producer — planner.py, mocked.py, and coder.py all import
# events, so the shape lives here once rather than duplicated per site.
# ---------------------------------------------------------------------------

POLICY_VERSION_PHASE_0 = "v3-phase-0-passive"


def _compute_features_seen(
    stage: str,
    step_idx: int | None,
    observed_prompt_token_count: int | None,
    context_paths_count: int | None,
) -> dict[str, Any]:
    """The feature inputs a Phase 1 policy engine would consume.

    Phase 0 records them; nothing reads them yet. All four keys are
    always present (None/0 fallbacks where a value is unavailable, e.g.
    a Planner error path with no usage), so the structural "contains at
    minimum the four named features" check holds on every row.
    """
    return {
        "observed_prompt_token_count": observed_prompt_token_count,
        "step_idx": step_idx,
        "stage": stage,
        "context_paths_count": context_paths_count,
    }


def routing_observability(
    *,
    stage: str,
    step_idx: int | None,
    observed_prompt_token_count: int | None,
    context_paths_count: int | None,
    route_actual: str | None,
    route_candidate: str | None = None,
    route_fallback_fired: bool = False,
    policy_version: str | None = None,
) -> dict[str, Any]:
    """Return the five v3 Phase 0 routing-observability fields, ready to
    merge into an event's `data` payload.

    Phase 0 / back-compat callers (e.g. coder.py) pass only `route_actual`:
    `route_candidate` mirrors it, `route_fallback_fired` is False, and
    `policy_version` is the Phase 0 placeholder.

    v3 Phase 1a Step 3: the Planner wrapper now passes `route_candidate`,
    `route_fallback_fired`, and `policy_version` from the RoutingPolicy
    decision, so `route_actual` reflects what the router DECIDED while the
    `model` data field reflects what the API RAN (Step3-F1).
    """
    return {
        "route_candidate": (
            route_candidate if route_candidate is not None else route_actual
        ),
        "route_actual": route_actual,
        "route_fallback_fired": route_fallback_fired,
        "policy_version": (
            policy_version if policy_version is not None else POLICY_VERSION_PHASE_0
        ),
        "features_seen": _compute_features_seen(
            stage, step_idx, observed_prompt_token_count, context_paths_count
        ),
    }


# ---------------------------------------------------------------------------
# v3 Phase 0 Step 2 — shadow-decision recorder (V3P0-3)
#
# Per Planner call, record what a hypothetical shadow router WOULD have
# decided alongside what the code actually did. Phase 0 ships exactly one
# placeholder rule: the shadow always picks Opus, so it always agrees with
# reality — the point is to prove the recording mechanism works, not to
# make a real decision. Phase 1 lands the first real Stage A rule inside
# `_compute_shadow_decision`. The emit fires immediately after each
# `planner.stage_X.api_end`, sharing that event's `features_seen` dict as
# the decision basis and its `route_actual` as the actual route taken.
# ---------------------------------------------------------------------------

SHADOW_ROUTE_PHASE_0 = "claude-opus-4-7"


def _compute_shadow_decision(features_seen: dict[str, Any]) -> str:
    """Phase 0 placeholder shadow rule: unconditionally Opus.

    `features_seen` is accepted but ignored in Phase 0 — Phase 1's first
    real Stage A rule reads it (prompt token count, context_paths_count,
    stage) to decide whether a cheaper route would have sufficed. Keeping
    the signature feature-aware now means Phase 1 changes only this body.
    """
    return SHADOW_ROUTE_PHASE_0


def emit_shadow_decision(
    *,
    stage: str,
    step_idx: int | None,
    features_seen: dict[str, Any],
    actual_route_taken: str | None,
    shadow_route_candidate: str | None = None,
    policy_version: str | None = None,
) -> bool:
    """Emit one `shadow.decision` event pairing the shadow router's
    candidate against the actual route taken.

    `agreement` is `shadow_route_candidate == actual_route_taken`. Phase 0
    callers pass neither override: the candidate comes from the Phase 0
    placeholder rule and `policy_version` defaults to the Phase 0 stamp.

    v3 Phase 1a Step 3: the Planner wrapper passes `shadow_route_candidate`
    (the RoutingPolicy's candidate) and `policy_version` (the policy's
    version), and supplies the merged decision_basis as `features_seen`. The
    ingest reads `policy_version` into the shadow_decisions row (criterion 4);
    Phase 0 events without it fall to the column DEFAULT.
    """
    candidate = (
        shadow_route_candidate if shadow_route_candidate is not None
        else _compute_shadow_decision(features_seen)
    )
    data: dict[str, Any] = {
        "stage": stage,
        "shadow_route_candidate": candidate,
        "shadow_decision_basis": features_seen,
        "actual_route_taken": actual_route_taken,
        "agreement": candidate == actual_route_taken,
    }
    if policy_version is not None:
        data["policy_version"] = policy_version
    return emit("shadow.decision", data, step_idx=step_idx)


# ---------------------------------------------------------------------------
# v3 Phase 0 Step 3 — silent-miss comparator scaffolding (V3P0-4)
#
# A comparator Phase 1 will use to detect silent under-contexting in
# cheap-routed Stage A: it compares the paths a (possibly cheap) route
# selected against a hypothetical baseline (Opus) selection. Phase 0
# wires the path but feeds it the same selection on both sides (routed ==
# baseline), so silent_miss_count is always 0 and jaccard is always 1.0
# by construction. `stage_a.silent_miss.detected` never fires in Phase 0
# (the path exists, guarded by silent_miss_count > 0). Phase 1 lands the
# first real cheap-vs-Opus comparison by feeding distinct selections.
# ---------------------------------------------------------------------------


def compare_stage_a_selections(
    routed_paths: list[str], baseline_paths: list[str]
) -> dict[str, Any]:
    """Compare a routed Stage A selection against a baseline selection.

    Returns:
      silent_miss_count    — paths in baseline but NOT routed (the danger:
                             context the cheap route silently dropped)
      hallucination_count  — paths in routed but NOT baseline
      jaccard_similarity   — |intersection| / |union|; 1.0 for two empty
                             selections (identical → perfect agreement)
      baseline_only_paths  — sorted list behind silent_miss_count
      routed_only_paths    — sorted list behind hallucination_count

    Pure function: no emit, no I/O. Order-independent (set semantics);
    duplicates within a side collapse.
    """
    routed = set(routed_paths or [])
    baseline = set(baseline_paths or [])
    baseline_only = sorted(baseline - routed)
    routed_only = sorted(routed - baseline)
    union = routed | baseline
    jaccard = 1.0 if not union else len(routed & baseline) / len(union)
    return {
        "silent_miss_count": len(baseline_only),
        "hallucination_count": len(routed_only),
        "jaccard_similarity": jaccard,
        "baseline_only_paths": baseline_only,
        "routed_only_paths": routed_only,
    }


# v3 Phase 2d Step 2 (Step3C-F1 hardening): the binary "silent_miss_count > 0"
# gate is hardened into four explicit dispositions (Q-D4 hybrid heuristic:
# per-row N=1, per-corpus K=2). The empty-context pass that Phase 1c-2c recorded
# implicitly as silent_miss == 0 is now recorded explicitly as `vacuous-empty`,
# and a silent_miss episode fires only on a `genuine-mismatch`. On T1-T6's
# uniformly empty-context corpus every row is vacuous-empty and
# silent_miss_episodes stays 0 (observationally identical to Phase 1c/2a/2b/2c);
# the genuine/uniform branches are forward-readiness for Phase 2d2's extended
# corpus, exercised by synthetic-corpus tests, not this build's sweep.
DISPOSITION_VACUOUS_EMPTY = "vacuous-empty"
DISPOSITION_VACUOUS_UNIFORM = "vacuous-uniform"
DISPOSITION_GENUINE_MATCH = "genuine-match"
DISPOSITION_GENUINE_MISMATCH = "genuine-mismatch"
DISPOSITION_UNKNOWN = "unknown"


def classify_comparator_disposition(
    comparator_result: dict[str, Any],
    baseline_paths: list[str],
    *,
    corpus_baselines: list[list[str]] | None = None,
    n: int = 1,
    k: int = 2,
) -> str:
    """Classify a Stage A comparison into one of four dispositions.

    Precedence (per-row before per-corpus — the cheaper check short-circuits):
      1. vacuous-empty   — per-row: the baseline holds fewer than N=1 distinct
         paths (an empty baseline). The canary cannot be graded against
         nothing → pass. Returned BEFORE the per-corpus check is evaluated.
      2. vacuous-uniform — per-corpus: across the baseline rows visible at call
         time (`corpus_baselines`, defaulting to this row alone) fewer than K=2
         distinct path values appear → no diversity to grade equivalence
         against → pass.
      3. genuine-match   — non-trivial baseline + diverse corpus + the routed
         selection dropped no baseline path (silent_miss_count == 0) → pass.
      4. genuine-mismatch — non-trivial baseline + diverse corpus + the routed
         selection dropped >=1 baseline path (silent_miss_count > 0) → the
         canary records a silent_miss episode (the Phase 1c-2c behavior,
         preserved on exactly this disposition).

    Match/mismatch is keyed on `silent_miss_count` (dropped baseline context),
    NOT full-set inequality — a pure hallucination (extra routed paths) is not
    a silent miss; it stays tracked by hallucination_count as before. Defensive:
    any malformed input → `unknown` (never raises)."""
    try:
        baseline = set(baseline_paths or [])
        if len(baseline) < n:
            return DISPOSITION_VACUOUS_EMPTY
        corpus = corpus_baselines if corpus_baselines is not None else [baseline_paths]
        distinct_paths: set[str] = set()
        for sel in corpus:
            distinct_paths |= set(sel or [])
        if len(distinct_paths) < k:
            return DISPOSITION_VACUOUS_UNIFORM
        miss = int(comparator_result.get("silent_miss_count", 0))
        return (DISPOSITION_GENUINE_MISMATCH if miss > 0
                else DISPOSITION_GENUINE_MATCH)
    except Exception:  # noqa: BLE001 — never-raise contract (defensive)
        return DISPOSITION_UNKNOWN


def emit_stage_a_shadow_compare(
    *,
    step_idx: int | None,
    routed_paths: list[str],
    baseline_paths: list[str],
    baseline_source: str = "identity",
    corpus_baselines: list[list[str]] | None = None,
) -> dict[str, Any]:
    """Emit the begin/end pair around a Stage A selection comparison.

    `shadow_compare.begin` carries only the inputs; `shadow_compare.end`
    carries all four comparator outputs (Q(a) decision). When the
    comparator finds a silent miss (silent_miss_count > 0), also emit
    `stage_a.silent_miss.detected` — by construction this never fires in
    Phase 0 (routed == baseline), but the path is live for Phase 1.
    Returns the comparator result dict. Never raises (delegates to emit).

    v3 Phase 1c Step 3 (Step3C-F1): `baseline_source` records WHERE the
    baseline selection came from — `"historical"` (option-(b) DB lookup hit),
    `"parallel"` (option-(a) live parallel-Opus fallback on a lookup miss), or
    `"identity"` (non-canary: baseline == routed). A data field on the existing
    shadow_compare kinds (no new VALID_KINDS entry); lets Phase 2 distinguish
    option-a/b episodes when grading rich-context silent_miss.

    v3 Phase 2d Step 2: `disposition` (one of the four enums above plus
    `unknown`) is recorded on `shadow_compare.end`, and the
    `stage_a.silent_miss.detected` episode fires only on `genuine-mismatch`
    (the per-row + per-corpus checks must both pass). `corpus_baselines`
    defaults to this row alone — the corpus visible at per-row call time; on
    the empty-context sweep `vacuous-empty` short-circuits before the per-corpus
    check matters. Phase 2d2 wires the real cross-row corpus.
    """
    emit(
        "stage_a.shadow_compare.begin",
        {
            "step_idx": step_idx,
            "routed_paths": list(routed_paths or []),
            "baseline_paths": list(baseline_paths or []),
            "baseline_source": baseline_source,
        },
        step_idx=step_idx,
    )
    result = compare_stage_a_selections(routed_paths, baseline_paths)
    disposition = classify_comparator_disposition(
        result, list(baseline_paths or []), corpus_baselines=corpus_baselines
    )
    emit(
        "stage_a.shadow_compare.end",
        {"step_idx": step_idx, "baseline_source": baseline_source,
         "disposition": disposition, **result},
        step_idx=step_idx,
    )
    if disposition == DISPOSITION_GENUINE_MISMATCH:
        emit(
            "stage_a.silent_miss.detected",
            {
                "step_idx": step_idx,
                "disposition": disposition,
                "silent_miss_count": result["silent_miss_count"],
                "baseline_only_paths": result["baseline_only_paths"],
            },
            step_idx=step_idx,
        )
    return result


# ---------------------------------------------------------------------------
# v3 Phase 0 Step 4 — cache-family diagnostics (V3P0-6)
#
# Three telemetry lines on every Planner stage event, separating what v2
# blended into one "caching" line:
#   (a) vault_index_hit — did Stage A reuse the in-process memoised vault
#       index (true on 2nd+ Stage A call within a build) or build it fresh
#       (false on the first)? Null on Stage B/C — the question doesn't
#       apply (they don't use the vault index). [Q(c): null, not false.]
#   (b) candidate_user_block_sizes — token-count estimate per user-prompt
#       block (brief / state / vault_files / prior_step) that COULD be
#       cache-controlled if v3 extended caching beyond the system prompt.
#       Phase 0 measures, never acts. Real-mode invariant: the sum ≈
#       observed input_tokens − the 3,479-token system prompt.
#   (c) seconds_since_cache_creation — wall-clock seconds since the most
#       recent cache_creation event on the same (run_id, mode); null for
#       cache_creation calls themselves and before any creation is seen.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Per-model Planner token constants (v3 Phase 2a Step 1, V3P2A-1).
#
# The criterion-3 sum-check relates the chars-based candidate_user_block_sizes
# decomposition to the API's real token counts via the AFFINE, cache-invariant
# relation (Step1C-F1):
#
#   uncached_user_prompt_equiv ≈ PLANNER_USER_TEMPLATE_TOKENS
#       + block_sum × block_token_inflation_factor(model)
#
#   uncached_user_prompt_equiv = input_tokens + cache_read_input_tokens
#       + cache_creation_input_tokens − planner_system_prompt_tokens(model)
#
# Cache-invariant: the cached system prompt moves between input_tokens and
# cache_read/creation, but the sum preserves the user-prompt total — so the
# relation survives Phase 1c Step 2's user-block caching.
#
# BOTH the system-prompt constant AND the inflation factor (the affine SLOPE)
# are PER-MODEL; the template intercept (PLANNER_USER_TEMPLATE_TOKENS) is
# SHARED. Phase 2a Step 0 Q-A5 measured this directly: applying the Opus slope
# (1.64) to the 9 Haiku Stage A rows of the Phase 1c exit-sweep gave 0/9 within
# ±5% (overshoot 21–33%), but a Haiku-native fit held 9/9 (R²=0.994) at slope
# 1.18 with the SAME intercept (~408 ≈ 407). The slope encodes chars/token
# density, which is model-specific (Opus ~3.0, Haiku ~4.0 chars/token on the
# dense JSON-structured prompt); the intercept is the fixed template
# scaffolding, which tokenises near-identically across models. So the affine
# FORM generalises across models; only the slope is per-model. This retired
# the Phase 1c Opus-only sum-check filter (Step1C-F2 carry-forward).
#
# Provenance:
#   Opus system 3479  — V2P4-4 (cache_creation_input_tokens; count_tokens 3477, Δ2)
#   Haiku system 2590 — Anthropic count_tokens API, Phase 2a Step 0 Q-A1
#                       (method validated against the Opus 3477 count)
#   Opus slope 1.64   — 4/2.44 (V3P0-6 density); Phase 1c Step 0 affine fit
#   Haiku slope 1.18  — Phase 1c exit-sweep Haiku Stage A fit (N=9, R²=0.994),
#                       Q-A5. Step 4 grades a fresh N=9 from the Phase 2a exit
#                       sweep, so the provenance carries forward.
#   template 407      — Phase 1c Step 0 affine intercept (shared, both models)
#
# Unknown models fall back to the Opus values (conservative) and register in
# unknown_token_models() — mirrors harness_v2.unknown_cost_models() (V3P1C-4),
# so a Phase 2 new model surfaces explicitly rather than silently defaulting.
# ---------------------------------------------------------------------------

_DEFAULT_TOKEN_MODEL = "claude-opus-4-7"

PLANNER_SYSTEM_PROMPT_TOKENS_BY_MODEL: dict[str, int] = {
    "claude-opus-4-7": 3479,
    "claude-haiku-4-5-20251001": 2590,
}

BLOCK_TOKEN_INFLATION_FACTOR_BY_MODEL: dict[str, float] = {
    "claude-opus-4-7": 1.64,
    "claude-haiku-4-5-20251001": 1.18,
}

# Fixed user-prompt template scaffolding (instructions / headers / schema
# reminder / formatting) wrapping the four content blocks, excluded from
# candidate_user_block_sizes (V3P0-6). SHARED across models (Q-A5: the
# intercept generalises; only the slope is per-model).
PLANNER_USER_TEMPLATE_TOKENS = 407

_unknown_token_models: set[str] = set()


def unknown_token_models() -> set[str]:
    """Models passed to the per-model token accessors that were absent from
    the mappings (they fell back to the Opus values). Mirrors
    harness_v2.unknown_cost_models() (V3P1C-4): surfaces cross-model gaps for
    explicit registration instead of silently defaulting."""
    return set(_unknown_token_models)


def planner_system_prompt_tokens(model: str) -> int:
    """Per-model Planner system-prompt token count (the quantity subtracted in
    the cache-invariant uncached_user_prompt_equiv). Unknown model → the Opus
    value (conservative) + registered in unknown_token_models()."""
    tokens = PLANNER_SYSTEM_PROMPT_TOKENS_BY_MODEL.get(model)
    if tokens is None:
        _unknown_token_models.add(model)
        return PLANNER_SYSTEM_PROMPT_TOKENS_BY_MODEL[_DEFAULT_TOKEN_MODEL]
    return tokens


# ---------------------------------------------------------------------------
# Cost rates — per-model, USD per million tokens. Verified against Anthropic's
# pricing page (platform.claude.com/docs/en/docs/about-claude/pricing, fetched
# 2026-05-26): Opus 4.7 $5/$25/$6.25/$0.50, Haiku 4.5 $1/$5/$1.25/$0.10
# (input / output / 5m-cache-write / cache-read per Mtok). v3 Phase 1c Step 3.5
# (Step3.5C-F1) replaced the stale Opus-4.1 rates ($15/$75/$18.75/$1.50). Cache:
# 5m-write = 1.25x input, read = 0.1x input.
#
# v4 Phase 1a housekeeping: lifted here from tools/harness_v2.py. events.py is
# already the canonical per-model-data home (PLANNER_SYSTEM_PROMPT_TOKENS_BY_
# MODEL above, V3P2A); hosting MODEL_RATES here lets the lightweight
# model-selection seam (anvil/routing.py) import the canonical rate table
# without dragging harness_v2's duckdb/openpyxl imports into its — and Step 3's
# planner — graph. harness_v2 and exam_harness now import MODEL_RATES from here:
# the V3P1C-4 mirror invariant (one constant, no drift across both harnesses +
# the seam). The SQL/DB-coupled helpers stay in harness_v2 — _rate_case and
# _cost_usd_case_sql generate DuckDB view SQL, unknown_cost_models(con) queries
# a DuckDB connection — none are constant-definitions. Unknown models fall back
# to the Opus 4.7 rates (conservative overcharge).
# ---------------------------------------------------------------------------

MODEL_RATES: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 5.0, "output": 25.0, "cache_create": 6.25, "cache_read": 0.50},
    "claude-haiku-4-5-20251001": {
        "input": 1.0, "output": 5.0, "cache_create": 1.25, "cache_read": 0.10},
}
DEFAULT_MODEL_RATES = MODEL_RATES["claude-opus-4-7"]


def block_token_inflation_factor(model: str) -> float:
    """Per-model chars/4 → real-token inflation (the affine SLOPE). Unknown
    model → the Opus value (conservative) + registered in
    unknown_token_models()."""
    factor = BLOCK_TOKEN_INFLATION_FACTOR_BY_MODEL.get(model)
    if factor is None:
        _unknown_token_models.add(model)
        return BLOCK_TOKEN_INFLATION_FACTOR_BY_MODEL[_DEFAULT_TOKEN_MODEL]
    return factor


# v3 Phase 2a Step 2 (V3P2A-2): raw Stage A response recording. The model's
# pre-parser response is recorded on planner.stage_a.parsed as raw_response_text
# (+ a truncated:bool flag), so a future rich-context comparator (Phase 2c) can
# grade on the model's INTENT, not just the parser's post-filter result. Q-A2:
# the empirical max Stage A response on the empty-context corpus is ~720 chars
# (mock) / ~280 (real); 16384 clears that ~22× while bounding per-event payload
# at ~16KB for the rich-context future. The cap protects the cache_diagnostics
# / per_task_comparison join surfaces and the DuckDB ingest path (R2).
RAW_RESPONSE_MAX_CHARS = 16384


def _truncate_raw_response(text: str) -> tuple[str, bool]:
    """Truncate a raw model response to RAW_RESPONSE_MAX_CHARS for recording.

    Returns (recorded_text, truncated). The slice is on the DECODED Python
    string (`text[:N]`), so it cuts on a character boundary and can never split
    a UTF-8 codepoint — UTF-8 safety by construction. `truncated` is True iff
    the original exceeded the limit; when False the recorded text is identical
    to the input. Single source of truth for the truncation contract."""
    text = text or ""
    return text[:RAW_RESPONSE_MAX_CHARS], len(text) > RAW_RESPONSE_MAX_CHARS


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars/token (the standard heuristic).

    Used for candidate block sizing — an estimate, not a tokenizer call.
    The real tokenizer count lives in the API's input_tokens; this gives
    a per-block decomposition that sums to ≈ that figure in real mode.
    """
    return (len(text or "") + 3) // 4


def estimate_user_block_sizes(blocks: dict[str, str]) -> dict[str, int]:
    """Map {block_name: block_text} → {block_name: token_estimate}.

    Each call site passes the user-prompt blocks it actually assembled
    (a stage with no prior-step block passes "" for it → 0). The sum is
    a candidate-caching decomposition of the user prompt.
    """
    return {name: _estimate_tokens(text) for name, text in blocks.items()}


def cache_diagnostics(
    *,
    vault_index_hit: bool | None,
    candidate_user_block_sizes: dict[str, int],
    seconds_since_cache_creation: float | None,
) -> dict[str, Any]:
    """Package the three cache-family fields for an event's data payload.

    Thin by design: each field is computed at the call site where its
    inputs live (vault_index_hit + block sizes via the self-stash
    pattern; seconds_since_cache_creation from the Planner's TTL state),
    then handed here so the three lines land together on every Planner
    stage event.
    """
    return {
        "vault_index_hit": vault_index_hit,
        "candidate_user_block_sizes": candidate_user_block_sizes,
        "seconds_since_cache_creation": seconds_since_cache_creation,
    }


# ---------------------------------------------------------------------------
# Module-global run state
# ---------------------------------------------------------------------------

_run_id: str | None = None
_anchor_monotonic: float | None = None
_drop_count: int = 0

_UNKNOWN_RUN = "unknown-run"

# Track unknown-kind log lines to keep the warning channel quiet:
# log the first occurrence of each unknown kind, drop the rest silently
# (the drop_count still increments).
_logged_unknown_kinds: set[str] = set()


# ---------------------------------------------------------------------------
# Pydantic Event schema
# ---------------------------------------------------------------------------

class Event(BaseModel):
    """One structured event. Serialised as a JSON line in events.jsonl."""

    ts: str
    run_id: str
    step_idx: int | None = None
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0

    @field_validator("kind")
    @classmethod
    def _kind_in_catalogue(cls, v: str) -> str:
        if v not in VALID_KINDS:
            raise ValueError(f"unknown kind: {v}")
        return v


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def current_run_id() -> str:
    """Return the active run_id, or `"unknown-run"` if no run is active."""
    return _run_id if _run_id is not None else _UNKNOWN_RUN


def drop_count() -> int:
    """Return the cumulative count of dropped emits for the active run.

    Resets at `end_run()`. Tests assert on this value to verify the
    never-raises contract held under injected failure.
    """
    return _drop_count


def begin_run(run_id: str) -> None:
    """Start a new run. Sets the module-global run_id and monotonic anchor,
    ensures the events file's parent dir is writable, emits `run.start`.

    Idempotent shape: a second `begin_run(...)` call resets the anchor
    (and run_id) and emits a fresh `run.start`. The caller is expected to
    pair it with an `end_run()`; lifecycle drift is the caller's problem.
    """
    global _run_id, _anchor_monotonic, _drop_count, _logged_unknown_kinds
    _run_id = str(run_id)
    _anchor_monotonic = time.monotonic()
    _drop_count = 0
    _logged_unknown_kinds = set()

    # Best-effort parent-dir creation. If this fails the first emit will
    # fail and increment _drop_count; never-raises holds.
    try:
        _events_path_for(_run_id).parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001 — never-raise
        log.warning(f"[events] begin_run mkdir failed for {_run_id}: {e}")

    log.info(f"[events] begin_run {_run_id}")
    emit("run.start", {})


def end_run() -> None:
    """Emit `run.end` with the cumulative drop count, then reset module
    globals so the next `begin_run` starts clean.

    Calling `end_run()` without a preceding `begin_run()` is a no-op:
    no event is emitted, the sentinel state is already in place.
    """
    global _run_id, _anchor_monotonic, _drop_count, _logged_unknown_kinds
    if _run_id is None:
        # No active run; nothing to flush. Stays silent (no log line).
        return

    drops = _drop_count
    rid = _run_id
    emit("run.end", {"drops": drops})
    log.info(f"[events] end_run {rid} drops={drops}")

    _run_id = None
    _anchor_monotonic = None
    _drop_count = 0
    _logged_unknown_kinds = set()


def emit(kind: str, data: dict[str, Any], step_idx: int | None = None) -> bool:
    """Append one Event to the active run's `events.jsonl`.

    Returns True on success, False on failure (validation, IO, anything).
    Never raises. Increments `_drop_count` on every failure path.
    """
    global _drop_count
    try:
        # Pre-validate kind to avoid the Pydantic ValidationError path
        # for the most common drop reason (a typo in instrumentation).
        if kind not in VALID_KINDS:
            if kind not in _logged_unknown_kinds:
                log.warning(f"[events] unknown kind: {kind}")
                _logged_unknown_kinds.add(kind)
            _drop_count += 1
            return False

        rid = current_run_id()
        ts = _now_iso()
        elapsed = _elapsed_ms(kind)

        try:
            event = Event(
                ts=ts,
                run_id=rid,
                step_idx=step_idx,
                kind=kind,
                data=data if isinstance(data, dict) else {},
                elapsed_ms=elapsed,
            )
        except ValidationError as e:
            log.warning(f"[events] schema validation failed for {kind}: {e}")
            _drop_count += 1
            return False

        line = event.model_dump_json()
        _append_line(_events_path_for(rid), line)
        return True

    except (OSError, UnicodeError) as e:
        log.warning(f"[events] write failed ({kind}): {type(e).__name__}: {e}")
        _drop_count += 1
        return False
    except Exception as e:  # noqa: BLE001 — never-raise contract
        log.warning(f"[events] unexpected ({kind}): {type(e).__name__}: {e}")
        _drop_count += 1
        return False


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC ISO-8601 with millisecond precision (e.g. 2026-05-20T10:15:42.123+00:00)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _elapsed_ms(kind: str) -> int:
    """Milliseconds since `begin_run` set the monotonic anchor.

    Returns 0 for `run.start` (the anchor was just set; the event marks
    t=0) or when no anchor is set (e.g. an emit before `begin_run`).
    """
    if kind == "run.start" or _anchor_monotonic is None:
        return 0
    return int((time.monotonic() - _anchor_monotonic) * 1000)


def _events_path_for(run_id: str) -> Path:
    """Resolve the events.jsonl path for a given run_id.

    Honours `ANVIL_ROOT` env (set at every call, not cached at import),
    so tests can redirect writes by setting the env or by patching
    `_events_path_for` directly. Defaults to the repo root (parent of
    the `anvil/` package dir) — same resolution as `Config.load()` at
    `anvil/config.py:55–56`.
    """
    default_root = Path(__file__).resolve().parent.parent
    root = Path(os.environ.get("ANVIL_ROOT", str(default_root))).expanduser()
    return root / "state" / "runs" / run_id / "events.jsonl"


def _append_line(path: Path, line: str) -> None:
    """Append `line` (plus newline) to `path`, creating parent dirs.

    Uses `_real_append` — O(1) per emit. Step 2 prep replaced the
    Step 1 read-modify-write block (which was O(file-size) per emit
    and would cost quadratic bytes across Step 7's calibration sweep).

    Tests patch `anvil.events._real_append` to inject OSError; this
    helper propagates the exception so `emit`'s outer handler catches
    it and increments `_drop_count`. `_real_write` is still captured
    at module top for any future caller that needs atomic-replace
    semantics, but the hot path no longer touches it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _real_append(path, line + "\n")
