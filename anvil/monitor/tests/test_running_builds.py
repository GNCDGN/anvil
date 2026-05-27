"""Mock-first tests for the v5 Phase 1c mode-guard. Local temp SQLite; no VPS,
no network. Covers mode_guard_check (incl. fail-closed staleness), the Mac-
invoked CLI (mark-active/mark-complete), and the schedule.poll defer integration."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from anvil.monitor import anvil_ops, running_builds, schedule


class TestModeGuardCheck(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(self.db)
        anvil_ops.init_db(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def test_no_active_build(self):
        g = running_builds.mode_guard_check(self.db)
        self.assertTrue(g["ok"]); self.assertFalse(g["active"]); self.assertFalse(g["stale"])

    def test_fresh_active_not_stale(self):
        now = datetime(2026, 6, 9, 12, 0, 0)
        anvil_ops.mark_build_running(self.db, "r1", started_at=now.isoformat(), brief_path="b.md")
        g = running_builds.mode_guard_check(self.db, now=now + timedelta(minutes=10), staleness_hours=6)
        self.assertTrue(g["active"]); self.assertFalse(g["stale"])
        self.assertEqual(g["result"]["run_id"], "r1")

    def test_old_active_is_stale(self):
        now = datetime(2026, 6, 9, 12, 0, 0)
        anvil_ops.mark_build_running(self.db, "r1", started_at=now.isoformat())
        g = running_builds.mode_guard_check(self.db, now=now + timedelta(hours=7), staleness_hours=6)
        self.assertTrue(g["active"]); self.assertTrue(g["stale"])

    def test_unparseable_started_at_is_stale(self):
        anvil_ops.mark_build_running(self.db, "r1", started_at="not-a-date")
        g = running_builds.mode_guard_check(self.db)
        self.assertTrue(g["active"]); self.assertTrue(g["stale"])

    def test_read_error_is_fail_closed_active_stale(self):
        with mock.patch.object(running_builds.anvil_ops, "active_build",
                               return_value={"ok": False, "error": "disk gone"}):
            g = running_builds.mode_guard_check(self.db)
        self.assertFalse(g["ok"]); self.assertTrue(g["active"]); self.assertTrue(g["stale"])


class TestCLI(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(self.db)

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def test_mark_active_then_complete(self):
        rc = running_builds._cli(["mark-active", "run-1", "builds/x/brief.md"], db=self.db)
        self.assertEqual(rc, 0)
        self.assertTrue(anvil_ops.active_build(self.db)["active"])
        rc = running_builds._cli(["mark-complete", "run-1"], db=self.db)
        self.assertEqual(rc, 0)
        self.assertFalse(anvil_ops.active_build(self.db)["active"])

    def test_mark_active_initializes_db(self):
        # the CLI may run before the service first boots → init_db is idempotent
        self.assertFalse(os.path.exists(self.db))
        rc = running_builds._cli(["mark-active", "run-1"], db=self.db)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.db))

    def test_bad_args(self):
        self.assertEqual(running_builds._cli([], db=self.db), 2)
        self.assertEqual(running_builds._cli(["frobnicate"], db=self.db), 2)
        self.assertEqual(running_builds._cli(["mark-active"], db=self.db), 2)


class TestScheduleDeferIntegration(unittest.TestCase):
    """The mode-guard end-to-end through schedule.poll: active → defer (no fire,
    no last_fired advance); clears → fire; stale → escalate once."""
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(self.db)
        anvil_ops.init_db(self.db)
        anvil_ops.add_scheduled_task(self.db, "weekly", "@daily 09:00", "builds/x/brief.md")
        self.now = datetime(2026, 6, 9, 9, 30)  # past 09:00 → due
        self.fired = []

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _dispatch(self, task):
        self.fired.append(task["task_id"]); return {"ok": True}

    def test_active_build_defers_then_fires_on_clear(self):
        # build active → deferred, not fired, last_fired NOT advanced
        anvil_ops.mark_build_running(self.db, "r1", started_at=self.now.isoformat())
        res = schedule.poll(self.db, now=self.now, dispatch=self._dispatch,
                            guard=running_builds.mode_guard_check)
        self.assertEqual(res["fired"], [])
        self.assertEqual(res["deferred"], ["weekly"])
        self.assertEqual(self.fired, [])
        task = anvil_ops.list_scheduled_tasks(self.db)["result"][0]
        self.assertIsNone(task["last_fired"])  # not advanced → re-fires later
        rows = {r["trigger_id"]: r["disposition"]
                for r in anvil_ops.list_triggers(self.db)["result"]}
        self.assertIn("deferred-active-build", rows.values())

        # build completes → next poll fires
        anvil_ops.clear_running_build(self.db, "r1", completed_at=self.now.isoformat())
        res2 = schedule.poll(self.db, now=self.now + timedelta(minutes=1),
                             dispatch=self._dispatch, guard=running_builds.mode_guard_check)
        self.assertEqual(res2["fired"], ["weekly"])
        self.assertEqual(self.fired, ["weekly"])

    def test_stale_build_escalates_once(self):
        # an active row 7h old (default staleness 6h) → stale → escalate once
        old = (self.now - timedelta(hours=7)).isoformat()
        anvil_ops.mark_build_running(self.db, "r1", started_at=old)
        escalations = []
        schedule.poll(self.db, now=self.now, dispatch=self._dispatch,
                      guard=running_builds.mode_guard_check, on_stale=escalations.append)
        schedule.poll(self.db, now=self.now + timedelta(minutes=1), dispatch=self._dispatch,
                      guard=running_builds.mode_guard_check, on_stale=escalations.append)
        self.assertEqual(len(escalations), 1)
        self.assertEqual(self.fired, [])  # stale never fires


if __name__ == "__main__":
    unittest.main()
