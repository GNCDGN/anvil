"""v2 Phase 1 Step 5 — MockedPlanner / MockedCoder production subclasses.

These are NOT unittest mocks. They are real classes selected at
Orchestrator construction time by `Config.mocked_planner` /
`Config.mocked_coder` (env: `MOCKED_PLANNER=1` / `MOCKED_CODER=1`),
driven by JSON / YAML fixtures keyed on `MOCKED_TASK_ID`. The
calibration framework (Steps 6–7) uses them for framework-only
profiling — same prompts, same validation, same event emission, no
real Anthropic / Claude Code calls.

Design pillars:

- **`MockedPlanner` overrides `_call_anthropic` only.** The Stage A
  inline block in `plan_step`, the `_run_stage_b_with_retry` retry
  loop, and `draft_completion_artefacts` (Stage C) all flow through
  `_call_anthropic` unchanged. Prompt assembly, validation, and the
  Step 2 structured event emission all happen for real. The mock
  substitutes the model response with fixture content and emits a
  synthesised `planner.stage_<X>.api_end` carrying token counts from
  an optional `<task>-step<N>.usage.json` sidecar (or zeros if absent).
- **`MockedCoder` overrides `_real_run` only** (introduced in Step 5
  via the inline-subprocess extraction). Pre-flight + Layer 2 git-diff
  still run on real disk; the mock's file-creation side effect
  (driven by `<task>-step<N>.coder-effect.yaml`) ensures the post-call
  `_git_files_touched` sees the expected files. Without that side
  effect the calibration's framework-overhead measurement for the
  Coder is meaningless (per notes.md Step 4 outcome finding 3).
- **Determinism.** With `MOCKED_PLANNER_JITTER_MS=0` and
  `MOCKED_CODER_JITTER_MS=0`, two consecutive runs of the same brief
  produce byte-identical `events.jsonl` modulo `ts` and `elapsed_ms`
  (wall-clock fields). Tests assert on the diff-modulo-timestamps.

Fixture layout:
  tests/fixtures/v2-phase-1/mocked-plans/
    <task_id>-step<N>.json                 — Plan or escalation block
    <task_id>-step<N>.usage.json           — optional token-count sidecar
    <task_id>-step<N>.coder-effect.yaml    — file-creation side effect for MockedCoder
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

from anvil import events as _events
from anvil.coder import Coder
from anvil.planner import Planner

# Fixture root. The calibration_runner (Step 6) writes the fixtures into
# `tests/fixtures/v2-phase-1/mocked-plans/` so they're tracked in git
# alongside the source — calibration is reproducible from a clean
# checkout. ANVIL_MOCKED_FIXTURE_ROOT overrides for tests that want a
# hermetic tmp_path.
_DEFAULT_FIXTURE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "v2-phase-1" / "mocked-plans"
)


def _fixture_root() -> Path:
    """Resolve the fixture root. Env override > default."""
    override = os.environ.get("ANVIL_MOCKED_FIXTURE_ROOT", "").strip()
    return Path(override) if override else _DEFAULT_FIXTURE_ROOT


def _task_id() -> str:
    """Read MOCKED_TASK_ID; raise if absent (the mock is unusable without it)."""
    tid = os.environ.get("MOCKED_TASK_ID", "").strip()
    if not tid:
        raise RuntimeError(
            "MockedPlanner/MockedCoder require MOCKED_TASK_ID env to be set; "
            "the calibration_runner sets it before invoking anvil"
        )
    return tid


# ---------------------------------------------------------------------------
# MockedPlanner
# ---------------------------------------------------------------------------

class MockedPlanner(Planner):
    """Planner subclass: `_call_anthropic` returns fixture content +
    emits a synthesised `planner.stage_<X>.api_end` with token counts
    from the paired `.usage.json` sidecar (or zeros if absent).

    Prompt assembly, validation, retry, and escalation all run for
    real. The only substitution is the model call.
    """

    def _call_anthropic(self, system, user, timeout, *, step, stage):
        # `step` is the 1-based step number the Planner uses for log
        # lines; convert to 0-based step_idx for events. Stage C
        # passes step=0; treat that as step_idx=None (run-level emit).
        if step == 0:
            step_idx = None
        else:
            step_idx = step - 1

        # Jitter — simulated latency. Defaults to 0 for deterministic
        # calibration; the runner can dial it up to mimic real Stage B
        # wall-clock for human comparison.
        jitter_ms = int(os.environ.get("MOCKED_PLANNER_JITTER_MS", "0"))
        if jitter_ms > 0:
            time.sleep(jitter_ms / 1000.0)

        task_id = _task_id()
        root = _fixture_root()
        step_token = step_idx if step_idx is not None else "C"
        fixture_path = root / f"{task_id}-step{step_token}.json"
        # Stage C missing-fixture handling (v2 Phase 1 Step 6 prep):
        # Tasks that reach orchestrator step 9 invoke
        # `draft_completion_artefacts` which calls `_call_anthropic`
        # with stage="C". If no `<task>-stepC.json` fixture exists,
        # return "" — Planner.draft_completion_artefacts treats an
        # empty response as the completion-artefacts-draft-failed
        # escalation path, the same code path real-mode hits on an
        # API hiccup. Preserves framework-profile fidelity (the
        # operations view still sees the Stage C call happened via
        # the api_end emit below) without forcing every calibration
        # task to author a Stage C artefacts fixture. Stage A/B
        # missing-fixture still raises — those are programming
        # errors, not gracefully-degraded execution paths.
        if stage == "C" and not fixture_path.is_file():
            text = ""
        else:
            if not fixture_path.is_file():
                raise RuntimeError(
                    f"MockedPlanner: fixture not found at {fixture_path}"
                )
            text = fixture_path.read_text(encoding="utf-8")

        # Optional token-count sidecar — zeros if absent.
        usage_path = root / f"{task_id}-step{step_token}.usage.json"
        if usage_path.is_file():
            try:
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                usage = {}
        else:
            usage = {}

        # Synthesised api_end. Same shape as the production wrapper's
        # emit at planner.py:_call_anthropic.
        stage_key = stage.lower()
        observed_input_tokens = int(usage.get("input_tokens", 0))
        cache_creation = int(usage.get("cache_creation_input_tokens", 0))
        # v3 Phase 1b Step 3 (V3P0-3 parallel-wire): mirror the production
        # wrapper's restructure — decide the route (pre-"call", stashing
        # _current_route_decision for plan_step's canary baseline check), then
        # build the routing dict. data.model sources from decision.route_actual
        # (Step3-F1 "model = ran"): canary → Haiku, shadow/placeholder → Opus.
        # The inherited plan_step / Stage-C path stashes _current_step_idx +
        # _current_context_paths_count + _current_lint_result before this
        # override runs (MockedPlanner overrides only _call_anthropic).
        decision = self._decide_route(stage)
        routing = self._routing_for(stage, decision, observed_input_tokens)
        _events.emit(
            f"planner.stage_{stage_key}.api_end",
            {
                "step_idx": step_idx,
                "model": self._api_model(stage, decision),
                "input_tokens": observed_input_tokens,
                "output_tokens": int(usage.get("output_tokens", 0)),
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0)),
                "duration_ms": jitter_ms,
                "ok": True,
                **routing,
                # v3 Phase 0 Step 4 (V3P0-6): cache-family diagnostics.
                # Inherited _cache_diag_fields reads the stashes set by the
                # inherited plan_step / _run_stage_b_with_retry and the
                # per-run TTL state. vault_index_hit tracks naturally
                # because the mock still runs the real _build_vault_index
                # via inherited plan_step; seconds_since_cache_creation is
                # null unless a usage sidecar reports cache_creation > 0.
                **self._cache_diag_fields(stage, cache_creation),
            },
            step_idx=step_idx,
        )
        # v3 Phase 0 Step 2 (V3P0-3): paired shadow.decision after the
        # synthesised api_end, so mock-mode Planner events get a shadow
        # row too (criterion 1: every Planner stage event in a sweep).
        _events.emit_shadow_decision(
            stage=stage,
            step_idx=step_idx,
            features_seen=decision.decision_basis,
            actual_route_taken=decision.route_actual,
            shadow_route_candidate=decision.route_candidate,
            policy_version=self._policy.policy_version,
        )

        return text

    def _stage_a_canary_baseline(self, prompt, step_idx):
        """v3 Phase 1b Step 3 (V3P0-3 parallel-wire): mock the canary
        parallel-Opus baseline by reading the SAME Stage A fixture the primary
        call read (fixtures are model-agnostic), so the baseline selection
        equals the primary → silent_miss == 0 deterministically. Emits
        planner.stage_a.canary_baseline.api_end (sidecar usage) so the mock
        sweep exercises the cost-ledger wiring end-to-end. Never raises."""
        baseline_model = self._model_for_stage("A")
        try:
            task_id = _task_id()
            root = _fixture_root()
            fixture_path = root / f"{task_id}-step{step_idx}.json"
            text = (
                fixture_path.read_text(encoding="utf-8")
                if fixture_path.is_file() else ""
            )
            usage = {}
            usage_path = root / f"{task_id}-step{step_idx}.usage.json"
            if usage_path.is_file():
                try:
                    usage = json.loads(usage_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    usage = {}
            ns = SimpleNamespace(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cache_creation_input_tokens=int(
                    usage.get("cache_creation_input_tokens", 0)),
                cache_read_input_tokens=int(
                    usage.get("cache_read_input_tokens", 0)),
            )
            self._emit_canary_baseline(step_idx, baseline_model, ns, 0, ok=True)
            return text
        except Exception as e:  # noqa: BLE001 — never-raise
            self._emit_canary_baseline(
                step_idx, baseline_model, None, 0, ok=False, error=str(e)[:300])
            return ""


# ---------------------------------------------------------------------------
# MockedCoder
# ---------------------------------------------------------------------------

class MockedCoder(Coder):
    """Coder subclass: `_real_run` materialises files on disk per the
    `<task>-step<N>.coder-effect.yaml` fixture and returns a fabricated
    `subprocess.CompletedProcess`. Pre-flight + Layer 2 scope verify
    still run.
    """

    def _real_run(self, cmd, prompt, target_repo_path):
        # Jitter.
        jitter_ms = int(os.environ.get("MOCKED_CODER_JITTER_MS", "0"))
        if jitter_ms > 0:
            time.sleep(jitter_ms / 1000.0)

        # step_idx is stashed by execute_step (Step 5 wiring at coder.py
        # entry); default to 0 if missing so tests calling _real_run
        # directly still work.
        step_idx = getattr(self, "_current_step_idx", 0) or 0

        task_id = _task_id()
        root = _fixture_root()
        effect_path = root / f"{task_id}-step{step_idx}.coder-effect.yaml"

        # Materialise files. Missing fixture → no file-creation side
        # effect (the calibration runner will surface this as a
        # files_touched=[] row that the harness's scope_verify
        # captures; same shape as a Coder run that touched nothing).
        files_created: list[str] = []
        if effect_path.is_file():
            try:
                effect = yaml.safe_load(effect_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                effect = {}
            for spec in effect.get("files", []) or []:
                rel = spec.get("path") if isinstance(spec, dict) else None
                content = spec.get("content", "") if isinstance(spec, dict) else ""
                if not rel:
                    continue
                full = Path(target_repo_path) / rel
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")
                files_created.append(rel)

        # Fabricated CompletedProcess. returncode=0 keeps the
        # orchestrator's exit-code escalation silent; the test harness
        # can vary the fixture to inject failure modes (e.g. T6's
        # future Coder-fail fixture).
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=(
                f"[anvil-coder] mocked execution for "
                f"{task_id}-step{step_idx}\n"
                f"[anvil-coder] files created: {', '.join(files_created)}\n"
            ),
            stderr="",
        )
