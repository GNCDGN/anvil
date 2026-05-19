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
- **`_real_write` capture.** `Path.write_text` is captured at module
  import time, before any code uses it. Production code uses the
  captured reference via the `_append_line` helper; tests patch
  `anvil.events._real_write` to inject failure modes. Same shape as
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

# Module-scope capture — production uses _real_write; tests patch this
# attribute to inject failure modes. Captured before any code below
# uses Path.write_text directly. Mirrors vault_ops._real_write (line 29)
# and ssh_ops._real_run (line 17).
_real_write = Path.write_text

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

    Uses `_real_write` indirectly: the existing-content read + concat +
    rewrite pattern is read-modify-write, which is slow but correct
    under the never-raises contract. The Step 1 brief calls out
    "correctness over micro-optimisation"; per-emit cost is trivial
    against the API-call cost it instruments.

    Tests patch `anvil.events._real_write` to inject OSError; this
    helper propagates the exception so `emit`'s outer handler catches
    it and increments `_drop_count`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        content = existing + line + "\n"
    else:
        content = line + "\n"
    _real_write(path, content, encoding="utf-8")
