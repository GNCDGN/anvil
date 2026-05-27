"""ANVIL operations ledger — the always-on monitor's persistent state.

v5 Phase 1a (item B). A standalone SQLite store for the VPS-resident
`anvil-monitor.service`: what is scheduled (`scheduled_tasks`), every trigger
received and its disposition (`trigger_log`), and what is currently building
Mac-side (`running_builds` — the mode-guard surface).

**Never-raises** (connector-pattern.md Contract 1): every public function
returns a structured result — `{"ok": True, ...}` on success, `{"ok": False,
"error": "<reason>"}` on any failure. No exception escapes; the caller
inspects `["ok"]`, never a try/except.

**Atomicity is the sqlite3 transaction (commit-last)** — the Q-A3
disposition. Each write runs inside a `with conn:` block, which commits on
clean exit and rolls back on exception, so a crashed write never leaves a
torn row. This is the right discipline for a SQLite store; the file-replace
(`os.replace`/fsync) pattern is for JSON-blob state (`deploy_history.json`),
not here.

**Boundary contract** (v5 Phase 1 design): the monitor's writes are bounded
to this SQLite (+ Telegram, later). No vault, no Claude Code, no briefs.

**Available-but-not-consumed in 1a:** the schema + helpers ship and are
unit-tested; the schedule trigger (1b) and the Sentry trigger + mode-guard
reads (1c) are the first consumers.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Any

log = logging.getLogger("anvil.monitor.anvil_ops")

# v1 schema foundation (Q-A3): forward-complete for 1b (scheduled_tasks +
# trigger_log) and 1c (running_builds + mode-guard reads) — no ALTER/migration
# in a later sub-build. NOT NULL columns are always supplied by their writer.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id       TEXT PRIMARY KEY,
    schedule_expr TEXT NOT NULL,
    brief_path    TEXT NOT NULL,
    confirm_mode  TEXT NOT NULL DEFAULT 'explicit',
    last_fired    TEXT,
    status        TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS trigger_log (
    trigger_id  TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    received_at TEXT NOT NULL,
    disposition TEXT,
    fired_at    TEXT,
    notes       TEXT
);
CREATE TABLE IF NOT EXISTS running_builds (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    brief_path   TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    completed_at TEXT,
    notes        TEXT
);
"""

_TABLES = ("scheduled_tasks", "trigger_log", "running_builds")


@contextmanager
def _connect(db_path: str):
    """Open a connection with Row factory; close it on exit. Caller wraps
    in the never-raises ladder."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _err(op: str, exc: Exception) -> dict:
    log.warning("anvil_ops %s failed: %s: %s", op, type(exc).__name__, exc)
    return {"ok": False, "error": f"anvil_ops {op}: {type(exc).__name__}: {exc}"}


def init_db(db_path: str) -> dict:
    """Create the three tables if absent (idempotent). Returns {"ok", "tables"}."""
    try:
        with _connect(db_path) as conn:
            with conn:  # transaction: commit-last
                conn.executescript(_SCHEMA)
        return {"ok": True, "tables": list(_TABLES)}
    except Exception as exc:  # never-raises
        return _err("init_db", exc)


def tables(db_path: str) -> dict:
    """List the table names present (for the Step 4 db-init verification)."""
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return {"ok": True, "result": [r["name"] for r in rows]}
    except Exception as exc:
        return _err("tables", exc)


# --- scheduled_tasks (1b consumer) -----------------------------------------

def add_scheduled_task(
    db_path: str,
    task_id: str,
    schedule_expr: str,
    brief_path: str,
    confirm_mode: str = "explicit",
    status: str = "active",
) -> dict:
    try:
        with _connect(db_path) as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO scheduled_tasks "
                    "(task_id, schedule_expr, brief_path, confirm_mode, last_fired, status) "
                    "VALUES (?, ?, ?, ?, "
                    "COALESCE((SELECT last_fired FROM scheduled_tasks WHERE task_id=?), NULL), ?)",
                    (task_id, schedule_expr, brief_path, confirm_mode, task_id, status),
                )
        return {"ok": True, "task_id": task_id}
    except Exception as exc:
        return _err("add_scheduled_task", exc)


def list_scheduled_tasks(db_path: str, status: str | None = "active") -> dict:
    try:
        with _connect(db_path) as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM scheduled_tasks ORDER BY task_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE status=? ORDER BY task_id",
                    (status,),
                ).fetchall()
        return {"ok": True, "result": [dict(r) for r in rows]}
    except Exception as exc:
        return _err("list_scheduled_tasks", exc)


def mark_task_fired(db_path: str, task_id: str, fired_at: str) -> dict:
    try:
        with _connect(db_path) as conn:
            with conn:
                cur = conn.execute(
                    "UPDATE scheduled_tasks SET last_fired=? WHERE task_id=?",
                    (fired_at, task_id),
                )
        return {"ok": True, "updated": cur.rowcount}
    except Exception as exc:
        return _err("mark_task_fired", exc)


# --- trigger_log (1b/1c consumer; idempotent per Q5) -----------------------

def log_trigger(
    db_path: str,
    trigger_id: str,
    source: str,
    received_at: str,
    disposition: str | None = None,
    fired_at: str | None = None,
    notes: str | None = None,
) -> dict:
    """Record a received trigger. Idempotent (INSERT OR IGNORE on the stable
    trigger_id) — the Q5 crash-recovery discipline: a replayed trigger after
    a restart does not double-insert."""
    try:
        with _connect(db_path) as conn:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO trigger_log "
                    "(trigger_id, source, received_at, disposition, fired_at, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (trigger_id, source, received_at, disposition, fired_at, notes),
                )
        return {"ok": True, "trigger_id": trigger_id, "inserted": cur.rowcount == 1}
    except Exception as exc:
        return _err("log_trigger", exc)


def update_trigger_disposition(
    db_path: str,
    trigger_id: str,
    disposition: str,
    fired_at: str | None = None,
    notes: str | None = None,
) -> dict:
    try:
        with _connect(db_path) as conn:
            with conn:
                cur = conn.execute(
                    "UPDATE trigger_log SET disposition=?, fired_at=COALESCE(?, fired_at), "
                    "notes=COALESCE(?, notes) WHERE trigger_id=?",
                    (disposition, fired_at, notes, trigger_id),
                )
        return {"ok": True, "updated": cur.rowcount}
    except Exception as exc:
        return _err("update_trigger_disposition", exc)


def list_triggers(db_path: str, limit: int = 50) -> dict:
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM trigger_log ORDER BY received_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return {"ok": True, "result": [dict(r) for r in rows]}
    except Exception as exc:
        return _err("list_triggers", exc)


# --- running_builds (1c mode-guard) ----------------------------------------

def mark_build_running(
    db_path: str, run_id: str, started_at: str, brief_path: str | None = None
) -> dict:
    try:
        with _connect(db_path) as conn:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO running_builds "
                    "(run_id, started_at, brief_path, status, completed_at, notes) "
                    "VALUES (?, ?, ?, 'active', NULL, NULL)",
                    (run_id, started_at, brief_path),
                )
        return {"ok": True, "run_id": run_id}
    except Exception as exc:
        return _err("mark_build_running", exc)


def clear_running_build(
    db_path: str, run_id: str, completed_at: str, status: str = "completed"
) -> dict:
    try:
        with _connect(db_path) as conn:
            with conn:
                cur = conn.execute(
                    "UPDATE running_builds SET status=?, completed_at=? WHERE run_id=?",
                    (status, completed_at, run_id),
                )
        return {"ok": True, "updated": cur.rowcount}
    except Exception as exc:
        return _err("clear_running_build", exc)


def active_build(db_path: str) -> dict:
    """The mode-guard read (1c): is a build active Mac-side? Returns
    {"ok", "active": bool, "result": <row or None>}."""
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM running_builds WHERE status='active' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return {"ok": True, "active": row is not None,
                "result": dict(row) if row else None}
    except Exception as exc:
        return _err("active_build", exc)
