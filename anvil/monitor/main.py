"""anvil-monitor entry point — the VPS-resident always-on monitor (v5 Phase 1a).

Phase 1a is IDLE: the service initializes the operations ledger, logs that it
started, and loops doing nothing. No triggers, no Telegram, no Sentry, no
model calls, no vault access (the trigger-dispatch Boundary contract). The
schedule trigger (1b) and the Sentry trigger + mode-guard (1c) are the first
consumers of the loop body.

Run modes:
  python3 -m anvil.monitor.main              # the idle service loop (systemd)
  python3 -m anvil.monitor.main --selfcheck  # init the ledger, verify, log, exit 0
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from datetime import datetime

from anvil.monitor import anvil_ops, running_builds, schedule, sentry_poller, wake

DEFAULT_DB = os.environ.get("ANVIL_OPS_DB_PATH", "state/anvil-ops.db")
POLL_INTERVAL_S = int(os.environ.get("ANVIL_MONITOR_POLL_S", "60"))
# The Sentry poll runs on its own (slower) cadence — 5-min lean (Q-C2). Gated
# on SENTRY_PROJECT: with no Sentry account provisioned (brief Amendment 1 —
# the live probe is deferred), the loop never calls out. The code ships +
# unit-tested; flipping SENTRY_PROJECT on (Phase 2) activates it with no change.
SENTRY_POLL_INTERVAL_S = int(os.environ.get("ANVIL_SENTRY_POLL_S", "300"))
SENTRY_ENABLED = bool(os.environ.get("SENTRY_PROJECT"))
_EXPECTED_TABLES = {"scheduled_tasks", "trigger_log", "running_builds"}

log = logging.getLogger("anvil.monitor")


def _dispatch_wake(task: dict) -> dict:
    """The schedule poll's wake dispatcher — sends the [ANVIL] Wake to the
    operator's Telegram (explicit-mode; the operator relays `go <path>` to the
    Mac wake-listener). Never-raises (wake.send_wake returns a structured
    result)."""
    return wake.send_wake(task)


def _dispatch_sentry(issue: dict) -> dict:
    """The Sentry poll's route handler — sends the [ANVIL] Sentry notice to the
    operator's Telegram (explicit; the operator decides whether to investigate).
    Never-raises."""
    return sentry_poller._notify(sentry_poller._alert_text(issue))


def _escalate_stale(text: str) -> dict:
    """The mode-guard's stale-build escalation — a one-shot Telegram notice (the
    monitor's existing stdlib send). The dispatch gate calls this once per stale
    incident (gated by the deferred-stale log_trigger insert)."""
    return sentry_poller._notify(text)


def _configure_logging() -> None:
    # v5 Phase 1b (1a Amendment 4 fix): stdout only. systemd's
    # StandardOutput=append owns the log file; adding a FileHandler too
    # double-wrote every line. Under systemd, stdout flows to the file;
    # run locally, it goes to the terminal.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def selfcheck(db_path: str = DEFAULT_DB) -> int:
    """Init the ledger, verify the three tables, log started, exit 0. The Step
    2 smoke + the Step 3/4 VPS idle-service verification use this."""
    _configure_logging()
    init = anvil_ops.init_db(db_path)
    if not init["ok"]:
        log.error("anvil-monitor selfcheck: init_db failed: %s", init["error"])
        return 1
    tbls = anvil_ops.tables(db_path)
    if not tbls["ok"] or not _EXPECTED_TABLES.issubset(set(tbls["result"])):
        log.error("anvil-monitor selfcheck: tables missing: %s", tbls)
        return 1
    log.info(
        "anvil-monitor selfcheck OK (db=%s, tables=%s) — Phase 1a idle, no triggers wired",
        db_path, sorted(_EXPECTED_TABLES),
    )
    return 0


class _Idle:
    def __init__(self) -> None:
        self.running = True

    def stop(self, *_: object) -> None:
        self.running = False


def run(db_path: str = DEFAULT_DB) -> int:
    """The idle main loop. Phase 1a: init the ledger, log started, loop doing
    nothing until SIGTERM/SIGINT. No triggers fire in 1a."""
    _configure_logging()
    init = anvil_ops.init_db(db_path)
    if not init["ok"]:
        log.error("anvil-monitor: init_db failed: %s — exiting", init["error"])
        return 1
    state = _Idle()
    signal.signal(signal.SIGTERM, state.stop)
    signal.signal(signal.SIGINT, state.stop)
    log.info(
        "anvil-monitor started (db=%s) — Phase 1c: schedule + sentry(%s) triggers",
        db_path, "on" if SENTRY_ENABLED else "off (no SENTRY_PROJECT)",
    )
    # Tick every 1s so SIGTERM/SIGINT stops the service promptly (a 60s sleep
    # would resume after the signal under PEP 475, delaying clean shutdown).
    # The schedule poll runs every POLL_INTERVAL_S; the Sentry poll every
    # SENTRY_POLL_INTERVAL_S. (Step 2 wires the running_builds mode-guard read
    # in front of both dispatches.)
    tick = 0
    sentry_since: datetime | None = None
    while state.running:
        time.sleep(1)
        tick += 1
        if tick % POLL_INTERVAL_S == 0:
            res = schedule.poll(db_path, dispatch=_dispatch_wake,
                                guard=running_builds.mode_guard_check, on_stale=_escalate_stale)
            if res.get("fired"):
                log.info("schedule poll fired: %s", res["fired"])
            if res.get("deferred"):
                log.info("schedule poll deferred (build active): %s", res["deferred"])
        if SENTRY_ENABLED and tick % SENTRY_POLL_INTERVAL_S == 0:
            now = datetime.now()
            sres = sentry_poller.poll(db_path, since=sentry_since, now=now,
                                      dispatch=_dispatch_sentry,
                                      guard=running_builds.mode_guard_check, on_stale=_escalate_stale)
            sentry_since = now
            if sres.get("routed"):
                log.info("sentry poll routed: %s", sres["routed"])
            if sres.get("deferred"):
                log.info("sentry poll deferred (build active): %s", sres["deferred"])
            elif not sres.get("ok"):
                log.warning("sentry poll error: %s", sres.get("error"))
    log.info("anvil-monitor stopped cleanly")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="anvil-monitor")
    p.add_argument("--selfcheck", action="store_true",
                   help="init the ledger, verify tables, log, exit 0 (no loop)")
    p.add_argument("--db", default=DEFAULT_DB, help="operations-ledger SQLite path")
    args = p.parse_args(argv)
    return selfcheck(args.db) if args.selfcheck else run(args.db)


if __name__ == "__main__":
    sys.exit(main())
