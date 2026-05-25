"""Planner — the real two-stage Opus-driven planner (design Parts 1-4 /
implementation-notes Component 3).

Stage A selects vault context files from a frontmatter index; Stage B
generates the JSON plan, with retry-once-with-error and escalation on a
second failure. `plan_step(brief, state, step_idx)` returns either a
validated `Plan` pydantic model or an escalation dict (`escalate: True`).
The caller checks `isinstance(result, dict) and result.get("escalate")`
first; otherwise it is a `Plan`.

The Phase 0 stub (hardcoded plans from a fixture) and the Phase 0
`validate_plan_scope` are removed here — scope is now enforced inside
`_validate_plan_structure` (checks 4 and 5) before any `Plan` is
constructed, so an out-of-scope plan becomes a planner-validation-failure
escalation rather than a separate orchestrator-side check.

`step_idx` is 0-based (the orchestrator loops `range(len(brief.steps))`);
`Plan.step_number` is 1-based.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Literal

import anthropic
import yaml
from pydantic import BaseModel

from anvil import events as _events
from anvil import routing
from anvil.brief import Step
from anvil.calibration import CHEAP_STAGE_A_MODEL
from anvil.policy import PHASE_1A_PLACEHOLDER, PHASE_1B_STAGE_A_CANARY, RoutingPolicy
from anvil.voice import load_voice_spec

# v3 Phase 3 3b (β-ii): opt-in env var gating the shadow-execute-Haiku branch.
# Dormant unless explicitly truthy — keeps every existing sweep (Phase 1b/2)
# byte-identical if re-run, and lets the 3b collection sweep (Step 6) + tests
# turn it on deterministically. Sibling to ANVIL_CANARY_TASKS="" (the 3a knob
# that opts OUT of canary routing); this opts IN to shadow collection.
_SHADOW_EXECUTE_ENV = "ANVIL_SHADOW_EXECUTE_HAIKU"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

log = logging.getLogger("anvil.planner")

# design Part 2: Stage A timeout is fixed at 30s (impl-notes Component 3).
# Stage B uses self.timeout (the configured planner_timeout).
_STAGE_A_TIMEOUT = 30

# v3 Phase 1a Step 1: canonical default model for unrouted Planner
# construction. A bare Planner() — or one given neither `model=` nor any
# per-stage kwarg — routes all three stages here. Deliberately NOT aliased
# to events.SHADOW_ROUTE_PHASE_0 (also "claude-opus-4-7" today): the
# planner's construction default and the placeholder shadow route are
# distinct concepts that will diverge once Phase 1b lands real routing.
DEFAULT_PLANNER_MODEL = "claude-opus-4-7"

# v4 Phase 1a Step 3 (Amendment 5): construction-default constants for
# call-site clarity. Both equal DEFAULT_PLANNER_MODEL — the v3 Stage A/B
# default is Opus under the placeholder policy. "Haiku on Stage A" is NOT a
# default: it is the calibration canary (PHASE_1B_STAGE_A_CANARY) routing
# empty-context Stage A to CHEAP_STAGE_A_MODEL. These are documentation aids;
# the live per-stage default is self.stage_<x>_model (= base = this constant
# when no per-stage kwarg is passed).
DEFAULT_STAGE_A_MODEL = DEFAULT_PLANNER_MODEL
DEFAULT_STAGE_B_MODEL = DEFAULT_PLANNER_MODEL


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


class Planner:
    """Two-stage planner. __init__ params are optional with defaults so
    Planner() still constructs (Phase 0 precedent, kept for the
    orchestrator default and the Step 5 retry tests). With no api_key the
    client is None; the system prompt is still loaded so tests that mock
    _call_anthropic at the method level work unchanged."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        vault_root=None,
        *,
        stage_a_model: str | None = None,
        stage_b_model: str | None = None,
        stage_c_model: str | None = None,
        policy=None,
        historical_baseline=None,
    ) -> None:
        self.api_key = api_key
        # v3 Phase 1a Step 1: per-stage model plumbing replaces the single
        # self.model. Three-tier resolution per stage: an explicit per-stage
        # kwarg wins; else the single `model=` kwarg (the back-compat base
        # that sets all three at once); else DEFAULT_PLANNER_MODEL. Every
        # former self.model read site now dispatches through
        # _model_for_stage(stage), so the wrong model can never leak into
        # an adjacent stage.
        base = model if model is not None else DEFAULT_PLANNER_MODEL
        self.stage_a_model = stage_a_model if stage_a_model is not None else base
        self.stage_b_model = stage_b_model if stage_b_model is not None else base
        self.stage_c_model = stage_c_model if stage_c_model is not None else base
        self.timeout = timeout
        self.vault_root = Path(vault_root) if vault_root else Path(".")
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else None
        # Voice substitution is one-shot at construction (design Part 1):
        # planner-system.md loaded once, {VOICE_SPEC} replaced by the live
        # spec (load_voice_spec() reads VAULT_PATH from the env — decision
        # #1, the Phase 0 vault_root shim is gone).
        system_path = (
            Path(__file__).resolve().parent / "prompts" / "planner-system.md"
        )
        voice_spec = load_voice_spec()
        self._system_prompt = system_path.read_text(encoding="utf-8").replace(
            "{VOICE_SPEC}", voice_spec
        )
        # Phase 4 Step 3: completion-artefacts prompt. Loaded once at
        # construction with {VOICE_SPEC} substituted, frozen for the
        # build lifetime. Separate from _system_prompt because the
        # discipline framing differs (drafting paperwork vs planning
        # a step).
        artefacts_path = (
            Path(__file__).resolve().parent / "prompts" / "planner-completion-artefacts.md"
        )
        self._artefacts_prompt = artefacts_path.read_text(encoding="utf-8").replace(
            "{VOICE_SPEC}", voice_spec
        )
        # v3 Phase 0 Step 4 (V3P0-6): cache-family diagnostics state.
        # Instance attributes (NOT module-global): one Planner per build
        # (orchestrator.py:120), so these are naturally build-scoped and
        # reset on construction. Instance scope also avoids the events.py
        # backwards-coupling a module-global reset would need, and removes
        # a real test-isolation hazard — a module-global keyed on run_id
        # would leak a stale cached index between tests that reuse the
        # same run_id (e.g. "planner-events-test"), whereas a fresh Planner
        # per test starts clean.
        #
        # _vault_index_cache: run_id -> built vault_index. Real
        # memoisation (build + reuse across steps); output-neutral because
        # the index is build-stable. _last_cache_creation_monotonic:
        # run_id -> time.monotonic() of the most recent cache_creation>0
        # call, for seconds_since_cache_creation. Keyed on run_id, which is
        # mode-suffixed in calibration and unique per build, so it is the
        # effective (run_id, mode) key — the Planner has no mode attribute
        # at plan time.
        self._vault_index_cache: dict[str, dict] = {}
        self._last_cache_creation_monotonic: dict[str, float] = {}
        # v3 Phase 1a Step 3 (V3P1A-3): per-instance routing policy engine.
        # Per-instance (not module-global) for the same test-isolation reason
        # as the V3P0-6 cache state — a fresh Planner per build/test starts
        # clean. MockedPlanner inherits this (no __init__ override).
        # v3 Phase 1b Step 2: the orchestrator may pass a calibration-wired
        # policy (PHASE_1B_STAGE_A_SHADOW) via `policy=`. None → the
        # PHASE_1A_PLACEHOLDER default (Phase-1a-equivalent, default sweep
        # byte-identical). The 3 emit sites already flow decision.route_candidate
        # through (V3P1A-3), so a diverging candidate needs no wrapper change.
        self._policy = policy if policy is not None else RoutingPolicy(
            PHASE_1A_PLACEHOLDER
        )
        # v3 Phase 1c Step 3 (V3P1C-3): comparator option-(b). None → the canary
        # always uses the live parallel-Opus baseline (Phase 1b-equivalent).
        self._historical_baseline = historical_baseline

    def _model_for_stage(self, stage: str) -> str:
        """v3 Phase 1a Step 1: resolve the model for a Planner stage.

        Explicit dict dispatch (not getattr-by-convention): an unknown
        stage raises a loud KeyError naming the offending value rather
        than silently masking a typo. Single source of truth for every
        model read site — plan_step / _run_stage_b_with_retry api_start
        emits, the _call_anthropic API call + emits, and Stage C via
        draft_completion_artefacts — and inherited unchanged by
        MockedPlanner (its overridden _call_anthropic reads it too;
        V3P0-3 parallel-wire).
        """
        return {
            "A": self.stage_a_model,
            "B": self.stage_b_model,
            "C": self.stage_c_model,
        }[stage]

    def plan_step(self, brief, state, step_idx: int):
        """Stage A -> Stage B (with retry). Returns a validated Plan, or an
        escalation dict (`escalate: True`) the orchestrator routes to the
        Telegram escalation path. Stage A failure / empty -> zero selected
        files; Stage B still sees the brief and state (design Part 2)."""
        # v2 Phase 1 Step 2: emit Stage A sub-events. Every emit is
        # never-raise; if events.py is misconfigured, planning still works.
        # v3 Phase 0 Step 4 (V3P0-6): in-process vault-index memoisation.
        # Build the index once per build (keyed on run_id) and reuse it on
        # subsequent steps' Stage A calls. vault_index_hit records whether
        # this call reused the cached index (true on the 2nd+ Stage A call
        # within a build) or built it fresh (false on the first). Reuse is
        # output-neutral — the index is build-stable (context_paths come
        # from the brief, which does not change mid-build).
        run_id = _events.current_run_id()
        if run_id in self._vault_index_cache:
            vault_index = self._vault_index_cache[run_id]
            self._current_vault_index_hit = True
        else:
            vault_index = _build_vault_index(
                [str(p) for p in brief.context_paths], self.vault_root
            )
            self._vault_index_cache[run_id] = vault_index
            self._current_vault_index_hit = False
        _events.emit(
            "planner.stage_a.start",
            {"step_idx": step_idx, "vault_index_size": len(vault_index)},
            step_idx=step_idx,
        )
        stage_a_prompt = _assemble_stage_a_prompt(
            brief, state, step_idx, vault_index
        )
        # v3 Phase 0 Step 4 (V3P0-6): candidate block sizes for Stage A,
        # stashed for the wrapper's cache_diagnostics emit. Stage A's
        # vault block is the frontmatter index (mapped to "vault_files");
        # it has no prior-step block. Re-derives brief_md/state_json/index
        # yaml (trivial I/O) rather than threading them out of the
        # assembly function — keeps that function's signature stable.
        self._current_block_sizes = _events.estimate_user_block_sizes({
            "brief": _read_brief_md(state),
            "state": state.model_dump_json(indent=2),
            "vault_files": yaml.safe_dump(
                vault_index, default_flow_style=False, sort_keys=True
            ),
            "prior_step": "",
        })
        _events.emit(
            "planner.stage_a.prompt_assembled",
            {
                "step_idx": step_idx,
                "prompt_chars": len(stage_a_prompt),
                "vault_index_size": len(vault_index),
            },
            step_idx=step_idx,
        )
        _events.emit(
            "planner.stage_a.api_start",
            {
                "step_idx": step_idx,
                "prompt_chars": len(stage_a_prompt),
                "model": self._model_for_stage("A"),
                "stage": "A",
            },
            step_idx=step_idx,
        )
        # _call_anthropic emits planner.stage_a.api_end from inside.
        # Stash step_idx on self so the wrapper picks it up.
        # v3 Phase 0 Step 1 (V3P0-2): also stash context_paths_count so
        # the wrapper's routing-observability emit can record it without
        # widening _call_anthropic's brief-agnostic signature.
        self._current_step_idx = step_idx
        self._current_context_paths_count = len(
            getattr(brief, "context_paths", []) or []
        )
        # v4 Phase 1a Step 3 (Amendment 5): per-step operator model override.
        # Read step.model (Step 2's brief-schema field), resolve it via
        # routing.resolve_model, and stash the resolved version string for
        # _api_model to consume. When set it overrides the policy (incl. the
        # canary) for the model the API runs and the `model` event field
        # records; None (no `model:` declared) → the existing v3 machinery runs
        # unchanged → byte-identical default path. Resolved only when declared
        # (a no-op on the default path); set fresh per step so it never leaks.
        try:
            _step_model = brief.steps[step_idx].model
        except (IndexError, AttributeError):
            _step_model = None
        self._current_step_model = (
            routing.resolve_model(_step_model) if _step_model else None
        )
        stage_a_resp = self._call_anthropic(
            system=self._system_prompt, user=stage_a_prompt,
            timeout=_STAGE_A_TIMEOUT, step=step_idx + 1, stage="A",
        )
        # v3 Phase 0 Step 3 (V3P0-5): pass step_idx so _parse_stage_a_response
        # can attribute each stage_a.parser_drop event to this step.
        selected = _parse_stage_a_response(
            stage_a_resp, vault_index, step_idx=step_idx
        )
        # paths_dropped_as_hallucinated = paths the model returned that
        # weren't in the vault_index. The parser already filters them
        # via `_parse_stage_a_response`; count via the difference.
        raw_lines = [
            ln.strip() for ln in stage_a_resp.splitlines() if ln.strip()
        ]
        dropped = max(0, len(raw_lines) - len(selected))
        # v3 Phase 2a Step 2 (V3P2A-2): record the parsed selection LIST
        # (selected_paths — comparator-ready, not just the count) and the
        # model's pre-parser response (raw_response_text, truncated to
        # RAW_RESPONSE_MAX_CHARS + a truncated flag). selected_paths is always a
        # list ([] when empty, never null — Q-A4); paths_returned (the count) is
        # retained for back-compat and equals len(selected_paths). The same emit
        # runs on the mock path (MockedPlanner overrides only _call_anthropic
        # and inherits plan_step), so the fields carry the mock fixture's parsed
        # selection + text — no mocked.py change needed (Step2-2a-F1).
        raw_response_text, raw_truncated = _events._truncate_raw_response(
            stage_a_resp)
        _events.emit(
            "planner.stage_a.parsed",
            {
                "step_idx": step_idx,
                "paths_returned": len(selected),
                "paths_dropped_as_hallucinated": dropped,
                "selected_paths": list(selected),
                "raw_response_text": raw_response_text,
                "truncated": raw_truncated,
            },
            step_idx=step_idx,
        )
        # v3 Phase 0 Step 3 (V3P0-4): silent-miss comparator. Lives here (not in
        # _call_anthropic) because `selected` — the parsed Stage A response —
        # only exists after the parse; the mock path inherits this via plan_step.
        # v3 Phase 1b Step 3 (V3P1B-3): canary parallel-Opus baseline. When the
        # canary fired (policy is the canary version AND the API ran the cheap
        # model, route_actual != per-stage Opus), run Opus in parallel on the
        # same prompt for the ground-truth baseline selection (comparator
        # option (a)); otherwise baseline == routed (identity, as before). For
        # empty-context briefs both select nothing → silent_miss == 0 (the
        # canary's success criterion).
        decision = getattr(self, "_current_route_decision", None)
        canary_fired = (
            decision is not None
            and getattr(self._policy, "policy_version", None)
            == PHASE_1B_STAGE_A_CANARY
            and decision.route_actual != self._model_for_stage("A")
        )
        if canary_fired:
            # v3 Phase 1c Step 3 (V3P1C-3): comparator option-(b). Try the
            # historical baseline first (a DB lookup, no API cost); fall back to
            # the live parallel-Opus call (option-a) only on a lookup miss. A
            # reconstructed ∅ (empty list) is a HIT (baseline_source=historical);
            # None is a miss (Step3C-F1).
            historical = None
            if self._historical_baseline is not None:
                task_id = _base_task_from_run_id(_events.current_run_id())
                if task_id:
                    historical = self._historical_baseline.lookup(task_id, step_idx)
            if historical is not None:
                baseline_selected = historical
                baseline_source = "historical"
            else:
                baseline_resp = self._stage_a_canary_baseline(stage_a_prompt, step_idx)
                baseline_selected = _parse_stage_a_response(
                    baseline_resp, vault_index, step_idx=step_idx
                )
                baseline_source = "parallel"
        else:
            baseline_selected = selected
            baseline_source = "identity"
        # v3 Phase 3 3b (β-i): supply the cross-row corpus vocabulary so the
        # comparator's K=2 distinctness check grades a selection against the
        # whole historical corpus, not the single emitting row (Rev B §B.2).
        # classify_comparator_disposition expects list[list[str]] — wrap the
        # flat vocabulary as one pseudo-selection so its union recovers it.
        # Empty vocab (no provider, or a corpus-less DB) → None → the single-row
        # default (today's behaviour). Never raises (provider is never-raise).
        corpus_vocab = (
            self._historical_baseline.corpus_distinct_paths()
            if self._historical_baseline is not None else frozenset()
        )
        corpus_baselines = [sorted(corpus_vocab)] if corpus_vocab else None
        # v3 Phase 3 3b (β-ii): shadow-execute Haiku. On a real-mode rich-context
        # SHADOW step with the opt-in set, run Haiku in parallel and grade its
        # selection against Opus's. INVERTED orientation (Rev B §B.4 #2, brief §5
        # watch item): routed = Haiku (the observed cheap selection), baseline =
        # Opus (`selected`, the ground truth) — so silent_miss = Opus paths Haiku
        # dropped (the T11 genuine-mismatch the design grades). route_actual is
        # UNCHANGED (Opus) — _decide_stage_a_shadow is untouched and plan_step
        # still returns Opus's `selected` downstream; Haiku is observed-only via
        # the comparator event. On a Haiku miss (None) or trigger-off, the
        # comparator runs as before (identity/canary).
        routed_for_compare = selected
        baseline_for_compare = baseline_selected
        compare_source = baseline_source
        if self._should_shadow_execute_haiku():
            haiku_selected = self._stage_a_shadow_execute_haiku(
                stage_a_prompt, step_idx, vault_index
            )
            if haiku_selected is not None:
                routed_for_compare = haiku_selected
                baseline_for_compare = selected
                compare_source = "shadow-execute"
        _events.emit_stage_a_shadow_compare(
            step_idx=step_idx,
            routed_paths=routed_for_compare,
            baseline_paths=baseline_for_compare,
            baseline_source=compare_source,
            corpus_baselines=corpus_baselines,
        )
        result = self._run_stage_b_with_retry(brief, state, step_idx, selected)
        if result.get("escalate"):
            _events.emit(
                "planner.escalate",
                {
                    "step_idx": step_idx,
                    "reason": result.get("reason", ""),
                    "detail": (result.get("detail") or "")[:500],
                },
                step_idx=step_idx,
            )
            return result
        return Plan(**result)

    def _cache_diag_fields(self, stage: str, cache_creation_tokens: int) -> dict:
        """v3 Phase 0 Step 4 (V3P0-6): build the three cache-family fields
        for a Planner stage emit.

        - vault_index_hit: the stashed Stage-A hit/miss; None on Stage
          B/C (the question doesn't apply — Q(c)).
        - candidate_user_block_sizes: the stashed per-block decomposition.
        - seconds_since_cache_creation: from the per-run TTL state — null
          on a cache_creation call (this IS the creation, and we record
          its timestamp for later reads), else now − last_creation (or
          null if no creation has been seen this run).
        """
        run_id = _events.current_run_id()
        now = time.monotonic()
        if cache_creation_tokens and cache_creation_tokens > 0:
            self._last_cache_creation_monotonic[run_id] = now
            seconds_since = None
        else:
            last = self._last_cache_creation_monotonic.get(run_id)
            seconds_since = None if last is None else (now - last)
        vault_index_hit = (
            getattr(self, "_current_vault_index_hit", None)
            if stage == "A" else None
        )
        return _events.cache_diagnostics(
            vault_index_hit=vault_index_hit,
            candidate_user_block_sizes=(
                getattr(self, "_current_block_sizes", {}) or {}
            ),
            seconds_since_cache_creation=seconds_since,
        )

    def _decide_route(self, stage: str):
        """v3 Phase 1b Step 3 (V3P1B-3): compute the RouteDecision PRE-call, so
        the API model can source from `decision.route_actual` (the canary runs
        the cheap model). Splits the old `_policy_routing` into a pre-call
        decide + a post-call `_routing_for`, computed once at the top of
        `_call_anthropic` and reused on the success + both error emit paths.

        `features` = Phase 0's `features_seen` merged with the stashed lint
        `structured_features` (lint wins on key collision, e.g.
        context_paths_count). Uses **pre-call** features only:
        observed_prompt_token_count is None here (it's a post-call observability
        value; Phase 1b's predicate uses context_paths_count, which is pre-call
        — Step3B-F3). Stashes `self._current_route_decision` so plan_step can
        detect whether the canary fired and run the parallel-Opus baseline.
        """
        step_idx = getattr(self, "_current_step_idx", None)
        context_paths_count = getattr(self, "_current_context_paths_count", None)
        features_seen = _events._compute_features_seen(
            stage, step_idx, None, context_paths_count
        )
        lint_res = getattr(self, "_current_lint_result", None)
        lint_features = getattr(lint_res, "structured_features", None) or {}
        merged = {**features_seen, **lint_features}  # lint wins on collision
        decision = self._policy.decide_route(
            stage, merged, fallback_model=self._model_for_stage(stage)
        )
        self._current_route_decision = decision
        return decision

    def _routing_for(self, stage: str, decision, observed_prompt_token_count):
        """v3 Phase 1b Step 3: build the routing-observability dict from the
        pre-computed `decision` + the post-call observed token count. The
        decision was made in `_decide_route` (pre-call); this only stamps the
        observability token count into `features_seen` (the recorded
        `decision_basis` keeps observed=None — Step3B-F3). `route_actual` /
        `route_candidate` / `route_fallback_fired` / `policy_version` all come
        from the decision."""
        return _events.routing_observability(
            stage=stage,
            step_idx=getattr(self, "_current_step_idx", None),
            observed_prompt_token_count=observed_prompt_token_count,
            context_paths_count=getattr(self, "_current_context_paths_count", None),
            route_actual=decision.route_actual,
            route_candidate=decision.route_candidate,
            route_fallback_fired=decision.route_fallback_fired,
            policy_version=self._policy.policy_version,
        )

    def _api_model(self, stage: str, decision) -> str:
        """v3 Phase 1b Step 3 (V3P1B-3): the model the API actually runs (and the
        `model` data field = "what ran").

        Default = the per-stage model — so Step 1's per-stage plumbing AND the
        Step3-F1 inversion (route_actual observational, model = per-stage) are
        PRESERVED for placeholder + shadow. The CANARY is the one policy where
        route_actual means "what runs": under it the API sources from
        decision.route_actual (the acted cheap model, or the per-stage fallback
        when it didn't act). Gating on the canary policy_version — not on
        `route_actual != _model_for_stage` — avoids a placeholder per-stage
        override falsely triggering a model swap (which would defeat Step 1's
        per-stage plumbing under the default policy).

        v4 Phase 1a Step 3 (Amendment 5): a per-step operator override
        (`self._current_step_model`, resolved in plan_step) wins over the
        policy — including the canary. It is the model the API runs and the
        `model` event field records. `route_actual` is left unchanged (the
        policy's decision); the `model`/`route_actual` divergence under an
        override is the intended v4 audit trail, not a regression. When the
        override is absent (the default path), the existing v3 resolution runs
        unchanged → byte-identical. NOT applied to `_model_for_stage`, which
        feeds the canary's Opus ground-truth baseline (_stage_a_canary_baseline)
        and the shadow-execute reference — those stay the construction model."""
        override = getattr(self, "_current_step_model", None)
        if override:
            return override
        if self._policy.policy_version == PHASE_1B_STAGE_A_CANARY:
            return decision.route_actual
        return self._model_for_stage(stage)

    # -----------------------------------------------------------------------
    # Phase 1 Step 5 — Anthropic call wrapper + Stage B retry loop
    #
    # Added to the existing Phase 0 class without touching plan_step (the
    # stub stays callable until Step 6 replaces it). Step 6 wires
    # self._system_prompt and self.vault_root in __init__; here they are
    # read defensively so this step's tests (which mock _call_anthropic at
    # the method level) do not depend on Step 6 wiring.
    # -----------------------------------------------------------------------

    def _call_anthropic(
        self, system: str, user: str, timeout: int, *, step: int, stage: str
    ) -> str:
        """Subprocess-free streaming call. Returns the model text, or ""
        on timeout / rate-limit / any error (never raises).

        SDK note (anthropic 0.102.0): messages.stream(...) is a context
        manager; the brief's stream.get_final_message().usage is reached
        via `with ... as stream`. RateLimitError / APITimeoutError are
        retried once after sleeping min(60, retry-after); a second
        failure returns "". A broad Exception is logged and returns "".
        """
        # v3 Phase 1b Step 3 (V3P1B-3): decide the route BEFORE the call so the
        # API model can source from decision.route_actual — under the canary the
        # API actually runs the cheap model (the Step3-F1 "model = ran" change,
        # now production). The one decision is reused by the success + both
        # error emit paths (A3). For shadow/placeholder, decision.route_actual
        # == _model_for_stage(stage), so this is behaviour-neutral there.
        decision = self._decide_route(stage)
        api_model = self._api_model(stage, decision)

        def _attempt() -> str:
            client = self._client.with_options(timeout=timeout)
            t0 = time.monotonic()
            # v2 Phase 4 Step 1: wrap the system prompt as a single cached
            # content block (cache_control: ephemeral). The whole
            # planner-system.md is the cache prefix (Step 0 Finding 4); one
            # marker covers both Stage A and Stage B because they pass the
            # same self._system_prompt (Finding 6). An empty/falsy system
            # passes through unchanged — an empty text block with
            # cache_control is API-rejected, and the 1024-token minimum
            # means caching only ever applies to the real (~2,603-token)
            # prompt anyway.
            system_param = (
                [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
                if system
                else system
            )
            with client.messages.stream(
                model=api_model,
                max_tokens=8192,
                system=system_param,
                messages=[{
                    "role": "user",
                    "content": _split_user_for_brief_cache(user),
                }],
            ) as stream:
                final = stream.get_final_message()
            duration = time.monotonic() - t0
            text = "".join(
                b.text for b in final.content
                if getattr(b, "type", None) == "text"
            )
            u = final.usage
            log.info(
                f"[planner] step={step} stage={stage} model={api_model} "
                f"input_tokens={u.input_tokens} "
                f"output_tokens={u.output_tokens} "
                f"cache_creation_input_tokens="
                f"{u.cache_creation_input_tokens or 0} "
                f"cache_read_input_tokens="
                f"{u.cache_read_input_tokens or 0} "
                f"duration_s={duration:.1f}"
            )
            # v2 Phase 1 Step 2 (notes.md Finding 1 constraint 2):
            # emit planner.stage_<X>.api_end from inside the wrapper,
            # keyed on the `stage` kwarg. Stage A/B/C all flow through
            # here; the [planner] log line above stays unchanged for
            # backward-compat with tools/exam_harness.py.
            # v3 Phase 0 Step 1 (V3P0-1): routing observability. Passive —
            # observed prompt size is the API's reported input_tokens. Built
            # once so the paired Step 2 shadow.decision can reuse features_seen
            # + route_actual. v3 Phase 1a Step 1: route_actual now sources from
            # the per-stage attribute (_model_for_stage(stage)), matching the
            # model actually passed to client.messages.stream above.
            routing = self._routing_for(stage, decision, u.input_tokens)
            _events.emit(
                f"planner.stage_{stage.lower()}.api_end",
                {
                    "step_idx": getattr(self, "_current_step_idx", None),
                    "model": api_model,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_creation_input_tokens":
                        u.cache_creation_input_tokens or 0,
                    "cache_read_input_tokens":
                        u.cache_read_input_tokens or 0,
                    "duration_ms": int(duration * 1000),
                    "ok": True,
                    **routing,
                    # v3 Phase 0 Step 4 (V3P0-6): cache-family diagnostics.
                    **self._cache_diag_fields(
                        stage, u.cache_creation_input_tokens or 0
                    ),
                },
                step_idx=getattr(self, "_current_step_idx", None),
            )
            # v3 Phase 0 Step 2 (V3P0-3): paired shadow.decision, fired
            # immediately after the api_end. Phase 0 placeholder always
            # agrees with the actual Opus route.
            _events.emit_shadow_decision(
                stage=stage,
                step_idx=getattr(self, "_current_step_idx", None),
                features_seen=decision.decision_basis,
                actual_route_taken=decision.route_actual,
                shadow_route_candidate=decision.route_candidate,
                policy_version=self._policy.policy_version,
            )
            return text

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
                try:
                    return _attempt()
                except (
                    anthropic.APITimeoutError,
                    anthropic.RateLimitError,
                ) as e2:
                    log.error(
                        f"[planner] step={step} stage={stage} "
                        f"rate-limit/timeout twice ({e2}); returning empty"
                    )
                    # v3 Phase 0 Step 1 (V3P0-1): error-path api_end still
                    # carries the five fields; observed_prompt_token_count
                    # is None (the call failed, no usage was returned).
                    routing = self._routing_for(stage, decision, None)
                    _events.emit(
                        f"planner.stage_{stage.lower()}.api_end",
                        {
                            "step_idx": getattr(self, "_current_step_idx", None),
                            "model": api_model,
                            "ok": False,
                            "error": "rate-limit/timeout",
                            **routing,
                            # v3 Phase 0 Step 4 (V3P0-6): no usage on the
                            # error path → cache_creation treated as 0.
                            **self._cache_diag_fields(stage, 0),
                        },
                        step_idx=getattr(self, "_current_step_idx", None),
                    )
                    # v3 Phase 0 Step 2 (V3P0-3): paired shadow.decision on
                    # the error path too, so the 1:1 invariant (one shadow
                    # row per api_end) holds on every path.
                    _events.emit_shadow_decision(
                        stage=stage,
                        step_idx=getattr(self, "_current_step_idx", None),
                        features_seen=decision.decision_basis,
                        actual_route_taken=decision.route_actual,
                        shadow_route_candidate=decision.route_candidate,
                        policy_version=self._policy.policy_version,
                    )
                    return ""
        except Exception as e:  # noqa: BLE001 — never-raise contract
            log.error(
                f"[planner] step={step} stage={stage} call failed "
                f"({e}); returning empty"
            )
            # v3 Phase 0 Step 1 (V3P0-1): error-path api_end carries the
            # five fields; no usage on this path.
            routing = self._routing_for(stage, decision, None)
            _events.emit(
                f"planner.stage_{stage.lower()}.api_end",
                {
                    "step_idx": getattr(self, "_current_step_idx", None),
                    "model": api_model,
                    "ok": False,
                    "error": str(e)[:300],
                    **routing,
                    # v3 Phase 0 Step 4 (V3P0-6): no usage on the error
                    # path → cache_creation treated as 0.
                    **self._cache_diag_fields(stage, 0),
                },
                step_idx=getattr(self, "_current_step_idx", None),
            )
            # v3 Phase 0 Step 2 (V3P0-3): paired shadow.decision (1:1).
            _events.emit_shadow_decision(
                stage=stage,
                step_idx=getattr(self, "_current_step_idx", None),
                features_seen=decision.decision_basis,
                actual_route_taken=decision.route_actual,
                shadow_route_candidate=decision.route_candidate,
                policy_version=self._policy.policy_version,
            )
            return ""

    def _emit_canary_baseline(
        self, step_idx, model, usage, duration_ms, *, ok, error=None
    ) -> None:
        """v3 Phase 1b Step 3 (V3P1B-3): emit the canary parallel-Opus baseline
        call's cost-bearing event. Its own kind (not stage_a.api_end) so it
        doesn't double-count the primary Stage A call or perturb the shadow 1:1
        invariant; the operations view counts its cost via the token formula."""
        data = {
            "step_idx": step_idx,
            "model": model,
            "duration_ms": duration_ms,
            "ok": ok,
        }
        if usage is not None:
            data.update({
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_creation_input_tokens":
                    usage.cache_creation_input_tokens or 0,
                "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
            })
        if error is not None:
            data["error"] = error
        _events.emit(_events.CANARY_BASELINE_KIND, data, step_idx=step_idx)

    def _stage_a_canary_baseline(self, prompt: str, step_idx) -> str:
        """v3 Phase 1b Step 3 (V3P1B-3): the parallel Opus baseline for a canary
        Stage A call. Streams the per-stage Opus reference model on the SAME
        prompt to get a ground-truth selection for the silent-miss comparator,
        emits planner.stage_a.canary_baseline.api_end (cost ledgered), and
        returns the response text. Never raises (→ "" on failure, an empty
        baseline selection). MockedPlanner overrides this to read the fixture."""
        baseline_model = self._model_for_stage("A")  # Opus reference
        try:
            client = self._client.with_options(timeout=_STAGE_A_TIMEOUT)
            t0 = time.monotonic()
            system_param = (
                [{
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }]
                if self._system_prompt else self._system_prompt
            )
            with client.messages.stream(
                model=baseline_model,
                max_tokens=8192,
                system=system_param,
                messages=[{
                    "role": "user",
                    "content": _split_user_for_brief_cache(prompt),
                }],
            ) as stream:
                final = stream.get_final_message()
            duration_ms = int((time.monotonic() - t0) * 1000)
            text = "".join(
                b.text for b in final.content
                if getattr(b, "type", None) == "text"
            )
            self._emit_canary_baseline(
                step_idx, baseline_model, final.usage, duration_ms, ok=True
            )
            return text
        except Exception as e:  # noqa: BLE001 — never-raise contract
            log.error(
                f"[planner] canary baseline (step={step_idx}) failed "
                f"({e}); empty baseline"
            )
            self._emit_canary_baseline(
                step_idx, baseline_model, None, 0, ok=False, error=str(e)[:300]
            )
            return ""

    def _should_shadow_execute_haiku(self) -> bool:
        """v3 Phase 3 3b (β-ii): gate for the shadow-execute-Haiku branch. ALL
        must hold (Rev B §B.4 #2):
          - opt-in env `ANVIL_SHADOW_EXECUTE_HAIKU` truthy (dormant by default);
          - real mode — the run_id carries the `-real` suffix (the Planner has
            no mode attribute; mode lives only in the run_id). Mock mode is
            excluded: this method makes a real Haiku API call, which a mock
            sweep must never do (MockedPlanner does NOT override it);
          - the policy is NOT the canary — the canary path already runs its own
            baseline comparison; shadow-execute is shadow-only (route_actual
            stays Opus). A canary run never reaches this branch;
          - rich context (`context_paths_count > 0`). Empty-context Haiku Stage A
            is the canonical Haiku-Stage-A canary's exclusive scope
            (`canaries/haiku-stage-a-cr-cc-zero.md`); shadow-execute grades the
            rich-context shapes only.
        Never raises (degrades to False on any unexpected internal state)."""
        try:
            if os.environ.get(_SHADOW_EXECUTE_ENV, "").strip().lower() not in _TRUTHY:
                return False
            if not (_events.current_run_id() or "").endswith("-real"):
                return False
            if getattr(self._policy, "policy_version", None) == PHASE_1B_STAGE_A_CANARY:
                return False
            return (getattr(self, "_current_context_paths_count", 0) or 0) > 0
        except Exception:  # noqa: BLE001 — never-raise contract
            return False

    def _stage_a_shadow_execute_haiku(
        self, prompt: str, step_idx, vault_index: dict
    ) -> "list[str] | None":
        """v3 Phase 3 3b (β-ii): the parallel Haiku Stage A shadow call. Mirrors
        `_stage_a_canary_baseline` but targets the cheap model
        (`CHEAP_STAGE_A_MODEL`, Haiku) instead of the per-stage Opus reference —
        it runs Haiku on the SAME assembled Stage A prompt to OBSERVE what a
        cheap route would select, parses it with the same Stage A parser, and
        records its cost-bearing event under `CANARY_BASELINE_KIND` (the existing
        kind — no new VALID_KINDS entry, Rev B §B.3; the kind exists precisely to
        log a parallel model call without perturbing the Stage A 1:1 invariant).
        The cache_diagnostics view extension (Step 4 / β-iii) maps this kind to
        stage 'A', so the emitted shape is identical to the Opus canary baseline
        (model + cache fields) — no `stage` data field is added here.

        Returns the parsed Haiku selection (the COMPARATOR's `routed` side), or
        None on any API/parse failure. Never raises (→ None, and a ok=False
        baseline event), so a Haiku hiccup can never tank a SHADOW step — the
        caller falls back to the identity comparison and the Opus selection is
        still what plan_step returns."""
        try:
            client = self._client.with_options(timeout=_STAGE_A_TIMEOUT)
            t0 = time.monotonic()
            system_param = (
                [{
                    "type": "text",
                    "text": self._system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }]
                if self._system_prompt else self._system_prompt
            )
            with client.messages.stream(
                model=CHEAP_STAGE_A_MODEL,
                max_tokens=8192,
                system=system_param,
                messages=[{
                    "role": "user",
                    "content": _split_user_for_brief_cache(prompt),
                }],
            ) as stream:
                final = stream.get_final_message()
            duration_ms = int((time.monotonic() - t0) * 1000)
            text = "".join(
                b.text for b in final.content
                if getattr(b, "type", None) == "text"
            )
            self._emit_canary_baseline(
                step_idx, CHEAP_STAGE_A_MODEL, final.usage, duration_ms, ok=True
            )
            return _parse_stage_a_response(text, vault_index, step_idx=step_idx)
        except Exception as e:  # noqa: BLE001 — never-raise contract
            log.error(
                f"[planner] shadow-execute Haiku (step={step_idx}) failed "
                f"({e}); no shadow selection"
            )
            self._emit_canary_baseline(
                step_idx, CHEAP_STAGE_A_MODEL, None, 0, ok=False, error=str(e)[:300]
            )
            return None

    def _run_stage_b_with_retry(
        self, brief, state, step_idx: int, selected_paths: list[str]
    ) -> dict:
        """Stage B call -> parse -> validate, with one retry-with-error.
        Returns a validated plan dict, or a planner-validation-failure
        escalation block (design Part 3 retry block). Stage A is not
        re-run; an empty response is a tooling failure -> immediate
        escalation, no retry."""
        system = getattr(self, "_system_prompt", "")
        vault_root = getattr(self, "vault_root", Path("."))
        files = _load_files(selected_paths, vault_root)
        files_loaded_chars = sum(len(c) for c in files.values())
        _events.emit(
            "planner.stage_b.start",
            {"step_idx": step_idx, "retry_attempt": 0},
            step_idx=step_idx,
        )
        _events.emit(
            "planner.stage_b.files_loaded",
            {
                "step_idx": step_idx,
                "files_loaded_count": len(files),
                "files_loaded_chars": files_loaded_chars,
            },
            step_idx=step_idx,
        )
        user_prompt = _assemble_stage_b_prompt(brief, state, step_idx, files)
        # v3 Phase 0 Step 4 (V3P0-6): candidate block sizes for Stage B —
        # the canonical four blocks. vault_files is the loaded file
        # contents (the bulk of Stage B's prompt); prior_step is the
        # prior-step handoff block. Stashed for the wrapper's cache_
        # diagnostics emit; reused on the retry call (same prompt body).
        self._current_block_sizes = _events.estimate_user_block_sizes({
            "brief": _read_brief_md(state),
            "state": state.model_dump_json(indent=2),
            "vault_files": "".join(files.values()),
            "prior_step": _prior_step_block(state, step_idx),
        })
        _events.emit(
            "planner.stage_b.prompt_assembled",
            {
                "step_idx": step_idx,
                "prompt_chars": len(user_prompt),
                "retry_attempt": 0,
            },
            step_idx=step_idx,
        )
        step_no = step_idx + 1

        _events.emit(
            "planner.stage_b.api_start",
            {
                "step_idx": step_idx,
                "prompt_chars": len(user_prompt),
                "model": self._model_for_stage("B"),
                "stage": "B",
                "retry_attempt": 0,
            },
            step_idx=step_idx,
        )
        self._current_step_idx = step_idx
        # v3 Phase 0 Step 1 (V3P0-2): stash context_paths_count alongside
        # step_idx (the retry call below reuses the same stash).
        self._current_context_paths_count = len(
            getattr(brief, "context_paths", []) or []
        )
        response = self._call_anthropic(
            system=system, user=user_prompt, timeout=self.timeout,
            step=step_no, stage="B",
        )
        if not response:
            return _escalation_block(
                "planner-validation-failure",
                "Stage B returned empty after first attempt",
                step_idx,
            )
        try:
            plan = _parse_plan_json(response)
            _validate_plan_structure(plan, brief.steps[step_idx])
            _events.emit(
                "planner.stage_b.parsed",
                {"step_idx": step_idx, "retry_attempt": 0},
                step_idx=step_idx,
            )
            _events.emit(
                "planner.validation.pass",
                {"step_idx": step_idx, "retry_attempt": 0},
                step_idx=step_idx,
            )
            return plan
        except (PlanParseError, PlanValidationError) as e:
            _events.emit(
                "planner.validation.fail",
                {
                    "step_idx": step_idx,
                    "first_error": str(e)[:500],
                    "retry_attempt": 0,
                },
                step_idx=step_idx,
            )
            _events.emit(
                "planner.retry.start",
                {"step_idx": step_idx, "first_error": str(e)[:500]},
                step_idx=step_idx,
            )
            retry_prompt = (
                user_prompt
                + "\n\n## Previous attempt failed validation\n\n"
                + "The previous response failed validation with this "
                + "error:\n\n<validation_error>\n"
                + f"{e}\n</validation_error>\n\n"
                + "Produce a corrected JSON object that addresses the "
                + "error. Output only the JSON, no preamble."
            )
            _events.emit(
                "planner.stage_b.prompt_assembled",
                {
                    "step_idx": step_idx,
                    "prompt_chars": len(retry_prompt),
                    "retry_attempt": 1,
                },
                step_idx=step_idx,
            )
            _events.emit(
                "planner.stage_b.api_start",
                {
                    "step_idx": step_idx,
                    "prompt_chars": len(retry_prompt),
                    "model": self._model_for_stage("B"),
                    "stage": "B",
                    "retry_attempt": 1,
                },
                step_idx=step_idx,
            )
            response2 = self._call_anthropic(
                system=system, user=retry_prompt, timeout=self.timeout,
                step=step_no, stage="B",
            )
            if not response2:
                _events.emit(
                    "planner.retry.end",
                    {
                        "step_idx": step_idx,
                        "second_error_or_none": "empty-response",
                    },
                    step_idx=step_idx,
                )
                return _escalation_block(
                    "planner-validation-failure",
                    f"Stage B retry returned empty. First error: {e}",
                    step_idx,
                )
            try:
                plan = _parse_plan_json(response2)
                _validate_plan_structure(plan, brief.steps[step_idx])
                _events.emit(
                    "planner.retry.end",
                    {"step_idx": step_idx, "second_error_or_none": None},
                    step_idx=step_idx,
                )
                _events.emit(
                    "planner.stage_b.parsed",
                    {"step_idx": step_idx, "retry_attempt": 1},
                    step_idx=step_idx,
                )
                _events.emit(
                    "planner.validation.pass",
                    {"step_idx": step_idx, "retry_attempt": 1},
                    step_idx=step_idx,
                )
                return plan
            except (PlanParseError, PlanValidationError) as e2:
                _events.emit(
                    "planner.retry.end",
                    {
                        "step_idx": step_idx,
                        "second_error_or_none": str(e2)[:500],
                    },
                    step_idx=step_idx,
                )
                _events.emit(
                    "planner.validation.fail",
                    {
                        "step_idx": step_idx,
                        "first_error": str(e2)[:500],
                        "retry_attempt": 1,
                    },
                    step_idx=step_idx,
                )
                return _escalation_block(
                    "planner-validation-failure",
                    f"Plan validation failed twice. First: {e}. "
                    f"Second: {e2}",
                    step_idx,
                )


    # -----------------------------------------------------------------------
    # Phase 4 Step 4 — draft_completion_artefacts
    #
    # One-shot Stage-C call: drafts the setup-log entry and checkpoint body
    # from the completed build state. Reuses _call_anthropic plumbing; same
    # retry-once-with-error pattern as Stage B. Returns either the validated
    # draft dict or a completion-artefacts-draft-failed escalation.
    # -----------------------------------------------------------------------

    def draft_completion_artefacts(self, brief, state) -> dict:
        """Draft setup_log_entry and checkpoint body from completed state.

        Returns either:
          {"setup_log_entry": str, "checkpoint": str}
        or an escalation dict:
          {"escalate": True, "reason": "completion-artefacts-draft-failed",
           "detail": <error>, "step_number": 0}

        Never raises. Two API attempts max (initial + retry-with-error);
        a second failure escalates. step_number=0 signals post-build
        drafting, not a numbered build step.
        """
        system = getattr(self, "_artefacts_prompt", "")
        user_prompt = _assemble_artefacts_prompt(brief, state)

        # v3 Phase 0 Step 1 (V3P0-2): close the V2P1-3 stash gap for
        # Stage C. Pre-Step-1, draft_completion_artefacts set neither
        # stash, so planner.stage_c.api_end carried a STALE
        # _current_step_idx left over from whatever Stage B last ran.
        # Stage C is post-build / run-level, so step_idx is None; the
        # stash now reflects that intent rather than leaking Stage B's
        # value. The historical stale artefact in v2 Stage C events is
        # documented (notes.md V3P0-2) but not retroactively repaired.
        self._current_step_idx = None
        self._current_context_paths_count = len(
            getattr(brief, "context_paths", []) or []
        )
        # v3 Phase 0 Step 4 (V3P0-6): Stage C candidate block sizes.
        # Stage C assembles brief + state (+ a small deploy block folded
        # into none of the canonical four); it has no vault_files or
        # prior_step block, so those are 0. The decomposition is honest
        # for the blocks Stage C actually has.
        self._current_block_sizes = _events.estimate_user_block_sizes({
            "brief": _read_brief_md(state),
            "state": state.model_dump_json(indent=2),
            "vault_files": "",
            "prior_step": "",
        })

        response = self._call_anthropic(
            system=system, user=user_prompt, timeout=self.timeout or 120,
            step=0, stage="C",
        )
        if not response:
            return {
                "escalate": True,
                "reason": "completion-artefacts-draft-failed",
                "detail": "Stage C returned empty after first attempt",
                "step_number": 0,
            }
        try:
            draft = _parse_artefacts_json(response)
            _validate_artefacts_structure(draft)
            return draft
        except (PlanParseError, PlanValidationError) as e:
            retry_prompt = (
                user_prompt
                + "\n\n## Previous attempt failed validation\n\n"
                + "The previous response failed validation with this "
                + "error:\n\n<validation_error>\n"
                + f"{e}\n</validation_error>\n\n"
                + "Produce a corrected JSON object that addresses the "
                + "error. Output only the JSON, no preamble."
            )
            response2 = self._call_anthropic(
                system=system, user=retry_prompt, timeout=self.timeout or 120,
                step=0, stage="C",
            )
            if not response2:
                return {
                    "escalate": True,
                    "reason": "completion-artefacts-draft-failed",
                    "detail": f"Retry returned empty. First error: {e}",
                    "step_number": 0,
                }
            try:
                draft = _parse_artefacts_json(response2)
                _validate_artefacts_structure(draft)
                return draft
            except (PlanParseError, PlanValidationError) as e2:
                return {
                    "escalate": True,
                    "reason": "completion-artefacts-draft-failed",
                    "detail": (
                        f"Artefact validation failed twice. "
                        f"First: {e}. Second: {e2}"
                    ),
                    "step_number": 0,
                }


# ---------------------------------------------------------------------------
# Phase 4 Step 4 — artefact drafting helpers
#
# Module-level pure functions. Same shape as the Stage A/B helpers below.
# ---------------------------------------------------------------------------


def _assemble_artefacts_prompt(brief, state) -> str:
    """Build the user prompt for Stage C from brief + state.

    str.replace per placeholder (not str.format) — matches Stage A/B
    discipline so JSON braces in the embedded state don\'t break parsing.
    """
    try:
        brief_md = Path(state.brief_path).read_text(encoding="utf-8")
    except OSError:
        brief_md = ""

    state_json = state.model_dump_json(indent=2)
    deploy_block = (
        json.dumps(state.deploy, indent=2) if state.deploy
        else "(no deploy on this build)"
    )

    return (
        "## Build brief\n\n"
        "<brief>\n"
        f"{brief_md}\n"
        "</brief>\n\n"
        "## Final build state\n\n"
        "<state>\n"
        f"{state_json}\n"
        "</state>\n\n"
        "## Deploy outcome\n\n"
        f"{deploy_block}\n\n"
        "## Instruction\n\n"
        "Produce the completion artefacts as a JSON object matching "
        "the schema above. Output only the JSON, no preamble, no fences."
    )


def _parse_artefacts_json(text: str) -> dict:
    """json.loads the response; raise PlanParseError on JSONDecodeError.

    Reuses PlanParseError so the retry-once-with-error catch contract
    holds (Stage B and Stage C both raise PlanParseError on bad JSON).
    Markdown fences are NOT stripped — fenced output fails deliberately.
    """
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise PlanParseError(
            f"Stage C output is not valid JSON ({e}). Raw text:\n{text}"
        ) from e


def _validate_artefacts_structure(draft: dict) -> None:
    """Validate draft has exactly two non-empty string fields.

    Reuses PlanValidationError so the retry-with-error catch contract
    is the same shape as Stage B\'s. Also handles the escalation case:
    if the model returns an escalate block, we accept it as-is and
    the caller routes it.
    """
    if not isinstance(draft, dict):
        raise PlanValidationError("draft is not a dict")
    # Accept escalation block as-is
    if draft.get("escalate") is True:
        if not isinstance(draft.get("reason"), str):
            raise PlanValidationError("escalation missing or non-str reason")
        if not isinstance(draft.get("detail"), str):
            raise PlanValidationError("escalation missing or non-str detail")
        return None
    # Normal draft validation
    if set(draft.keys()) != {"setup_log_entry", "checkpoint"}:
        raise PlanValidationError(
            f"draft keys must be exactly {{\"setup_log_entry\", \"checkpoint\"}}; "
            f"got {sorted(draft.keys())}"
        )
    for key in ("setup_log_entry", "checkpoint"):
        if not isinstance(draft[key], str):
            raise PlanValidationError(f"{key} must be a string")
        if not draft[key].strip():
            raise PlanValidationError(f"{key} is empty")
    if not draft["setup_log_entry"].lstrip().startswith("## "):
        raise PlanValidationError(
            "setup_log_entry must start with \"## \" (the date heading)"
        )
    # Checkpoint body should start with a markdown heading (# or ##)
    if not draft["checkpoint"].lstrip().startswith(("#", "##")):
        raise PlanValidationError(
            "checkpoint must start with a markdown heading"
        )
    return None


# ---------------------------------------------------------------------------
# Phase 1 Stage A — vault index, prompt assembly, response parsing
#
# Added alongside the Phase 0 stub (above), which stays callable until Step 6
# replaces it. These are pure module-level functions: no Anthropic call, no
# client state. Step 6's real Planner class calls them; _call_anthropic
# lands in Step 5.
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_STAGE_A_TEMPLATE = _PROMPTS_DIR / "planner-stage-a.md"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_frontmatter(path: Path) -> dict:
    """First 4096 bytes -> leading `---` YAML block -> dict, or {} if the
    block is absent, unparseable, or not a mapping.

    Adapted from the Veronica vault-index reference (conversational
    implementation-notes Component 1) and anvil.brief._split_frontmatter;
    kept local so planner has no sibling-private coupling. The 4096-byte
    cap matches the reference; a frontmatter block larger than that
    truncates and yields {} (acceptable per "no parseable frontmatter
    -> {}").
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return {}
    text = head.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _build_vault_index(
    context_paths: list[str], vault_root: Path
) -> dict[str, dict]:
    """{path_str: frontmatter_dict} for every file under context_paths.

    A path that is a file is indexed directly. A folder is walked with a
    depth-2 cap: a file directly inside the folder is level 1, a file one
    subfolder deep is level 2, anything deeper is excluded. Dotfiles and
    .DS_Store are skipped (the Veronica reference's noise filter). Files
    with no parseable frontmatter map to {} -- present, not skipped.
    """
    vault_root = Path(vault_root)
    index: dict[str, dict] = {}
    for raw in context_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = vault_root / p
        if p.is_file():
            index[str(p)] = _parse_frontmatter(p)
            continue
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if f.name.startswith(".") or f.name == ".DS_Store":
                    continue
                if len(f.relative_to(p).parts) > 2:
                    continue
                index[str(f)] = _parse_frontmatter(f)
    return index


def _read_brief_md(state) -> str:
    """Read the brief markdown from state.brief_path; "" if unreadable.

    v3 Phase 0 Step 4 (V3P0-6): extracted so the candidate-block-size
    computation and the prompt assemblers share one never-raise read.
    """
    try:
        return Path(state.brief_path).read_text(encoding="utf-8")
    except OSError:
        return ""


# v3 Phase 1c Step 2 (V3P1C-2): narrowed planner-side caching. The brief
# block sits at the prefix of both the Stage A and Stage B user prompts
# (`## Build brief\n\n<brief>\n…\n</brief>\n…`) and is byte-stable across every
# step of a build, so `[system + brief]` is a stable cacheable prefix.
# Anthropic's 1024-token cache minimum is on the CUMULATIVE prefix — the
# 3479-token system prompt alone clears it (Step2C-F1) — so the brief caches
# even though the block itself is < 1024 tokens. Splitting the user content
# adds a second cache_control breakpoint after the brief (2 total, under the
# 4-breakpoint limit); cache_read then fires on every subsequent same-model
# call within the 5-min TTL (Stage B reads what Stage A created; multi-step
# tasks read across steps).
_BRIEF_CLOSE_TAG = "</brief>"


def _split_user_for_brief_cache(user_text: str) -> "list[dict] | str":
    """Split an assembled user prompt into a 2-block content list with
    cache_control:ephemeral on the brief-prefix block, for Anthropic prompt
    caching of the (stable, prefix-positioned) brief.

    block1 = everything through the brief's closing `</brief>` tag (cached);
    block2 = the remainder. `block1["text"] + block2["text"]` is byte-identical
    to `user_text` — the model sees the same prompt; only the request structure
    changes (cache_control is request metadata, not content), so the prompt
    TEXT stays byte-identical (criterion 5).

    No-op contract: returns `user_text` UNCHANGED (a bare string) when the
    `</brief>` marker is absent (the Stage C artefacts prompt, or an empty
    prompt) so those calls pass through uncached. Assumes the brief markdown
    contains no literal `</brief>` tag — true for ANVIL briefs, where
    `<brief>…</brief>` is the template's own wrapper, so the FIRST `</brief>`
    is unambiguously the brief block's close."""
    idx = user_text.find(_BRIEF_CLOSE_TAG)
    if idx == -1:
        return user_text
    split = idx + len(_BRIEF_CLOSE_TAG)
    prefix, rest = user_text[:split], user_text[split:]
    if not prefix or not rest:
        return user_text
    return [
        {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": rest},
    ]


# v3 Phase 1c Step 3 (V3P1C-3): comparator option-(b) — the historical
# baseline. The canary's silent-miss comparator needs a ground-truth Opus
# Stage A selection; Phase 1b ran a live parallel Opus call per canary
# (option-a, ~$0.083 each). Option-(b) looks that selection up from a prior
# real sweep instead, dropping the parallel cost. The recorded
# planner.stage_a.parsed originally carried paths_returned (a COUNT) only
# (Step3C-F1) — so the V3P1C-3 baseline was reconstructed: paths_returned == 0
# → ∅; paths_returned > 0 → None (can't reconstruct the set from a count).
# v3 Phase 2a (V3P2A-2) discharged Step3C-F3 by recording selected_paths (the
# list); v3 Phase 2c (V3P2C-2, Q-A4) switches lookup() to read selected_paths
# directly (rich-context-ready), keeping the count reconstruction only as the
# legacy fallback for pre-Phase-2a baseline DBs (e.g. the Phase 1b exit-sweep).
_BASELINE_MODE_SUFFIXES = ("-mock", "-real", "-unknown")


def _base_task_from_run_id(run_id: "str | None") -> "str | None":
    """Strip the mode suffix from a run_id → the mode-independent task label.

    `"T1-doc-edit-mock"` → `"T1-doc-edit"`. The historical baseline is always
    the `-real` run regardless of the current mode, so the caller appends
    `-real` to look it up. Returns None for an empty run_id."""
    if not run_id:
        return None
    for suf in _BASELINE_MODE_SUFFIXES:
        if run_id.endswith(suf):
            return run_id[: -len(suf)]
    return run_id


class HistoricalBaselineProvider:
    """v3 Phase 1c Step 3 (V3P1C-3): comparator option-(b) baseline source.

    Wraps a prior sweep's DuckDB (default the Phase 1b exit-sweep) and returns
    the recorded Opus Stage A selection for a (task, step). Never raises —
    a missing/broken DB, a missing row, or an unreconstructable selection all
    return None so the caller falls back to the live parallel-Opus baseline."""

    def __init__(self, db_path: "Path | str | None") -> None:
        self._db_path = Path(db_path) if db_path else None
        # v3 Phase 3 3b (β-i): cache for corpus_distinct_paths(). None = not yet
        # computed; an empty frozenset is a valid computed result (empty corpus).
        self._corpus_cache: "frozenset[str] | None" = None

    def lookup(self, task_id: str, step_idx: int) -> "list[str] | None":
        """Return the recorded baseline selection for (task_id, step_idx), or
        None if it can't be determined (→ caller uses the parallel call).

        v3 Phase 2c Step 2 (V3P2C-2, Q-A4 discharge): prefer `selected_paths` —
        the selection LIST recorded on every `planner.stage_a.parsed` event
        since Phase 2a (V3P2A-2). Read directly, so a non-empty baseline
        reconstructs EXACTLY (rich-context-ready for Phase 2d), not just ∅
        inferred from a count. Fall back to the legacy `paths_returned`
        reconstruction (Step3C-F1: 0 → []; > 0 → None) for baseline DBs that
        predate Phase 2a recording — the default `ANVIL_HISTORICAL_BASELINE_DB`
        still points at the Phase 1b exit-sweep, which carries `paths_returned`
        but not `selected_paths`. Empty-context behaviour is identical on both
        paths (both → []); the switch is forward-readiness, not a behaviour
        change on the empty-context corpus. Never raises."""
        if not self._db_path or not self._db_path.is_file():
            return None
        try:
            import duckdb
            con = duckdb.connect(str(self._db_path), read_only=True)
            try:
                row = con.execute(
                    "SELECT json_extract(data, '$.selected_paths'), "
                    "CAST(json_extract(data, '$.paths_returned') AS BIGINT) "
                    "FROM events WHERE kind = 'planner.stage_a.parsed' "
                    "AND run_id = ? AND mode = 'real' AND step_idx = ? LIMIT 1",
                    [f"{task_id}-real", step_idx],
                ).fetchone()
            finally:
                con.close()
            if row is None:
                return None
            selected_paths_json, paths_returned = row[0], row[1]
            # Phase 2a+ baseline: selected_paths recorded directly (V3P2A-2).
            # json_extract returns the JSON array text ('[]' or '["a/b.md",…]')
            # for a recorded list, or SQL NULL (→ Python None) on a legacy DB
            # whose `parsed` events lack the key.
            if selected_paths_json is not None:
                parsed = json.loads(selected_paths_json)
                if isinstance(parsed, list):
                    return parsed  # [] or non-empty — exact, rich-context-ready
                # Defensive (partial-migration / a JSON-null value): fall through
                # to the legacy count reconstruction below.
            # Legacy fallback — Phase 1a/1b DBs predate selected_paths recording.
            if paths_returned == 0:
                return []
            return None  # rich-context legacy: can't reconstruct a list from a count
        except Exception:  # noqa: BLE001 — never-raise contract
            return None

    def corpus_distinct_paths(self) -> "frozenset[str]":
        """v3 Phase 3 3b (β-i, Rev B §B.2): the cross-row baseline vocabulary —
        the union of distinct Opus `selected_paths` across the WHOLE historical
        corpus (every real-mode `stage_a_selections` row, no task filter; the
        historical DB *is* the corpus). This is what the comparator's per-corpus
        K=2 distinctness check (`anvil/events.py:398`) grades against, so a
        single-path baseline is no longer judged in isolation (which trips
        `vacuous-uniform`) but against the corpus vocabulary.

        Cached on first call — the corpus is fixed at sweep-start. An empty /
        missing / broken DB, or one whose schema lacks `stage_a_selections`
        (e.g. the Phase 1b exit-sweep, or a minimal events-only DB), returns an
        empty frozenset; the caller decides what an empty corpus means (it
        degrades the K-check to the single-row default — today's behaviour).
        Never raises (mirrors `lookup`)."""
        if self._corpus_cache is not None:
            return self._corpus_cache
        result: "frozenset[str]" = frozenset()
        if self._db_path and self._db_path.is_file():
            try:
                import duckdb
                con = duckdb.connect(str(self._db_path), read_only=True)
                try:
                    rows = con.execute(
                        "SELECT DISTINCT path FROM ("
                        "SELECT UNNEST(CAST(selected_paths AS VARCHAR[])) AS path "
                        "FROM stage_a_selections WHERE mode = 'real')"
                    ).fetchall()
                    result = frozenset(r[0] for r in rows if r[0] is not None)
                finally:
                    con.close()
            except Exception:  # noqa: BLE001 — never-raise contract
                result = frozenset()
        self._corpus_cache = result
        return result


def _assemble_stage_a_prompt(
    brief, state, step_idx: int, vault_index: dict[str, dict]
) -> str:
    """Load planner-stage-a.md and substitute its placeholders.

    Substitution is str.replace per placeholder in a fixed order, NOT
    str.format: the template plus the YAML vault-index block contain
    literal { } braces that would break str.format. Do not "simplify"
    this to .format.

    {VOICE_SPEC} is not handled here. It is a system-prompt concern
    (Step 2 / Step 6 Planner.__init__); Stage A's user-prompt template
    has no {VOICE_SPEC} token. system prompt = system= arg; this
    template = user= arg.

    {BRIEF_MARKDOWN} source is state.brief_path (the Brief object carries
    no raw text; the orchestrator persists the brief path into state).
    Unreadable -> "" (never-raise); the structured step fields are still
    substituted.

    {STATE_JSON} uses state.model_dump_json(indent=2). design Part 2's
    sample said json.dumps(state.dict(), indent=2); pinned pydantic v2
    -> model_dump_json, same precedent as brief.py's dataclass->pydantic
    reconciliation (tracked decision #5).
    """
    template = _STAGE_A_TEMPLATE.read_text(encoding="utf-8")
    step = brief.steps[step_idx]

    try:
        brief_md = Path(state.brief_path).read_text(encoding="utf-8")
    except OSError:
        brief_md = ""

    state_json = state.model_dump_json(indent=2)
    vault_index_yaml = yaml.safe_dump(
        vault_index, default_flow_style=False, sort_keys=True
    )

    subs = [
        ("{BRIEF_MARKDOWN}", brief_md),
        ("{STATE_JSON}", state_json),
        ("{STEP_NUMBER}", str(step.number)),
        ("{STEP_NAME}", step.name),
        ("{STEP_SCOPE_FILES}", ", ".join(step.scope_files)),
        ("{STEP_SCOPE_OPERATIONS}", ", ".join(step.scope_operations)),
        ("{STEP_NOTES}", step.notes or ""),
        ("{CONTEXT_PATHS}", ", ".join(str(c) for c in brief.context_paths)),
        ("{VAULT_INDEX_YAML}", vault_index_yaml),
    ]
    out = template
    for token, value in subs:
        out = out.replace(token, value)

    # Phase 2 Step 6 (decision #18 layer 2): append a
    # [disk-reconciliation-note] block when the brief carries
    # parse_warnings for THIS step. Stage B sees this through the
    # Stage A->B handoff and may surface the reconciliation in
    # escalation_triggers. The Stage A template itself is not
    # modified; the block is appended at assembly time, same posture
    # as Phase 1's {PRIOR_STEP_BLOCK} rendering. The block is omitted
    # entirely when no warning applies to the current step.
    relevant = [
        w for w in getattr(brief, "parse_warnings", []) or []
        if w.get("step_number") == step.number
    ]
    if relevant:
        lines = []
        for w in relevant:
            cm = w.get("closest_match")
            cm_text = f"'{cm}'" if cm else "(no single close match found)"
            lines.append(
                f"[disk-reconciliation-note] Brief step {w['step_number']} "
                f"references '{w['path']}'; this path does not exist at "
                f"target_repo_path. Closest match on disk: {cm_text}. "
                "The Coder will reconcile at execute time; you may want "
                "to flag this in escalation_triggers."
            )
        out = out.rstrip() + "\n\n" + "\n".join(lines) + "\n"

    return out


def _parse_stage_a_response(
    text: str, vault_index: dict[str, dict], step_idx: int | None = None
) -> list[str]:
    """Newline-split, strip, drop empties, filter to paths in vault_index
    (hallucination guard), then dedupe preserving first occurrence.

    The filter runs before the dedupe so the dedupe target is the
    surviving in-index set, not the raw response.

    v3 Phase 0 Step 3 (V3P0-5): a non-empty line that is not in
    vault_index is dropped (a hallucinated / out-of-index path); this was
    silent before. Each such drop now emits one `stage_a.parser_drop`
    event carrying the dropped path + step_idx, making the existing
    behaviour observable. The emit is gated on `step_idx is not None`:
    plan_step always supplies it, but the bare-function unit tests call
    without a step (and without an active events run), so gating keeps
    them from writing to the unknown-run sentinel.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        p = line.strip()
        if not p:
            continue
        if p not in vault_index:
            if step_idx is not None:
                _events.emit(
                    "stage_a.parser_drop",
                    {"step_idx": step_idx, "dropped_path": p},
                    step_idx=step_idx,
                )
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Phase 1 Stage B — file loader, prompt assembly, JSON parse, validation
#
# Added alongside the Phase 0 stub and Stage A (above). The Phase 0
# validate_plan_scope (Plan-object -> bool) stays callable; it is the
# orchestrator's Phase 0 path, retired in Step 6 with the stub. The new
# _validate_plan_structure (raw dict -> raise) is the Phase 1 entry point
# and is independent of pydantic so its errors thread cleanly into the
# Step 5 retry prompt. No Anthropic call here; _call_anthropic is Step 5.
# ---------------------------------------------------------------------------

_STAGE_B_TEMPLATE = _PROMPTS_DIR / "planner-stage-b.md"
_TRUNCATION_MARKER = "\n\n[... truncated at 50000 chars]"

_REQUIRED_PLAN_FIELDS = (
    "step_number",
    "step_name",
    "files_to_touch",
    "operations",
    "approach",
    "smoke_test",
    "expected_outcome",
    "commit_message",
    "scope_boundaries",
    "confidence",
    "escalation_triggers",
)
_VALID_CONFIDENCE = {"high", "medium", "low"}


class PlanParseError(Exception):
    """Stage B output was not parseable as a single JSON object. Carries
    the raw text. Subclasses Exception (not PlannerError) so a broad
    `except PlannerError` cannot swallow it; Step 5's retry catches
    (PlanParseError, PlanValidationError)."""


class PlanValidationError(Exception):
    """Stage B output parsed as JSON but failed structural validation (or
    was a malformed escalation block). Same catch contract as
    PlanParseError."""


def _load_files(
    paths: list[str], vault_root: Path, max_chars_per_file: int = 50_000
) -> dict[str, str]:
    """{path: content} for each readable path. Over the cap, content is
    truncated to the cap and the literal design marker is appended.
    Missing or unreadable files are logged and omitted (never raised) --
    Stage A's index can be stale relative to disk by the time Stage B
    runs, so a vanished file degrades the context, it does not abort.
    """
    vault_root = Path(vault_root)
    out: dict[str, str] = {}
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            p = vault_root / p
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as e:
            log.warning(f"[planner] selected file unreadable, omitting: {raw} ({e})")
            continue
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + _TRUNCATION_MARKER
        out[raw] = content
    return out


def _prior_step_block(state, step_idx: int) -> str:
    """design Part 3 {PRIOR_STEP_BLOCK}. step_idx == 0 -> the literal
    first-step string; otherwise a structured block from
    state.steps[step_idx - 1]. coder output is read defensively
    (coder_output then the on-disk coder_result -- decision #6, the
    Step 7 rename is deferred)."""
    if step_idx == 0:
        return "(none — this is the first step)"
    prior = state.steps[step_idx - 1]
    plan_json = json.dumps(prior.plan) if prior.plan else "(no plan persisted)"
    coder_out = (
        getattr(prior, "coder_output", None)
        or getattr(prior, "coder_result", None)
        or "(manual mode — no output captured)"
    )
    return (
        f"Step {prior.n}: {prior.name}\n"
        f"Plan: {plan_json}\n"
        f"Coder output: {coder_out}\n"
        f"Smoke test result: {prior.smoke or '(none)'}\n"
        f"Commit hash: {prior.commit or '(none)'}"
    )


def _assemble_stage_b_prompt(brief, state, step_idx: int, files: dict[str, str]) -> str:
    """Load planner-stage-b.md and substitute placeholders.

    str.replace per placeholder in a fixed order, NOT str.format: the
    template plus the embedded JSON state and vault-file blocks contain
    literal { } braces that would break str.format. Do not "simplify"
    this to .format.

    {VOICE_SPEC} is not handled here (system-prompt concern, Step 6).
    {STATE_JSON} uses model_dump_json (decision #5). {BRIEF_MARKDOWN}
    source is state.brief_path; unreadable -> "" (never-raise).
    """
    template = _STAGE_B_TEMPLATE.read_text(encoding="utf-8")
    step = brief.steps[step_idx]

    try:
        brief_md = Path(state.brief_path).read_text(encoding="utf-8")
    except OSError:
        brief_md = ""

    if files:
        vault_files_blocks = "".join(
            f'<vault_file path="{p}">\n{c}\n</vault_file>\n\n'
            for p, c in files.items()
        )
    else:
        vault_files_blocks = "(none selected)"

    subs = [
        ("{BRIEF_MARKDOWN}", brief_md),
        ("{STATE_JSON}", state.model_dump_json(indent=2)),
        ("{PRIOR_STEP_BLOCK}", _prior_step_block(state, step_idx)),
        ("{VAULT_FILES_BLOCKS}", vault_files_blocks),
        ("{STEP_NUMBER}", str(step.number)),
        ("{STEP_NAME}", step.name),
        ("{STEP_SCOPE_FILES}", ", ".join(step.scope_files)),
        ("{STEP_SCOPE_OPERATIONS}", ", ".join(step.scope_operations)),
        ("{STEP_NOTES}", step.notes or ""),
        ("{CONTEXT_PATHS}", ", ".join(str(c) for c in brief.context_paths)),
    ]
    out = template
    for token, value in subs:
        out = out.replace(token, value)
    return out


def _parse_plan_json(text: str) -> dict:
    """Strip surrounding whitespace, json.loads. Raise PlanParseError on
    JSONDecodeError with the raw text in the message. Markdown fences are
    NOT stripped -- fenced output fails parsing deliberately so the model
    does not drift toward emitting fences."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise PlanParseError(
            f"Stage B output is not valid JSON ({e}). Raw text:\n{text}"
        ) from e


def _validate_plan_structure(plan: dict, brief_step: Step) -> None:
    """The eight design Part 3 checks, in order, raising
    PlanValidationError on the first failure. Returns None on success.

    Operates on the raw dict, not a Plan model -- the caller constructs
    Plan(**plan) only after this passes. brief_step is anvil.brief.Step
    (design's "BriefStep"); its scope is the flat scope_files /
    scope_operations attrs (design's brief_step.scope.files shorthand),
    same as Stage A.
    """
    # 1. Escalation short-circuit.
    if plan.get("escalate") is True:
        if not isinstance(plan.get("reason"), str):
            raise PlanValidationError("escalation missing or non-str reason")
        if not isinstance(plan.get("detail"), str):
            raise PlanValidationError("escalation missing or non-str detail")
        if not isinstance(plan.get("step_number"), int):
            raise PlanValidationError("escalation missing or non-int step_number")
        if "options" in plan and not (
            isinstance(plan["options"], list)
            and all(isinstance(o, str) for o in plan["options"])
        ):
            raise PlanValidationError("escalation options must be list[str]")
        return None

    # 2. Required fields present.
    for name in _REQUIRED_PLAN_FIELDS:
        if name not in plan:
            raise PlanValidationError(f"missing field: {name}")

    # 3. step_number matches the brief step.
    if plan["step_number"] != brief_step.number:
        raise PlanValidationError(
            f"step_number mismatch: plan {plan['step_number']} "
            f"vs brief {brief_step.number}"
        )

    # 4. files_to_touch within declared scope (literal-equal, no globs).
    for path in plan["files_to_touch"]:
        if path not in brief_step.scope_files:
            raise PlanValidationError(f"out-of-scope file: {path}")

    # 5. operations within declared scope.
    for op in plan["operations"]:
        if op not in brief_step.scope_operations:
            raise PlanValidationError(f"out-of-scope operation: {op}")

    # 6. confidence in the allowed set.
    if plan["confidence"] not in _VALID_CONFIDENCE:
        raise PlanValidationError(f"invalid confidence: {plan['confidence']}")

    # 7. escalation_triggers is list[str] (may be empty).
    et = plan["escalation_triggers"]
    if not (isinstance(et, list) and all(isinstance(x, str) for x in et)):
        raise PlanValidationError("escalation_triggers must be list[str]")

    # 8. scope_boundaries is a dict with str in_scope / out_of_scope.
    sb = plan["scope_boundaries"]
    if not (
        isinstance(sb, dict)
        and isinstance(sb.get("in_scope"), str)
        and isinstance(sb.get("out_of_scope"), str)
    ):
        raise PlanValidationError(
            "scope_boundaries must be a dict with str in_scope and out_of_scope"
        )
    return None


def _escalation_block(reason: str, detail: str, step_idx: int) -> dict:
    """design Part 3 escalation shape. No `options` field:
    planner-validation-failure escalations carry no choice for Genco,
    only an abort decision. step_number is 1-indexed (brief) from the
    0-indexed step_idx."""
    return {
        "escalate": True,
        "reason": reason,
        "detail": detail,
        "step_number": step_idx + 1,
    }
