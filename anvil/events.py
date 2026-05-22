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
# Event-kind catalogue (45 kinds)
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
})
assert len(VALID_KINDS) == 45, f"VALID_KINDS count drift: {len(VALID_KINDS)}"

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
) -> dict[str, Any]:
    """Return the five v3 Phase 0 routing-observability fields, ready to
    merge into an event's `data` payload.

    Phase 0 is passive: `route_candidate` mirrors `route_actual` (no
    policy engine selects an alternative), `route_fallback_fired` is
    always False (no fallback paths exist), and `policy_version` is the
    literal placeholder. Wire Phase 1's policy engine here when it lands.
    """
    return {
        "route_candidate": route_actual,
        "route_actual": route_actual,
        "route_fallback_fired": False,
        "policy_version": POLICY_VERSION_PHASE_0,
        "features_seen": _compute_features_seen(
            stage, step_idx, observed_prompt_token_count, context_paths_count
        ),
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
