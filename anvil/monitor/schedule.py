"""ANVIL monitor — the schedule trigger (v5 Phase 1b, item C).

The first trigger type. An in-process poll (run from `main.py`'s loop every
`POLL_INTERVAL_S`) reads `scheduled_tasks` where `status='active'`, fires any
whose `schedule_expr` is due against `now` (the stdlib matcher), writes a
`trigger_log` row (idempotent — keyed by the scheduled fire-time, so a window
fires once), dispatches the wake (the injected `dispatch` callable — Step 2
wires `wake.send_wake`; Step 1 passes a logging stub), and advances
`last_fired`.

**Operator-configured, deterministic** (the Frame): the matcher fires against
the operator's `schedule_expr` rules; it never interprets content. **Never-
raises** (Contract 1): `poll` returns a structured summary; a bad task row
logs + continues, never aborts the loop.

Schedule grammar (Q-B1 lean — minimal stdlib, no `croniter`):
  `@hourly`              — top of every hour
  `@daily HH:MM`         — every day at HH:MM (local)
  `@weekly DOW HH:MM`    — every week on DOW (mon/tue/.../sun) at HH:MM

Missed-trigger recovery (Q-B2): "due" = the most-recent scheduled time <= now
exists AND `last_fired` is older than it. So a fire-time that passed during
downtime fires once on the next tick (not skipped, not repeated).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from anvil.monitor import anvil_ops

log = logging.getLogger("anvil.monitor.schedule")

_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"bad HH:MM: {s}")
    return h, m


def most_recent_due(schedule_expr: str, now: datetime) -> datetime | None:
    """The most recent scheduled datetime <= now for `schedule_expr`, or None
    if the expression is unparseable. Raises nothing the caller can't handle —
    a ValueError on a bad expr is caught by `is_due`/`poll`."""
    parts = schedule_expr.strip().split()
    kind = parts[0]
    if kind == "@hourly":
        return now.replace(minute=0, second=0, microsecond=0)
    if kind == "@daily":
        h, m = _parse_hhmm(parts[1])
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return cand if cand <= now else cand - timedelta(days=1)
    if kind == "@weekly":
        dow = _DOW[parts[1].lower()[:3]]
        h, m = _parse_hhmm(parts[2])
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # step back to the most recent matching weekday at/<= now
        delta = (now.weekday() - dow) % 7
        cand = cand - timedelta(days=delta)
        if cand > now:
            cand = cand - timedelta(days=7)
        return cand
    raise ValueError(f"unsupported schedule_expr: {schedule_expr!r}")


def is_due(schedule_expr: str, now: datetime, last_fired: str | None) -> bool:
    """True if `schedule_expr` has a most-recent-due time <= now that is newer
    than `last_fired` (ISO string, or None for never-fired)."""
    try:
        mrd = most_recent_due(schedule_expr, now)
    except (ValueError, KeyError, IndexError) as exc:
        log.warning("unparseable schedule_expr %r: %s", schedule_expr, exc)
        return False
    if mrd is None:
        return False
    if last_fired is None:
        return True
    try:
        lf = datetime.fromisoformat(last_fired)
    except ValueError:
        return True  # unparseable last_fired → treat as never-fired (fire once)
    return lf < mrd


def _log_stub(task: dict) -> dict:
    """The Step 1 wake dispatcher stub — logs the routing decision. Step 2
    replaces it with `wake.send_wake`."""
    log.info("schedule: would wake — task=%s brief=%s confirm=%s",
             task.get("task_id"), task.get("brief_path"), task.get("confirm_mode"))
    return {"ok": True, "stub": True}


def poll(db_path: str, now: datetime | None = None, dispatch=_log_stub) -> dict:
    """One schedule poll: fire every due active task. Never-raises. Returns
    {"ok", "fired": [task_id, ...], "checked": int}. `dispatch(task)` is the
    wake handler (Step 2: `wake.send_wake`)."""
    now = now or datetime.now()
    fired: list[str] = []
    try:
        res = anvil_ops.list_scheduled_tasks(db_path, status="active")
        if not res["ok"]:
            return {"ok": False, "error": res["error"]}
        tasks = res["result"]
        for task in tasks:
            expr = task.get("schedule_expr", "")
            if not is_due(expr, now, task.get("last_fired")):
                continue
            mrd = most_recent_due(expr, now)
            trigger_id = f"sched:{task['task_id']}@{mrd.isoformat()}"
            logged = anvil_ops.log_trigger(
                db_path, trigger_id, source="schedule",
                received_at=now.isoformat(), disposition="firing",
                notes=f"brief={task.get('brief_path')} confirm={task.get('confirm_mode')}",
            )
            if not logged["ok"] or not logged["inserted"]:
                # already logged this window (idempotent) → skip the re-fire
                continue
            try:
                d = dispatch(task)
            except Exception as exc:  # never-raises: a bad dispatch logs + continues
                log.warning("schedule dispatch failed for %s: %s", task["task_id"], exc)
                d = {"ok": False, "error": str(exc)}
            anvil_ops.update_trigger_disposition(
                db_path, trigger_id,
                disposition="fired" if d.get("ok") else "dispatch-failed",
                fired_at=now.isoformat(),
            )
            anvil_ops.mark_task_fired(db_path, task["task_id"], now.isoformat())
            fired.append(task["task_id"])
        return {"ok": True, "fired": fired, "checked": len(tasks)}
    except Exception as exc:  # never-raises
        log.warning("schedule poll failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"schedule poll: {type(exc).__name__}: {exc}"}
