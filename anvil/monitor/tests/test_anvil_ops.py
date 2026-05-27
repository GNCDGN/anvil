"""Mock-first unit tests for the ANVIL operations ledger (v5 Phase 1a).

Run locally against a temp SQLite file — no VPS, no real DB path. Grades the
three-table schema, the transactional-commit-last writes, the never-raises
ladder, and that the schema leaves room for 1b/1c without a migration.
"""
import os
import sqlite3
import tempfile
import unittest

from anvil.monitor import anvil_ops


class _TmpDB(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)  # start with no file; init_db creates it
        self.assertTrue(anvil_ops.init_db(self.db)["ok"])

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)


class TestInit(_TmpDB):
    def test_init_creates_three_tables(self):
        res = anvil_ops.tables(self.db)
        self.assertTrue(res["ok"])
        self.assertEqual(
            set(res["result"]),
            {"scheduled_tasks", "trigger_log", "running_builds"},
        )

    def test_init_idempotent(self):
        # second init does not error or drop data
        self.assertTrue(anvil_ops.add_scheduled_task(
            self.db, "t1", "0 22 * * 0", "active/weekly.md")["ok"])
        self.assertTrue(anvil_ops.init_db(self.db)["ok"])
        self.assertEqual(len(anvil_ops.list_scheduled_tasks(self.db)["result"]), 1)


class TestScheduledTasks(_TmpDB):
    def test_add_and_list_roundtrip(self):
        r = anvil_ops.add_scheduled_task(
            self.db, "weekly", "0 22 * * 0", "active/weekly.md", confirm_mode="auto")
        self.assertTrue(r["ok"])
        rows = anvil_ops.list_scheduled_tasks(self.db)["result"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "weekly")
        self.assertEqual(rows[0]["confirm_mode"], "auto")
        self.assertEqual(rows[0]["status"], "active")
        self.assertIsNone(rows[0]["last_fired"])

    def test_confirm_mode_defaults_explicit(self):
        anvil_ops.add_scheduled_task(self.db, "t", "@daily", "b.md")
        self.assertEqual(
            anvil_ops.list_scheduled_tasks(self.db)["result"][0]["confirm_mode"],
            "explicit",
        )

    def test_status_filter(self):
        anvil_ops.add_scheduled_task(self.db, "a", "@daily", "a.md", status="active")
        anvil_ops.add_scheduled_task(self.db, "p", "@daily", "p.md", status="paused")
        self.assertEqual(len(anvil_ops.list_scheduled_tasks(self.db, "active")["result"]), 1)
        self.assertEqual(len(anvil_ops.list_scheduled_tasks(self.db, None)["result"]), 2)

    def test_mark_fired_preserved_on_reupsert(self):
        anvil_ops.add_scheduled_task(self.db, "t", "@daily", "b.md")
        anvil_ops.mark_task_fired(self.db, "t", "2026-06-09T22:00:00Z")
        # re-upsert (e.g. config reload) must not wipe last_fired
        anvil_ops.add_scheduled_task(self.db, "t", "@daily", "b.md", confirm_mode="auto")
        row = anvil_ops.list_scheduled_tasks(self.db)["result"][0]
        self.assertEqual(row["last_fired"], "2026-06-09T22:00:00Z")
        self.assertEqual(row["confirm_mode"], "auto")


class TestTriggerLog(_TmpDB):
    def test_log_and_list(self):
        r = anvil_ops.log_trigger(self.db, "trg-1", "schedule", "2026-06-09T22:00:00Z")
        self.assertTrue(r["ok"])
        self.assertTrue(r["inserted"])
        rows = anvil_ops.list_triggers(self.db)["result"]
        self.assertEqual(rows[0]["source"], "schedule")

    def test_log_idempotent(self):
        # Q5: a replayed trigger (same id) after a restart does not double-insert
        anvil_ops.log_trigger(self.db, "trg-1", "sentry", "2026-06-09T22:00:00Z")
        again = anvil_ops.log_trigger(self.db, "trg-1", "sentry", "2026-06-09T22:05:00Z")
        self.assertTrue(again["ok"])
        self.assertFalse(again["inserted"])
        self.assertEqual(len(anvil_ops.list_triggers(self.db)["result"]), 1)

    def test_update_disposition(self):
        anvil_ops.log_trigger(self.db, "trg-1", "schedule", "2026-06-09T22:00:00Z")
        r = anvil_ops.update_trigger_disposition(
            self.db, "trg-1", "fired", fired_at="2026-06-09T22:00:01Z", notes="wake sent")
        self.assertEqual(r["updated"], 1)
        row = anvil_ops.list_triggers(self.db)["result"][0]
        self.assertEqual(row["disposition"], "fired")
        self.assertEqual(row["notes"], "wake sent")


class TestRunningBuildsModeGuard(_TmpDB):
    def test_no_active_build_initially(self):
        r = anvil_ops.active_build(self.db)
        self.assertTrue(r["ok"])
        self.assertFalse(r["active"])
        self.assertIsNone(r["result"])

    def test_mark_running_then_active(self):
        anvil_ops.mark_build_running(self.db, "run-1", "2026-06-09T22:00:00Z", "active/b.md")
        r = anvil_ops.active_build(self.db)
        self.assertTrue(r["active"])
        self.assertEqual(r["result"]["run_id"], "run-1")

    def test_clear_then_inactive(self):
        anvil_ops.mark_build_running(self.db, "run-1", "2026-06-09T22:00:00Z")
        anvil_ops.clear_running_build(self.db, "run-1", "2026-06-09T22:10:00Z")
        self.assertFalse(anvil_ops.active_build(self.db)["active"])


class TestNeverRaises(unittest.TestCase):
    def test_init_on_unwritable_path_returns_structured_error(self):
        # a path under a nonexistent directory cannot be opened → structured error
        bad = "/nonexistent-dir-xyz/sub/anvil-ops.db"
        r = anvil_ops.init_db(bad)
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_read_on_corrupt_db_returns_structured_error(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.write(fd, b"this is not a sqlite database")
        os.close(fd)
        try:
            r = anvil_ops.list_scheduled_tasks(path)
            self.assertFalse(r["ok"])
            self.assertIn("error", r)
        finally:
            os.unlink(path)

    def test_all_public_helpers_return_dicts_with_ok(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            anvil_ops.init_db(path)
            for res in (
                anvil_ops.tables(path),
                anvil_ops.add_scheduled_task(path, "t", "@daily", "b.md"),
                anvil_ops.list_scheduled_tasks(path),
                anvil_ops.mark_task_fired(path, "t", "x"),
                anvil_ops.log_trigger(path, "g", "schedule", "x"),
                anvil_ops.update_trigger_disposition(path, "g", "fired"),
                anvil_ops.list_triggers(path),
                anvil_ops.mark_build_running(path, "r", "x"),
                anvil_ops.active_build(path),
                anvil_ops.clear_running_build(path, "r", "y"),
            ):
                self.assertIsInstance(res, dict)
                self.assertIn("ok", res)
                self.assertTrue(res["ok"])
        finally:
            if os.path.exists(path):
                os.unlink(path)


class TestSchemaForwardRoom(_TmpDB):
    def test_columns_present_for_1b_1c(self):
        # the v1 schema is forward-complete: 1b writes scheduled_tasks +
        # trigger_log, 1c writes running_builds — all columns exist now, so no
        # ALTER/migration in a later sub-build (the Q-A3 disposition).
        with sqlite3.connect(self.db) as conn:
            cols = {
                t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
                for t in ("scheduled_tasks", "trigger_log", "running_builds")
            }
        self.assertLessEqual(
            {"task_id", "schedule_expr", "brief_path", "confirm_mode", "last_fired", "status"},
            cols["scheduled_tasks"])
        self.assertLessEqual(
            {"trigger_id", "source", "received_at", "disposition", "fired_at", "notes"},
            cols["trigger_log"])
        self.assertLessEqual(
            {"run_id", "started_at", "brief_path", "status", "completed_at", "notes"},
            cols["running_builds"])


if __name__ == "__main__":
    unittest.main()
