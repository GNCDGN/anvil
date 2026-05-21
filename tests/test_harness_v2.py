"""v2 Phase 1 Step 4 — harness_v2 tests.

Covers ingest idempotency, view shapes, JSONL round-trip, XLSX export,
cost computation, mode.txt resolution, run-dir discovery, and the
two-distinct-latency columns required by Step 3 outcome finding 6.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

from openpyxl import load_workbook

from tools import harness_v2


_FIX_ROOT = Path(__file__).resolve().parent.parent / "tools" / "fixtures" / "v2-phase-1"
# v2 Phase 2 Step 1: fixture dirs are mode-suffixed (`-mock`, `-real`),
# and the events.jsonl run_id field carries the same mode segment. The
# T1-mock + T1-real pair lives under one task_id (T1) and gives the
# composite-key regression test direct fixture coverage.
_CLEAN_DIR = _FIX_ROOT / "T1-doc-edit-mock"
_CLEAN_DIR_REAL = _FIX_ROOT / "T1-doc-edit-real"
_ESC_DIR = _FIX_ROOT / "T3-out-of-scope-real"
# v2 Phase 2 Step 2: T2-two-step-real fixture synthesises the
# "validation.fail → retry → escalate" episode shape (2 stage_b.api_end
# events with tokens, 1 validation.fail, 1 escalate). Direct fixture
# coverage for validation_failure_episodes.
_RETRY_ESC_DIR = _FIX_ROOT / "T2-two-step-real"


def _event_row(t_ms, kind, data, *, run_id, step_idx=None):
    t = datetime(2026, 5, 20, 14, 30, 0, tzinfo=timezone.utc) + timedelta(milliseconds=t_ms)
    return {
        "ts": t.isoformat(timespec="milliseconds"),
        "run_id": run_id,
        "step_idx": step_idx,
        "kind": kind,
        "data": data,
        "elapsed_ms": t_ms,
    }


class _HarnessTestBase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "test.duckdb"
        self.con = harness_v2.open_db(self.db_path)

    def tearDown(self) -> None:
        self.con.close()
        self._tmp.cleanup()


class TestIngestRoundTrip(_HarnessTestBase):

    def test_ingest_round_trip(self) -> None:
        # Create a synthetic run-dir under tmp.
        run_dir = self.tmp_path / "T9-synth"
        run_dir.mkdir()
        events = [
            _event_row(0, "run.start", {}, run_id="T9-synth"),
            _event_row(
                10, "planner.stage_a.api_end",
                {"model": "claude-opus-4-7", "input_tokens": 1000,
                 "output_tokens": 50, "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0, "duration_ms": 1500, "ok": True},
                run_id="T9-synth", step_idx=0,
            ),
            _event_row(20, "run.end", {"drops": 0}, run_id="T9-synth"),
        ]
        (run_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8",
        )
        (run_dir / "mode.txt").write_text("mock\n", encoding="utf-8")
        summary = harness_v2.ingest(self.con, run_dir)
        self.assertEqual(summary["events_ingested"], 3)
        self.assertEqual(summary["task_id"], "T9")
        self.assertEqual(summary["task_label"], "synth")
        self.assertEqual(summary["mode"], "mock")
        # Round-trip via SQL.
        rows = self.con.execute(
            "SELECT kind FROM events WHERE run_id = 'T9-synth' ORDER BY ts"
        ).fetchall()
        self.assertEqual([r[0] for r in rows],
                         ["run.start", "planner.stage_a.api_end", "run.end"])

    def test_idempotent_reingest(self) -> None:
        harness_v2.ingest(self.con, _CLEAN_DIR)
        first_count = self.con.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = 'T1-doc-edit-mock'"
        ).fetchone()[0]
        # Re-ingest the same dir.
        harness_v2.ingest(self.con, _CLEAN_DIR)
        second_count = self.con.execute(
            "SELECT COUNT(*) FROM events WHERE run_id = 'T1-doc-edit-mock'"
        ).fetchone()[0]
        self.assertEqual(first_count, second_count)


class TestOperationsView(_HarnessTestBase):

    def test_clean_fixture_operations_count_and_columns(self) -> None:
        harness_v2.ingest(self.con, _CLEAN_DIR)
        rows = harness_v2.query_operations(self.con, run_id="T1-doc-edit-mock")
        # 7 cost-bearing operations for a clean single-step run:
        # planner.stage_a.api_end, planner.stage_b.api_end,
        # planner.validation.pass, coder.subprocess.end,
        # coder.scope_verify, smoke.end, git.commit.end
        self.assertEqual(len(rows), 7)
        # Column count matches the declared OPERATIONS_COLUMNS tuple.
        self.assertEqual(len(rows[0]), len(harness_v2._OPERATIONS_COLUMNS))

    def test_escalation_fixture_surfaces_escalation_reason(self) -> None:
        harness_v2.ingest(self.con, _ESC_DIR)
        rows = self.con.execute(
            "SELECT operation_kind, escalation_reason, escalation_user_latency_ms "
            "FROM operations WHERE run_id = 'T3-out-of-scope-real' "
            "  AND operation_kind = 'escalation.resolved'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "escalation.resolved")
        self.assertEqual(rows[0][2], 5000)
        # The escalation.resolved itself doesn't carry a reason (the reason
        # lives on escalation.raised). The operations view doesn't join
        # raised→resolved, so this column is None for the resolved row.
        # That's a documented limitation; recorded as a Step 5 carry.

    def test_cost_computation_correctness(self) -> None:
        harness_v2.ingest(self.con, _CLEAN_DIR)
        # Stage A: input=10500, output=115, cache=0/0
        #   cost = (10500*15 + 115*75) / 1e6 = (157500 + 8625) / 1e6 = 0.166125
        rows = self.con.execute(
            "SELECT cost_usd FROM operations "
            "WHERE run_id = 'T1-doc-edit-mock' AND operation_kind = 'planner.stage_a.api_end'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][0], 0.166125, places=4)

    def test_two_distinct_latency_columns_surface(self) -> None:
        # poll_reply_chars vs escalation_user_latency_ms — they live in
        # the operations view as two distinct columns, populated only on
        # their respective kinds. Step 3 outcome finding 6.
        harness_v2.ingest(self.con, _ESC_DIR)
        cols = harness_v2._OPERATIONS_COLUMNS
        self.assertIn("escalation_user_latency_ms", cols)
        self.assertIn("poll_reply_chars", cols)
        # The escalation row has latency_ms populated, poll_reply_chars NULL.
        # No poll.reply event in fixtures → that column is NULL for all rows.
        rows = self.con.execute(
            "SELECT escalation_user_latency_ms, poll_reply_chars "
            "FROM operations WHERE run_id = 'T3-out-of-scope-real' "
            "  AND operation_kind = 'escalation.resolved'"
        ).fetchall()
        self.assertEqual(rows[0][0], 5000)
        self.assertIsNone(rows[0][1])


class TestPerRunSummary(_HarnessTestBase):

    def test_clean_fixture_summary_fields(self) -> None:
        harness_v2.ingest(self.con, _CLEAN_DIR)
        rows = harness_v2.query_per_run_summary(self.con, run_id="T1-doc-edit-mock")
        self.assertEqual(len(rows), 1)
        # Columns: run_id, task_id, task_label, mode,
        # total_cost_usd, total_duration_s, planner_calls, coder_calls,
        # escalations, resumed, terminal_event
        row = rows[0]
        # v2 Phase 2 Step 1: run_id carries `-mock` suffix; task_label
        # stays mode-independent (`doc-edit`, not `doc-edit-mock`)
        # because derive_task() strips the mode suffix before parsing.
        self.assertEqual(row[0], "T1-doc-edit-mock")
        self.assertEqual(row[1], "T1")
        self.assertEqual(row[2], "doc-edit")
        self.assertEqual(row[3], "mock")
        # 2 planner calls (stage A + stage B), 1 coder call, 0 escalations,
        # not resumed.
        self.assertEqual(row[6], 2)  # planner_calls
        self.assertEqual(row[7], 1)  # coder_calls
        self.assertEqual(row[8], 0)  # escalations
        self.assertEqual(row[9], False)  # resumed

    def test_resumed_run_flag_true(self) -> None:
        run_dir = self.tmp_path / "T9-resumed"
        run_dir.mkdir()
        events = [
            _event_row(0,  "run.start",  {}, run_id="T9-resumed"),
            _event_row(5,  "run.resume", {"run_id": "T9-resumed", "from_step": 2}, run_id="T9-resumed"),
            _event_row(10, "run.end",    {"drops": 0}, run_id="T9-resumed"),
        ]
        (run_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8",
        )
        harness_v2.ingest(self.con, run_dir)
        rows = harness_v2.query_per_run_summary(self.con, run_id="T9-resumed")
        self.assertEqual(rows[0][9], True)


class TestPerTaskComparison(_HarnessTestBase):

    def test_comparison_pivots_mock_real(self) -> None:
        harness_v2.ingest(self.con, _CLEAN_DIR)        # T1 / mock
        harness_v2.ingest(self.con, _ESC_DIR)          # T3 / real
        rows = self.con.execute(
            "SELECT task_id, planner_calls_mock, planner_calls_real, "
            "       framework_overhead_s "
            "FROM per_task_comparison ORDER BY task_id"
        ).fetchall()
        # Two rows (T1, T3); each populates one side of the mock/real split.
        self.assertEqual(len(rows), 2)
        t1 = next(r for r in rows if r[0] == "T1")
        t3 = next(r for r in rows if r[0] == "T3")
        # T1 was mocked: planner_calls_mock populated, planner_calls_real None
        self.assertIsNotNone(t1[1])
        self.assertIsNone(t1[2])
        # T3 was real: planner_calls_real populated, planner_calls_mock None
        self.assertIsNone(t3[1])
        self.assertIsNotNone(t3[2])
        # framework_overhead_s == total_duration_mock (per brief).
        self.assertIsNotNone(t1[3])  # T1 has mock data → has overhead

    # v2 Phase 2 Step 1 ----------------------------------------------------
    # The composite (run_id, mode) idempotency key is the load-bearing
    # invariant of Step 1. The two tests below give it direct fixture
    # coverage: ingesting mock-then-real for the same task_id must
    # preserve both halves on disk, in the events table, and in the
    # derived per_task_comparison view.

    def test_mock_then_real_same_task_preserves_both_halves(self) -> None:
        """Mock ingest followed by real ingest for the SAME task_id must
        not clobber the mock rows. Under v2 Phase 1's run_id-only
        idempotency key this would have lost the mock half (V2P1-4)."""
        harness_v2.ingest(self.con, _CLEAN_DIR)         # T1 / mock
        mock_count = self.con.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE run_id = 'T1-doc-edit-mock' AND mode = 'mock'"
        ).fetchone()[0]
        self.assertGreater(mock_count, 0)

        harness_v2.ingest(self.con, _CLEAN_DIR_REAL)    # T1 / real
        # After the real ingest, the mock rows must still be there.
        mock_count_after = self.con.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE run_id = 'T1-doc-edit-mock' AND mode = 'mock'"
        ).fetchone()[0]
        real_count_after = self.con.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE run_id = 'T1-doc-edit-real' AND mode = 'real'"
        ).fetchone()[0]
        self.assertEqual(mock_count_after, mock_count,
                         "real ingest clobbered the mock half")
        self.assertGreater(real_count_after, 0,
                           "real ingest produced no rows")
        # run_metadata also holds both halves.
        rm_rows = self.con.execute(
            "SELECT run_id, mode FROM run_metadata WHERE task_id = 'T1' "
            "ORDER BY mode"
        ).fetchall()
        self.assertEqual(rm_rows,
                         [("T1-doc-edit-mock", "mock"),
                          ("T1-doc-edit-real", "real")])

    def test_per_task_comparison_non_null_both_halves(self) -> None:
        """After ingesting a mock+real pair for the same task, the T1
        per_task_comparison row has BOTH mock and real columns populated
        — the load-bearing exam-question of v2 Phase 2 Q1."""
        harness_v2.ingest(self.con, _CLEAN_DIR)         # T1 / mock
        harness_v2.ingest(self.con, _CLEAN_DIR_REAL)    # T1 / real
        row = self.con.execute(
            "SELECT task_id, planner_calls_mock, planner_calls_real, "
            "       total_duration_mock, total_duration_real, "
            "       framework_overhead_s "
            "FROM per_task_comparison WHERE task_id = 'T1'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "T1")
        self.assertIsNotNone(row[1], "planner_calls_mock NULL")
        self.assertIsNotNone(row[2], "planner_calls_real NULL")
        self.assertIsNotNone(row[3], "total_duration_mock NULL")
        self.assertIsNotNone(row[4], "total_duration_real NULL")
        self.assertIsNotNone(row[5], "framework_overhead_s NULL")


class TestValidationFailureEpisodes(_HarnessTestBase):
    """v2 Phase 2 Step 2: validation_failure_episodes view.

    The T2-two-step-real fixture encodes the production
    "validation.fail → retry → escalate" episode shape (planner.py
    `_run_stage_b_with_retry`, second-attempt empty path). The view
    must surface one episode row for the fixture's step_idx=0 with
    n_validation_fails=1 and extra_api_calls=1 (the retry's
    stage_b.api_end is "beyond the first")."""

    def test_episode_row_shape(self) -> None:
        harness_v2.ingest(self.con, _RETRY_ESC_DIR)
        rows = harness_v2.query_validation_failure_episodes(self.con)
        self.assertEqual(len(rows), 1, f"expected 1 episode, got {rows}")
        # Columns per _VALIDATION_FAILURE_EPISODES_COLUMNS:
        #   (run_id, task_id, mode, step_idx, n_validation_fails,
        #    first_error, second_error, recovered,
        #    extra_api_calls, total_extra_cost_usd)
        r = rows[0]
        self.assertEqual(r[0], "T2-two-step-real")
        self.assertEqual(r[1], "T2")
        self.assertEqual(r[2], "real")
        self.assertEqual(r[3], 0)
        self.assertEqual(r[4], 1, "n_validation_fails")
        self.assertEqual(r[5], "missing field: scope_boundaries",
                         "first_error")
        self.assertIsNone(r[6], "second_error should be NULL")
        self.assertFalse(r[7], "recovered should be FALSE (escalated)")
        self.assertEqual(r[8], 1, "extra_api_calls")
        # total_extra_cost_usd = cost of the retry stage_b.api_end =
        # (2000 input * 15 + 20 output * 75) / 1e6 = 0.0315
        self.assertAlmostEqual(r[9], 0.0315, places=6,
                               msg="total_extra_cost_usd")

    def test_empty_db_returns_no_episodes(self) -> None:
        """No validation.fail events → empty view."""
        harness_v2.ingest(self.con, _CLEAN_DIR)  # T1-mock: no fails
        rows = harness_v2.query_validation_failure_episodes(self.con)
        self.assertEqual(rows, [])


class TestModeResolution(_HarnessTestBase):

    def test_mode_present(self) -> None:
        run_dir = self.tmp_path / "T9-mode"
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text(
            json.dumps(_event_row(0, "run.start", {}, run_id="T9-mode")) + "\n",
            encoding="utf-8",
        )
        (run_dir / "mode.txt").write_text("real\n", encoding="utf-8")
        s = harness_v2.ingest(self.con, run_dir)
        self.assertEqual(s["mode"], "real")

    def test_mode_absent_defaults_unknown(self) -> None:
        run_dir = self.tmp_path / "T9-no-mode"
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text(
            json.dumps(_event_row(0, "run.start", {}, run_id="T9-no-mode")) + "\n",
            encoding="utf-8",
        )
        s = harness_v2.ingest(self.con, run_dir)
        self.assertEqual(s["mode"], "unknown")


class TestIngestAllDiscovery(_HarnessTestBase):

    def test_ingest_all_finds_every_run_dir(self) -> None:
        # Lay out three run-dirs under tmp/state/runs/.
        runs_root = self.tmp_path / "state" / "runs"
        runs_root.mkdir(parents=True)
        for name in ("Tx-one", "Ty-two", "Tz-three"):
            d = runs_root / name
            d.mkdir()
            (d / "events.jsonl").write_text(
                json.dumps(_event_row(0, "run.start", {}, run_id=name)) + "\n",
                encoding="utf-8",
            )
        # Decoy: a dir under runs/ that lacks events.jsonl must NOT be ingested.
        (runs_root / "decoy").mkdir()
        summaries = harness_v2.ingest_all(self.con, self.tmp_path / "state")
        ingested_ids = {s["run_id"] for s in summaries}
        self.assertEqual(ingested_ids, {"Tx-one", "Ty-two", "Tz-three"})


class TestXLSXExport(_HarnessTestBase):

    def test_xlsx_has_four_sheets_with_bold_headers(self) -> None:
        # v2 Phase 2 Step 2: export grew a fourth sheet
        # (validation_episodes) sourced from the new view.
        harness_v2.ingest(self.con, _CLEAN_DIR)
        harness_v2.ingest(self.con, _ESC_DIR)
        harness_v2.ingest(self.con, _RETRY_ESC_DIR)
        out = self.tmp_path / "out.xlsx"
        harness_v2.export_xlsx(self.con, out)
        self.assertTrue(out.is_file())
        wb = load_workbook(out)
        self.assertEqual(
            set(wb.sheetnames),
            {
                "operations",
                "per_run_summary",
                "per_task_comparison",
                "validation_episodes",
            },
        )
        # Header row 1 bold on every sheet, frozen panes at A2.
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            self.assertTrue(ws.cell(row=1, column=1).font.bold,
                            f"{sheet_name} header not bold")
            self.assertEqual(ws.freeze_panes, "A2",
                             f"{sheet_name} not frozen at A2")

    def test_xlsx_validation_episodes_sheet_header_order(self) -> None:
        """The validation_episodes sheet's header row must match
        `_VALIDATION_FAILURE_EPISODES_COLUMNS` 1:1 (column order is the
        contract the v2 Phase 2 exam reads against)."""
        harness_v2.ingest(self.con, _RETRY_ESC_DIR)
        out = self.tmp_path / "out.xlsx"
        harness_v2.export_xlsx(self.con, out)
        wb = load_workbook(out)
        ws = wb["validation_episodes"]
        actual_headers = tuple(
            ws.cell(row=1, column=ix).value
            for ix in range(1, len(harness_v2._VALIDATION_FAILURE_EPISODES_COLUMNS) + 1)
        )
        self.assertEqual(
            actual_headers,
            harness_v2._VALIDATION_FAILURE_EPISODES_COLUMNS,
        )
        # The single data row in the T2-fixture-only export carries the
        # expected episode shape.
        self.assertEqual(ws.cell(row=2, column=1).value, "T2-two-step-real")
        self.assertEqual(ws.cell(row=2, column=5).value, 1)  # n_validation_fails
        self.assertEqual(ws.cell(row=2, column=9).value, 1)  # extra_api_calls


class TestSelfCheck(unittest.TestCase):
    """The bundled self-check should pass without raising."""

    def test_self_check_passes(self) -> None:
        # Redirect ANVIL_ROOT so the production calibration.duckdb is
        # untouched by the test.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"ANVIL_ROOT": tmp}):
                rc = harness_v2.self_check()
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# v2 Phase 3 Step 1 — spend_ledger (Candidate A) + cumulative_spend_by_task
# ---------------------------------------------------------------------------

class TestSpendLedger(_HarnessTestBase):
    """Candidate A: every ingest appends a ledger row; the prior
    non-superseded row for the (run_id, mode) is flipped to
    superseded=TRUE. History accumulates; the overwrite on
    events/run_metadata is unchanged."""

    def test_spend_ledger_initial_ingest_appends_one_row(self) -> None:
        harness_v2.ingest(self.con, _CLEAN_DIR)
        rows = self.con.execute(
            "SELECT run_id, mode, total_cost_usd, superseded FROM spend_ledger"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "T1-doc-edit-mock")
        self.assertEqual(rows[0][1], "mock")
        self.assertGreater(rows[0][2], 0.0)  # non-zero cost snapshot
        self.assertFalse(rows[0][3])          # current (not superseded)

    def test_spend_ledger_second_ingest_supersedes_first(self) -> None:
        harness_v2.ingest(self.con, _CLEAN_DIR)
        harness_v2.ingest(self.con, _CLEAN_DIR)
        rows = self.con.execute(
            "SELECT ledger_id, total_cost_usd, superseded, ingest_ts "
            "FROM spend_ledger WHERE run_id='T1-doc-edit-mock' AND mode='mock' "
            "ORDER BY ledger_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        # Lower ledger_id (the original) is superseded; higher is current.
        self.assertTrue(rows[0][2])
        self.assertFalse(rows[1][2])
        # ingest_ts is non-decreasing (≤ tolerates same-microsecond ties;
        # the superseded flag, not the ts, is the authoritative order).
        self.assertLessEqual(rows[0][3], rows[1][3])
        # cumulative_spend_by_task: n_ingests=2, cumulative=sum, latest=current.
        cum = self.con.execute(
            "SELECT n_ingests, cumulative_cost_usd, latest_cost_usd "
            "FROM cumulative_spend_by_task WHERE run_id='T1-doc-edit-mock'"
        ).fetchone()
        self.assertEqual(cum[0], 2)
        self.assertAlmostEqual(cum[1], rows[0][1] + rows[1][1], places=9)
        self.assertAlmostEqual(cum[2], rows[1][1], places=9)

    def test_spend_ledger_third_ingest_history_accumulates(self) -> None:
        for _ in range(3):
            harness_v2.ingest(self.con, _CLEAN_DIR)
        flags = [
            r[0] for r in self.con.execute(
                "SELECT superseded FROM spend_ledger "
                "WHERE run_id='T1-doc-edit-mock' ORDER BY ledger_id"
            ).fetchall()
        ]
        self.assertEqual(len(flags), 3)            # not capped at two
        self.assertEqual(flags.count(True), 2)     # two superseded
        self.assertEqual(flags.count(False), 1)    # one current
        self.assertFalse(flags[-1])                # current is the latest
        n = self.con.execute(
            "SELECT n_ingests FROM cumulative_spend_by_task "
            "WHERE run_id='T1-doc-edit-mock'"
        ).fetchone()[0]
        self.assertEqual(n, 3)

    def test_spend_ledger_transaction_atomicity(self) -> None:
        # First ingest commits one current row.
        harness_v2.ingest(self.con, _CLEAN_DIR)
        before = self.con.execute(
            "SELECT ledger_id, superseded FROM spend_ledger ORDER BY ledger_id"
        ).fetchall()
        self.assertEqual(len(before), 1)
        self.assertFalse(before[0][1])

        # Second ingest: inject a failure on the ledger INSERT (after the
        # superseded UPDATE). The whole transaction must roll back — the
        # UPDATE that flipped superseded=TRUE must not persist, and no row
        # is appended. Atomicity proof: the ledger is byte-identical to
        # `before`, never left with two non-superseded rows.
        #
        # DuckDBPyConnection.execute is a read-only C attribute, so it
        # can't be mock.patch'd. A thin proxy that delegates to the real
        # connection (same underlying transaction) but raises on the
        # ledger INSERT does the injection.
        class _FailOnLedgerInsert:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **kw):
                if sql.strip().startswith("INSERT INTO spend_ledger"):
                    raise RuntimeError("injected failure mid-ledger-write")
                return self._real.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with self.assertRaises(RuntimeError):
            harness_v2.ingest(_FailOnLedgerInsert(self.con), _CLEAN_DIR)

        after = self.con.execute(
            "SELECT ledger_id, superseded FROM spend_ledger ORDER BY ledger_id"
        ).fetchall()
        self.assertEqual(after, before)  # rollback restored exact prior state

    def test_cumulative_spend_view_reconciliation(self) -> None:
        # Two ingests of T1-real (a re-run) + one of T2-real.
        harness_v2.ingest(self.con, _CLEAN_DIR_REAL)
        harness_v2.ingest(self.con, _CLEAN_DIR_REAL)
        harness_v2.ingest(self.con, _RETRY_ESC_DIR)
        # actual_real_spend = SUM over all real ledger rows (3 ingests).
        actual = self.con.execute(
            "SELECT SUM(total_cost_usd) FROM spend_ledger WHERE mode='real'"
        ).fetchone()[0]
        all_rows = self.con.execute(
            "SELECT total_cost_usd FROM spend_ledger WHERE mode='real'"
        ).fetchall()
        self.assertEqual(len(all_rows), 3)
        self.assertAlmostEqual(actual, sum(r[0] for r in all_rows), places=9)
        self.assertGreater(actual, 0.0)
        # recorded_latest = SUM per_run_summary real (latest only, 2 tasks).
        recorded = self.con.execute(
            "SELECT SUM(total_cost_usd) FROM per_run_summary WHERE mode='real'"
        ).fetchone()[0]
        # actual - recorded == the one superseded T1-real ingest.
        superseded_sum = self.con.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0.0) FROM spend_ledger "
            "WHERE mode='real' AND superseded = TRUE"
        ).fetchone()[0]
        self.assertAlmostEqual(actual - recorded, superseded_sum, places=9)


class TestHarnessCLI(_HarnessTestBase):
    """v2 Phase 3 Step 1 CLI surface: --db-path threading + the new
    cumulative-spend subcommand. All invocations pass --db-path to a tmp
    DB so the live v2-phase-2 default is never touched."""

    def test_harness_cli_db_path_flag(self) -> None:
        from contextlib import redirect_stdout
        import io
        # Regression: the bare-CLI default no longer points at the
        # pre-`mode` v1 DB (the binder-error root cause).
        self.assertIn("v2-phase-2", str(harness_v2.db_path()))
        self.assertNotIn("v2-phase-1", str(harness_v2.db_path()))
        # --db-path threads end-to-end into ingest + query.
        db = self.tmp_path / "cli.duckdb"
        with redirect_stdout(io.StringIO()):
            rc = harness_v2.main(["--db-path", str(db), "ingest", str(_CLEAN_DIR)])
        self.assertEqual(rc, 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = harness_v2.main(
                ["--db-path", str(db), "per-run-summary", "T1-doc-edit-mock"]
            )
        self.assertEqual(rc, 0)
        self.assertIn("T1-doc-edit-mock", buf.getvalue())

    def test_harness_cli_cumulative_spend_subcommand(self) -> None:
        from contextlib import redirect_stdout
        import io
        db = self.tmp_path / "cli.duckdb"
        con = harness_v2.open_db(db)
        harness_v2.ingest(con, _CLEAN_DIR)
        harness_v2.ingest(con, _CLEAN_DIR)  # re-run → n_ingests=2
        con.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = harness_v2.main(["--db-path", str(db), "cumulative-spend"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        for col in harness_v2._CUMULATIVE_SPEND_COLUMNS:
            self.assertIn(col, out)
        self.assertIn("T1-doc-edit-mock", out)
        # --mode filter: a real filter excludes the mock-only fixture row.
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = harness_v2.main(
                ["--db-path", str(db), "cumulative-spend", "--mode", "real"]
            )
        self.assertEqual(rc2, 0)
        self.assertNotIn("T1-doc-edit-mock", buf2.getvalue())


class TestV1Quarantine(unittest.TestCase):
    """v2 Phase 3 Step 1: the v1 DB is renamed to calibration-archived.duckdb
    so the (re-pointed) default CLI can't reach it. `state/` is gitignored,
    so a fresh checkout / CI carries neither file — skip there."""

    def test_v1_db_quarantine(self) -> None:
        anvil_root = Path(harness_v2.__file__).resolve().parent.parent
        original = anvil_root / "state" / "v2-phase-1" / "calibration.duckdb"
        archived = anvil_root / "state" / "v2-phase-1" / "calibration-archived.duckdb"
        if not original.exists() and not archived.exists():
            self.skipTest(
                "v1 DB absent in this environment (state/ is gitignored); "
                "quarantine verified manually at deployment"
            )
        self.assertFalse(
            original.exists(),
            "v1 calibration.duckdb still at original path — quarantine incomplete",
        )
        self.assertTrue(archived.exists(), "archived v1 DB missing")


if __name__ == "__main__":
    unittest.main()
