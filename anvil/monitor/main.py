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

from anvil.monitor import anvil_ops

DEFAULT_DB = os.environ.get("ANVIL_OPS_DB_PATH", "state/anvil-ops.db")
POLL_INTERVAL_S = int(os.environ.get("ANVIL_MONITOR_POLL_S", "60"))
_EXPECTED_TABLES = {"scheduled_tasks", "trigger_log", "running_builds"}

log = logging.getLogger("anvil.monitor")


def _configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_path = os.environ.get("LOG_PATH")
    if log_path:
        try:
            handlers.append(logging.FileHandler(log_path))
        except OSError:
            pass  # never-raises: fall back to stdout only
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
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
    log.info("anvil-monitor started (db=%s) — Phase 1a idle: no triggers wired", db_path)
    # Tick every 1s so SIGTERM/SIGINT stops the service promptly (a 60s sleep
    # would resume after the signal under PEP 475, delaying clean shutdown).
    # POLL_INTERVAL_S is the trigger-poll cadence 1b/1c run their poll body on.
    tick = 0
    while state.running:
        time.sleep(1)
        tick += 1
        if tick % POLL_INTERVAL_S == 0:
            # Phase 1a: no-op. 1b wires the schedule poll here; 1c wires the
            # Sentry poll + the running_builds mode-guard read.
            pass
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
