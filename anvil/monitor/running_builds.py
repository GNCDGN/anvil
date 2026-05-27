"""ANVIL monitor — the mode-guard (v5 Phase 1c, item F).

The monitor reads ``running_builds`` before routing any trigger: if a build is
active Mac-side, the trigger defers (the poll's ``guard`` hook) so the monitor
never wakes the Mac into a second concurrent build. The active signal is
written by the **Mac orchestrator over SSH** — the reverse channel (Q-C1:
``ssh_ops.ssh_run`` + ``VPS_HOST``/``VPS_USER``; NOT Telegram — the 409
single-consumer constraint, the Mac holds the bot's getUpdates) — via this
module's CLI:

    python3 -m anvil.monitor.running_builds mark-active   <run_id> <brief_path>
    python3 -m anvil.monitor.running_builds mark-complete <run_id>

**Fail-closed staleness** (design Q8 / Q-C5): an ``active`` row whose
``started_at`` is older than the staleness window is almost certainly a build
whose completion SSH-write never landed (a crash, a dropped connection). The
mode-guard reports it ``stale`` so the dispatch gate escalates ONCE (to the
operator) rather than blocking new triggers forever OR silently routing into a
maybe-live build. The operator clears the row; the deferred trigger then fires.

**Never-raises** (Contract 1): wraps ``anvil_ops``, returns structured results;
a read error is reported ``active + stale`` (fail-closed — never wake into
uncertainty, and surface it).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta

from anvil.monitor import anvil_ops

log = logging.getLogger("anvil.monitor.running_builds")

DEFAULT_DB = os.environ.get("ANVIL_OPS_DB_PATH", "state/anvil-ops.db")


def _staleness_hours() -> float:
    try:
        return float(os.environ.get("ANVIL_BUILD_STALENESS_H", "6"))
    except ValueError:
        return 6.0


def mode_guard_check(
    db_path: str, *, now: datetime | None = None, staleness_hours: float | None = None
) -> dict:
    """The dispatch gate's read. Returns
    ``{"ok", "active": bool, "stale": bool, "result": <row or None>}``.
    Never-raises; a read error is reported ``active + stale`` (fail-closed)."""
    now = now or datetime.now()
    staleness_hours = _staleness_hours() if staleness_hours is None else staleness_hours
    res = anvil_ops.active_build(db_path)
    if not res["ok"]:
        log.warning("mode_guard_check: active_build read failed: %s", res.get("error"))
        return {"ok": False, "active": True, "stale": True, "result": None,
                "error": res.get("error")}
    if not res["active"]:
        return {"ok": True, "active": False, "stale": False, "result": None}
    row = res["result"]
    stale = False
    try:
        started = datetime.fromisoformat(row["started_at"])
        stale = (now - started) > timedelta(hours=staleness_hours)
    except (ValueError, TypeError, KeyError):
        stale = True  # unparseable started_at → fail-closed escalate
    return {"ok": True, "active": True, "stale": stale, "result": row}


# --- the Mac-invoked CLI (over SSH — the reverse channel) ------------------

def _cli(argv: list[str], db: str | None = None) -> int:
    """``mark-active <run_id> <brief>`` / ``mark-complete <run_id>``. Prints a
    one-line result; exit 0 on ok, 1 on a write failure, 2 on bad args. The
    Mac's ssh_run reads the exit code + output."""
    db = db or DEFAULT_DB
    if not argv:
        print("usage: running_builds (mark-active <run_id> <brief>|mark-complete <run_id>)")
        return 2
    cmd = argv[0]
    now = datetime.now().isoformat()
    anvil_ops.init_db(db)  # idempotent — the CLI may run before the service first boots
    if cmd == "mark-active" and len(argv) >= 2:
        run_id = argv[1]
        brief = argv[2] if len(argv) >= 3 else None
        r = anvil_ops.mark_build_running(db, run_id, started_at=now, brief_path=brief)
        print(f"mark-active {run_id}: ok={r['ok']}")
        return 0 if r["ok"] else 1
    if cmd == "mark-complete" and len(argv) >= 2:
        run_id = argv[1]
        r = anvil_ops.clear_running_build(db, run_id, completed_at=now)
        print(f"mark-complete {run_id}: ok={r['ok']} updated={r.get('updated')}")
        return 0 if r["ok"] else 1
    print(f"bad args: {argv}")
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
