"""v2 Phase 1 Step 4 — harness_v2: events.jsonl → DuckDB → views/XLSX.

Consumer of the event stream wired by Steps 1–3. Reads
`<ANVIL_ROOT>/state/runs/<run_id>/events.jsonl`, ingests into the
`events` table at `<ANVIL_ROOT>/state/v2-phase-1/calibration.duckdb`,
exposes three views (operations / per_run_summary / per_task_comparison)
and an openpyxl XLSX export.

The legacy `tools/exam_harness.py` is unchanged and stays around for
backward-compat parsers of the `[planner]` log line. harness_v2.py
reads JSONL only — no log-line parsing.

CLI surface:
    python tools/harness_v2.py ingest <run-dir>
    python tools/harness_v2.py ingest-all [--state-root <path>]
    python tools/harness_v2.py operations [--run-id <id>]
    python tools/harness_v2.py per-run-summary [--run-id <id>]
    python tools/harness_v2.py per-task-comparison
    python tools/harness_v2.py export-xlsx <out.xlsx>
    python tools/harness_v2.py --self-check

The `_real_write = Path.write_text` capture at module top mirrors
`anvil/events.py` and `anvil/vault_ops.py`. Tests patch via this seam
when they want to control filesystem behaviour deterministically.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb
from openpyxl import Workbook
from openpyxl.styles import Font

# Module-scope capture — tests patch via this seam for failure injection.
_real_write = Path.write_text

# ---------------------------------------------------------------------------
# Cost rates — Opus 4.7, as of 2026-05. Update if pricing changes.
# Mirrors tools/exam_harness.py rates so the two harnesses agree.
# ---------------------------------------------------------------------------

RATES_USD_PER_M = {
    "input": 15.0,
    "output": 75.0,
    "cache_creation": 18.75,
    "cache_read": 1.50,
}

# ---------------------------------------------------------------------------
# FIELD_MAP — operations-view projection generator
#
# Each entry: column_name -> (kinds_pattern, json_path, sql_cast).
# kinds_pattern is a `|`-separated list of event kinds the field applies to.
# json_path is a `$.field` JSON pointer. sql_cast is the DuckDB type the
# json_extract value is cast to. Some columns are derived (cost_usd,
# cache_hit_rate, ts_start, prompt_chars) and live outside this map.
# ---------------------------------------------------------------------------

FIELD_MAP: dict[str, tuple[str, str, str]] = {
    # Token usage (only on api_end events)
    "input_tokens":         ("planner.stage_a.api_end|planner.stage_b.api_end|planner.stage_c.api_end",
                             "$.input_tokens", "BIGINT"),
    "output_tokens":        ("planner.stage_a.api_end|planner.stage_b.api_end|planner.stage_c.api_end",
                             "$.output_tokens", "BIGINT"),
    "cache_creation_tokens": ("planner.stage_a.api_end|planner.stage_b.api_end|planner.stage_c.api_end",
                              "$.cache_creation_input_tokens", "BIGINT"),
    "cache_read_tokens":    ("planner.stage_a.api_end|planner.stage_b.api_end|planner.stage_c.api_end",
                             "$.cache_read_input_tokens", "BIGINT"),
    # Operation outcome
    "model":                ("*.api_end", "$.model", "VARCHAR"),
    "duration_ms":          ("*", "$.duration_ms", "BIGINT"),
    "response_chars":       ("coder.subprocess.end", "$.stdout_chars", "BIGINT"),
    "files_loaded":         ("planner.stage_b.files_loaded", "$.files_loaded_count", "BIGINT"),
    "files_touched_count":  ("coder.scope_verify", "$.files_touched_count", "BIGINT"),
    "exit_code":            ("coder.subprocess.end", "$.exit_code", "BIGINT"),
    "ok":                   ("*.end|*.reply|*.resolved", "$.ok", "BOOLEAN"),
    "escalation_reason":    ("escalation.raised|escalation.resolved|planner.escalate|coder.preflight.escalate",
                             "$.reason", "VARCHAR"),
    "escalation_user_latency_ms": ("escalation.resolved", "$.latency_ms_user", "BIGINT"),
    "retry_attempt":        ("planner.stage_b.*", "$.retry_attempt", "BIGINT"),
    "out_of_scope_count":   ("coder.scope_verify", "$.out_of_scope_count", "BIGINT"),
}

# Kinds that surface as one row each in the operations view. Each is the
# "terminal" event for an operation. .start events become joined columns
# (ts_start, prompt_chars) via correlated subquery.
OPERATION_KINDS: tuple[str, ...] = (
    "planner.stage_a.api_end",
    "planner.stage_b.api_end",
    "planner.stage_c.api_end",
    "planner.validation.pass",
    "planner.validation.fail",
    "planner.retry.end",
    "planner.escalate",
    "coder.subprocess.end",
    "coder.preflight.escalate",
    "coder.scope_verify",
    "smoke.end",
    "git.commit.end",
    "git.push.end",
    "ssh.stage.end",
    "telegram.send.end",
    "telegram.poll.reply",
    "escalation.resolved",
)


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------

def _anvil_root() -> Path:
    """Resolve ANVIL_ROOT from env, defaulting to the repo root (parent
    of the `tools/` dir)."""
    default = Path(__file__).resolve().parent.parent
    return Path(os.environ.get("ANVIL_ROOT", str(default))).expanduser()


def db_path() -> Path:
    """Default DuckDB path under `<ANVIL_ROOT>/state/v2-phase-1/`."""
    return _anvil_root() / "state" / "v2-phase-1" / "calibration.duckdb"


def open_db(path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open (or create) the DuckDB file at `path` (or `db_path()`).

    On first open, creates the schema (events table, run_metadata table,
    three views). Subsequent opens are no-ops at the schema level —
    CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE VIEW are both
    idempotent.
    """
    p = path if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(p))
    _ensure_schema(con)
    return con


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    # v2 Phase 2 Step 1: `mode` is a first-class column on events (sourced
    # from `<run_dir>/mode.txt` at ingest time, default `'unknown'`). The
    # delete-then-insert idempotency key is the composite (run_id, mode);
    # a mock-then-real ingest of the same task no longer clobbers the
    # mock half. run_metadata's PK is the same composite.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            ts          TIMESTAMP,
            run_id      VARCHAR,
            mode        VARCHAR NOT NULL DEFAULT 'unknown',
            step_idx    INTEGER,
            kind        VARCHAR,
            data        JSON,
            elapsed_ms  BIGINT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS run_metadata (
            run_id        VARCHAR,
            mode          VARCHAR NOT NULL DEFAULT 'unknown',
            task_id       VARCHAR,
            task_label    VARCHAR,
            ingested_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, mode)
        )
        """
    )
    con.execute(_OPERATIONS_VIEW_SQL)
    con.execute(_PER_RUN_SUMMARY_VIEW_SQL)
    con.execute(_PER_TASK_COMPARISON_VIEW_SQL)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

# operations view: one row per cost-bearing operation across all runs.
# JSON extraction is verbose but explicit — each column documents its
# source kind set + json path. The .start lookup for ts_start /
# prompt_chars is a correlated subquery; works at fixture scale and at
# the calibration sweep's ~hundreds-of-events scale.
_OPERATIONS_VIEW_SQL = """
CREATE OR REPLACE VIEW operations AS
SELECT
    e.run_id,
    rm.task_id,
    rm.task_label,
    rm.mode,
    e.step_idx,
    e.kind AS operation_kind,
    (
        SELECT s.ts FROM events s
        WHERE s.run_id = e.run_id
          AND COALESCE(s.step_idx, -1) = COALESCE(e.step_idx, -1)
          AND s.kind = CASE
            WHEN e.kind LIKE '%.api_end'           THEN REPLACE(e.kind, '.api_end', '.api_start')
            WHEN e.kind = 'coder.subprocess.end'    THEN 'coder.subprocess.start'
            WHEN e.kind = 'smoke.end'               THEN 'smoke.start'
            WHEN e.kind = 'git.commit.end'          THEN 'git.commit.start'
            WHEN e.kind = 'git.push.end'            THEN 'git.push.start'
            WHEN e.kind = 'ssh.stage.end'           THEN 'ssh.stage.start'
            WHEN e.kind = 'telegram.send.end'       THEN 'telegram.send.start'
            WHEN e.kind = 'telegram.poll.reply'     THEN 'telegram.poll.start'
            WHEN e.kind = 'escalation.resolved'     THEN 'escalation.raised'
            ELSE NULL
          END
        ORDER BY s.ts DESC LIMIT 1
    ) AS ts_start,
    e.ts AS ts_end,
    CAST(json_extract(e.data, '$.duration_ms') AS BIGINT) AS duration_ms,
    json_extract_string(e.data, '$.model') AS model,
    CAST(json_extract(e.data, '$.input_tokens') AS BIGINT) AS input_tokens,
    CAST(json_extract(e.data, '$.output_tokens') AS BIGINT) AS output_tokens,
    CAST(json_extract(e.data, '$.cache_creation_input_tokens') AS BIGINT) AS cache_creation_tokens,
    CAST(json_extract(e.data, '$.cache_read_input_tokens') AS BIGINT) AS cache_read_tokens,
    CASE
        WHEN CAST(json_extract(e.data, '$.input_tokens') AS BIGINT) IS NULL THEN NULL
        WHEN (
            COALESCE(CAST(json_extract(e.data, '$.cache_read_input_tokens') AS BIGINT), 0) +
            COALESCE(CAST(json_extract(e.data, '$.cache_creation_input_tokens') AS BIGINT), 0) +
            CAST(json_extract(e.data, '$.input_tokens') AS BIGINT)
        ) = 0 THEN NULL
        ELSE
            COALESCE(CAST(json_extract(e.data, '$.cache_read_input_tokens') AS BIGINT), 0) * 1.0 /
            (
                COALESCE(CAST(json_extract(e.data, '$.cache_read_input_tokens') AS BIGINT), 0) +
                COALESCE(CAST(json_extract(e.data, '$.cache_creation_input_tokens') AS BIGINT), 0) +
                CAST(json_extract(e.data, '$.input_tokens') AS BIGINT)
            )
    END AS cache_hit_rate,
    CASE
        WHEN CAST(json_extract(e.data, '$.input_tokens') AS BIGINT) IS NULL THEN 0.0
        ELSE (
            COALESCE(CAST(json_extract(e.data, '$.input_tokens') AS BIGINT), 0) * 15.0 +
            COALESCE(CAST(json_extract(e.data, '$.output_tokens') AS BIGINT), 0) * 75.0 +
            COALESCE(CAST(json_extract(e.data, '$.cache_creation_input_tokens') AS BIGINT), 0) * 18.75 +
            COALESCE(CAST(json_extract(e.data, '$.cache_read_input_tokens') AS BIGINT), 0) * 1.50
        ) / 1000000.0
    END AS cost_usd,
    (
        SELECT CAST(json_extract(s.data, '$.prompt_chars') AS BIGINT) FROM events s
        WHERE s.run_id = e.run_id
          AND COALESCE(s.step_idx, -1) = COALESCE(e.step_idx, -1)
          AND s.kind = CASE
            WHEN e.kind LIKE '%.api_end'           THEN REPLACE(e.kind, '.api_end', '.api_start')
            WHEN e.kind = 'coder.subprocess.end'    THEN 'coder.subprocess.start'
            ELSE NULL
          END
        ORDER BY s.ts DESC LIMIT 1
    ) AS prompt_chars,
    CAST(json_extract(e.data, '$.stdout_chars') AS BIGINT) AS response_chars,
    CAST(json_extract(e.data, '$.files_loaded_count') AS BIGINT) AS files_loaded,
    CAST(json_extract(e.data, '$.files_touched_count') AS BIGINT) AS files_touched_count,
    CAST(json_extract(e.data, '$.exit_code') AS BIGINT) AS exit_code,
    CAST(json_extract(e.data, '$.ok') AS BOOLEAN) AS ok,
    CASE
        WHEN e.kind = 'planner.validation.pass' THEN 'pass'
        WHEN e.kind = 'planner.validation.fail' THEN 'fail'
        ELSE NULL
    END AS validation_result,
    json_extract_string(e.data, '$.reason') AS escalation_reason,
    CAST(json_extract(e.data, '$.latency_ms_user') AS BIGINT) AS escalation_user_latency_ms,
    CAST(json_extract(e.data, '$.retry_attempt') AS BIGINT) AS retry_attempt,
    CAST(json_extract(e.data, '$.out_of_scope_count') AS BIGINT) AS out_of_scope_count,
    CAST(json_extract(e.data, '$.reply_text_chars') AS BIGINT) AS poll_reply_chars
FROM events e
-- v2 Phase 2 Step 1: JOIN on the composite (run_id, mode) to match
-- run_metadata's composite PK. With the same run_id potentially
-- present under both 'mock' and 'real', a run_id-only JOIN would
-- duplicate every event row.
LEFT JOIN run_metadata rm USING (run_id, mode)
WHERE e.kind IN (
    'planner.stage_a.api_end',
    'planner.stage_b.api_end',
    'planner.stage_c.api_end',
    'planner.validation.pass',
    'planner.validation.fail',
    'planner.retry.end',
    'planner.escalate',
    'coder.subprocess.end',
    'coder.preflight.escalate',
    'coder.scope_verify',
    'smoke.end',
    'git.commit.end',
    'git.push.end',
    'ssh.stage.end',
    'telegram.send.end',
    'telegram.poll.reply',
    'escalation.resolved'
)
"""

# Column order in operations (mirrored in XLSX export):
_OPERATIONS_COLUMNS: tuple[str, ...] = (
    "run_id", "task_id", "task_label", "mode", "step_idx", "operation_kind",
    "ts_start", "ts_end", "duration_ms", "model",
    "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_hit_rate", "cost_usd",
    "prompt_chars", "response_chars",
    "files_loaded", "files_touched_count",
    "exit_code", "ok", "validation_result",
    "escalation_reason", "escalation_user_latency_ms",
    "retry_attempt", "out_of_scope_count", "poll_reply_chars",
)


_PER_RUN_SUMMARY_VIEW_SQL = """
CREATE OR REPLACE VIEW per_run_summary AS
SELECT
    e.run_id,
    rm.task_id,
    rm.task_label,
    rm.mode,
    (SELECT COALESCE(SUM(cost_usd), 0.0) FROM operations o WHERE o.run_id = e.run_id) AS total_cost_usd,
    EXTRACT(EPOCH FROM (MAX(e.ts) - MIN(e.ts))) AS total_duration_s,
    COUNT(*) FILTER (WHERE e.kind LIKE 'planner.stage_%' AND e.kind LIKE '%.api_end') AS planner_calls,
    COUNT(*) FILTER (WHERE e.kind = 'coder.subprocess.end') AS coder_calls,
    COUNT(*) FILTER (WHERE e.kind = 'escalation.raised') AS escalations,
    BOOL_OR(e.kind = 'run.resume') AS resumed,
    (
        SELECT e2.kind FROM events e2
        WHERE e2.run_id = e.run_id ORDER BY e2.ts DESC LIMIT 1
    ) AS terminal_event
FROM events e
-- v2 Phase 2 Step 1: composite-key JOIN; see operations view comment.
LEFT JOIN run_metadata rm USING (run_id, mode)
GROUP BY e.run_id, rm.task_id, rm.task_label, rm.mode
"""

_PER_RUN_SUMMARY_COLUMNS: tuple[str, ...] = (
    "run_id", "task_id", "task_label", "mode",
    "total_cost_usd", "total_duration_s",
    "planner_calls", "coder_calls", "escalations",
    "resumed", "terminal_event",
)


# v2 Phase 2 Step 1: rewrite per_task_comparison as a FULL OUTER JOIN
# between the mock half and the real half of `per_run_summary`, keyed on
# task_id. Each half is a WHERE-mode-filtered subquery — explicit about
# which mode column each metric is sourced from. The prior CASE-pivot
# version was functionally equivalent under v2 Phase 1's run_id-unique
# constraint; under v2 Phase 2's composite key, T1 mock and T1 real are
# two rows sharing task_id, and the JOIN shape makes the cross-mode read
# unambiguous (no MAX() across the two rows).
_PER_TASK_COMPARISON_VIEW_SQL = """
CREATE OR REPLACE VIEW per_task_comparison AS
WITH
    mock_runs AS (
        SELECT task_id, planner_calls, total_duration_s, total_cost_usd
        FROM per_run_summary
        WHERE mode = 'mock' AND task_id IS NOT NULL
    ),
    real_runs AS (
        SELECT task_id, planner_calls, total_duration_s, total_cost_usd
        FROM per_run_summary
        WHERE mode = 'real' AND task_id IS NOT NULL
    )
SELECT
    COALESCE(m.task_id, r.task_id) AS task_id,
    m.planner_calls   AS planner_calls_mock,
    r.planner_calls   AS planner_calls_real,
    m.total_duration_s AS total_duration_mock,
    r.total_duration_s AS total_duration_real,
    r.total_cost_usd  AS total_cost_real,
    m.total_duration_s AS framework_overhead_s
FROM mock_runs m
FULL OUTER JOIN real_runs r USING (task_id)
"""

_PER_TASK_COMPARISON_COLUMNS: tuple[str, ...] = (
    "task_id",
    "planner_calls_mock", "planner_calls_real",
    "total_duration_mock", "total_duration_real",
    "total_cost_real",
    "framework_overhead_s",
)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

_RUN_DIR_RE = re.compile(r"^(T\d+)(?:-(.+))?$")

# v2 Phase 2 Step 1: calibration run_ids carry the mode as a trailing
# segment (`T1-doc-edit-mock`, `T1-doc-edit-real`). The task_label is
# conceptually mode-independent — mode is its own column. Strip the
# suffix before applying the regex so `task_label` stays `doc-edit`,
# `out-of-scope`, etc., not `doc-edit-mock`.
_MODE_SUFFIXES = ("-mock", "-real", "-unknown")


def derive_task(run_id: str) -> tuple[str, str]:
    """Derive (task_id, task_label) from a run-dir name.

    Calibration runs use `T<N>` or `T<N>-<label>` prefixes; v2 Phase 2
    appends a `-mock`/`-real`/`-unknown` mode segment after the label.
    Real ANVIL runs use `<YYYY-MM-DD-HHMM>-<slug>`. For non-calibration
    shapes, task_id falls back to the run_id and task_label to "".
    """
    stripped = run_id
    for suf in _MODE_SUFFIXES:
        if stripped.endswith(suf):
            stripped = stripped[: -len(suf)]
            break
    m = _RUN_DIR_RE.match(stripped)
    if m:
        return m.group(1), (m.group(2) or "")
    return run_id, ""


def _read_mode(run_dir: Path) -> str:
    """Read `<run_dir>/mode.txt` if present; default `"unknown"` otherwise."""
    p = run_dir / "mode.txt"
    if not p.is_file():
        return "unknown"
    try:
        return p.read_text(encoding="utf-8").strip() or "unknown"
    except Exception:  # noqa: BLE001 — never-raise
        return "unknown"


def _parse_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL file into a list of dicts. Silently drops malformed lines."""
    out: list[dict] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Malformed lines are dropped (consistent with the never-raise
            # contract of the events.py producer side).
            continue
    return out


def ingest(con: duckdb.DuckDBPyConnection, run_dir: Path) -> dict:
    """Ingest one run-dir's events.jsonl into the events table.

    Idempotent: deletes any prior rows for this run_id, then inserts.
    Returns a small summary dict for the caller (event count + dropped
    count + task_id / mode resolution).
    """
    run_dir = Path(run_dir).resolve()
    run_id = run_dir.name
    task_id, task_label = derive_task(run_id)
    mode = _read_mode(run_dir)

    events_path = run_dir / "events.jsonl"
    rows = _parse_jsonl(events_path)

    con.execute("BEGIN")
    try:
        # v2 Phase 2 Step 1: composite (run_id, mode) idempotency key.
        # A mock-then-real ingest of the same task no longer clobbers the
        # mock half because the modes differ. `mode` is stamped on every
        # event row at ingest time (sourced from `<run_dir>/mode.txt`).
        con.execute(
            "DELETE FROM events WHERE run_id = ? AND mode = ?",
            [run_id, mode],
        )
        con.execute(
            "DELETE FROM run_metadata WHERE run_id = ? AND mode = ?",
            [run_id, mode],
        )
        con.execute(
            "INSERT INTO run_metadata (run_id, mode, task_id, task_label) "
            "VALUES (?, ?, ?, ?)",
            [run_id, mode, task_id, task_label],
        )
        for ev in rows:
            con.execute(
                """
                INSERT INTO events (ts, run_id, mode, step_idx, kind, data, elapsed_ms)
                VALUES (CAST(? AS TIMESTAMP), ?, ?, ?, ?, CAST(? AS JSON), ?)
                """,
                [
                    ev.get("ts"),
                    ev.get("run_id") or run_id,
                    mode,
                    ev.get("step_idx"),
                    ev.get("kind"),
                    json.dumps(ev.get("data") or {}),
                    int(ev.get("elapsed_ms") or 0),
                ],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return {
        "run_id": run_id,
        "task_id": task_id,
        "task_label": task_label,
        "mode": mode,
        "events_ingested": len(rows),
    }


def ingest_all(
    con: duckdb.DuckDBPyConnection, state_root: Path | None = None,
) -> list[dict]:
    """Iterate every subdir under `<state_root>/runs/` (or `<ANVIL_ROOT>/state/runs/`)
    containing an `events.jsonl` and ingest each. Returns a list of
    per-run summary dicts in dir-name order.

    Per Step 3 outcome finding 1: discovers run-dirs by globbing rather
    than requiring an explicit list — every directory with an
    `events.jsonl` is a candidate.
    """
    root = state_root if state_root is not None else (_anvil_root() / "state")
    runs_root = root / "runs"
    if not runs_root.is_dir():
        return []
    out: list[dict] = []
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        if (run_dir / "events.jsonl").is_file():
            out.append(ingest(con, run_dir))
    return out


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def query_operations(
    con: duckdb.DuckDBPyConnection, run_id: str | None = None,
) -> list[tuple]:
    if run_id:
        return con.execute(
            f"SELECT {', '.join(_OPERATIONS_COLUMNS)} FROM operations "
            "WHERE run_id = ? ORDER BY ts_end",
            [run_id],
        ).fetchall()
    return con.execute(
        f"SELECT {', '.join(_OPERATIONS_COLUMNS)} FROM operations "
        "ORDER BY run_id, ts_end"
    ).fetchall()


def query_per_run_summary(
    con: duckdb.DuckDBPyConnection, run_id: str | None = None,
) -> list[tuple]:
    if run_id:
        return con.execute(
            f"SELECT {', '.join(_PER_RUN_SUMMARY_COLUMNS)} FROM per_run_summary "
            "WHERE run_id = ?",
            [run_id],
        ).fetchall()
    return con.execute(
        f"SELECT {', '.join(_PER_RUN_SUMMARY_COLUMNS)} FROM per_run_summary "
        "ORDER BY run_id"
    ).fetchall()


def query_per_task_comparison(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        f"SELECT {', '.join(_PER_TASK_COMPARISON_COLUMNS)} FROM per_task_comparison "
        "ORDER BY task_id"
    ).fetchall()


# ---------------------------------------------------------------------------
# XLSX export
# ---------------------------------------------------------------------------

def export_xlsx(con: duckdb.DuckDBPyConnection, out_path: Path) -> None:
    """Export three sheets (operations, per_run_summary, per_task_comparison)
    to `out_path`. Header row bold, frozen panes on row 1, no charts."""
    wb = Workbook()
    # Default sheet → operations.
    ws_ops = wb.active
    ws_ops.title = "operations"
    _write_sheet(ws_ops, _OPERATIONS_COLUMNS, query_operations(con),
                 cost_columns={"cost_usd"},
                 rate_columns={"cache_hit_rate"})

    ws_runs = wb.create_sheet("per_run_summary")
    _write_sheet(ws_runs, _PER_RUN_SUMMARY_COLUMNS, query_per_run_summary(con),
                 cost_columns={"total_cost_usd"})

    ws_tasks = wb.create_sheet("per_task_comparison")
    _write_sheet(ws_tasks, _PER_TASK_COMPARISON_COLUMNS,
                 query_per_task_comparison(con),
                 cost_columns={"total_cost_real"})

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))


def _write_sheet(
    ws,
    columns: tuple[str, ...],
    rows: list[tuple],
    *,
    cost_columns: set[str] | None = None,
    rate_columns: set[str] | None = None,
) -> None:
    cost_columns = cost_columns or set()
    rate_columns = rate_columns or set()
    bold = Font(bold=True)
    # Header row.
    for col_ix, name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=col_ix, value=name)
        cell.font = bold
    # Data rows.
    for row_ix, values in enumerate(rows, start=2):
        for col_ix, (name, value) in enumerate(zip(columns, values), start=1):
            cell = ws.cell(row=row_ix, column=col_ix, value=value)
            if name in cost_columns:
                cell.number_format = "$0.00"
            elif name in rate_columns:
                cell.number_format = "0.0%"
            elif isinstance(value, int):
                cell.number_format = "0"
    ws.freeze_panes = "A2"


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

_SELF_CHECK_FIXTURES = (
    # v2 Phase 2 Step 1: fixture dir names carry the mode segment
    # (`T1-doc-edit-mock`, `T1-doc-edit-real`, `T3-out-of-scope-real`).
    # Dir name == run_id, and the events.jsonl carries that run_id
    # verbatim — the JOIN between events and run_metadata stays on
    # run_id. The T1 mock+real pair gives the self-check direct coverage
    # of the new composite-key invariant: ingesting both does not
    # clobber the mock half.
    "T1-doc-edit-mock",
    "T1-doc-edit-real",
    "T3-out-of-scope-real",
)


def self_check() -> int:
    """Run the harness end-to-end against the two bundled fixtures.

    Returns 0 on PASS, 1 on FAIL. Uses a tmp DuckDB file so the live
    `state/v2-phase-1/calibration.duckdb` is untouched.
    """
    fixture_root = Path(__file__).resolve().parent / "fixtures" / "v2-phase-1"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "self-check.duckdb"
            con = open_db(db_file)

            ingested: list[dict] = []
            for name in _SELF_CHECK_FIXTURES:
                run_dir = fixture_root / name
                if not (run_dir / "events.jsonl").is_file():
                    print(f"self-check: FAIL — missing fixture {run_dir}")
                    return 1
                ingested.append(ingest(con, run_dir))

            # Assertions.
            ops = query_operations(con)
            if len(ops) < 2:
                print(f"self-check: FAIL — operations rows too few ({len(ops)})")
                return 1
            runs = query_per_run_summary(con)
            expected_runs = len(_SELF_CHECK_FIXTURES)
            if len(runs) != expected_runs:
                print(
                    f"self-check: FAIL — per_run_summary expected "
                    f"{expected_runs}, got {len(runs)}"
                )
                return 1
            tasks = query_per_task_comparison(con)
            # v2 Phase 2 Step 1: per_task_comparison groups by task_id;
            # T1 contributes one row (mock + real on a single task_id),
            # T3 contributes one row (real-only). Two task_id rows total.
            if len(tasks) < 1:
                print("self-check: FAIL — per_task_comparison returned 0 rows")
                return 1
            # v2 Phase 2 Step 1: the T1 row must have BOTH mock and real
            # halves populated — the regression that the composite-key
            # fix is meant to prevent.
            t1_row = next(
                (r for r in tasks if r[0] == "T1"), None,
            )
            if t1_row is None:
                print("self-check: FAIL — T1 missing from per_task_comparison")
                return 1
            # Columns per _PER_TASK_COMPARISON_COLUMNS:
            #   (task_id, planner_calls_mock, planner_calls_real,
            #    total_duration_mock, total_duration_real,
            #    total_cost_real, framework_overhead_s)
            if t1_row[1] is None or t1_row[2] is None:
                print(
                    "self-check: FAIL — T1 mock+real key-shape regression: "
                    f"mock={t1_row[1]} real={t1_row[2]}"
                )
                return 1

            xlsx_path = tmp_path / "self-check.xlsx"
            export_xlsx(con, xlsx_path)
            if not xlsx_path.is_file():
                print(f"self-check: FAIL — XLSX not created at {xlsx_path}")
                return 1
            from openpyxl import load_workbook
            wb = load_workbook(xlsx_path)
            expected_sheets = {"operations", "per_run_summary", "per_task_comparison"}
            actual_sheets = set(wb.sheetnames)
            if not expected_sheets.issubset(actual_sheets):
                missing = expected_sheets - actual_sheets
                print(f"self-check: FAIL — XLSX missing sheets {missing}")
                return 1
            # Header row bold check.
            ws = wb["operations"]
            if not ws.cell(row=1, column=1).font.bold:
                print("self-check: FAIL — operations header row not bold")
                return 1

            con.close()
        print(f"self-check: PASS ({len(ops)} ops, {len(runs)} runs, {len(tasks)} tasks)")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"self-check: FAIL — {type(e).__name__}: {e}")
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_rows(columns: tuple[str, ...], rows: Iterable[tuple]) -> None:
    """Compact tab-separated stdout output. Suited for grep / paste into
    spreadsheets; not for human-pretty-printing wide tables."""
    sys.stdout.write("\t".join(columns) + "\n")
    for r in rows:
        sys.stdout.write("\t".join("" if v is None else str(v) for v in r) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness_v2",
        description="ANVIL v2 Phase 1 event-stream harness.",
    )
    parser.add_argument("--self-check", action="store_true",
                        help="Run against bundled fixtures; print PASS/FAIL.")
    sub = parser.add_subparsers(dest="command")

    p_ing = sub.add_parser("ingest", help="Ingest one run-dir.")
    p_ing.add_argument("run_dir", type=Path)

    p_ingall = sub.add_parser("ingest-all", help="Ingest every state/runs/* dir.")
    p_ingall.add_argument("--state-root", type=Path, default=None)

    p_ops = sub.add_parser("operations", help="Print operations view.")
    p_ops.add_argument("--run-id", type=str, default=None)

    p_runs = sub.add_parser("per-run-summary", help="Print per_run_summary view.")
    p_runs.add_argument("--run-id", type=str, default=None)

    sub.add_parser("per-task-comparison", help="Print per_task_comparison view.")

    p_xlsx = sub.add_parser("export-xlsx", help="Export three sheets to <out.xlsx>.")
    p_xlsx.add_argument("out_path", type=Path)

    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()

    if args.command is None:
        parser.print_help()
        return 2

    con = open_db()
    try:
        if args.command == "ingest":
            summary = ingest(con, args.run_dir)
            print(json.dumps(summary, indent=2))
            return 0
        if args.command == "ingest-all":
            summaries = ingest_all(con, args.state_root)
            print(json.dumps(summaries, indent=2))
            return 0
        if args.command == "operations":
            _print_rows(_OPERATIONS_COLUMNS, query_operations(con, args.run_id))
            return 0
        if args.command == "per-run-summary":
            _print_rows(_PER_RUN_SUMMARY_COLUMNS, query_per_run_summary(con, args.run_id))
            return 0
        if args.command == "per-task-comparison":
            _print_rows(_PER_TASK_COMPARISON_COLUMNS, query_per_task_comparison(con))
            return 0
        if args.command == "export-xlsx":
            export_xlsx(con, args.out_path)
            print(f"wrote {args.out_path}")
            return 0
    finally:
        con.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
