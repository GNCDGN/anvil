"""Mock-first tests for the v5 Phase 1b schedule trigger. Local temp SQLite;
no VPS, no network."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from anvil.monitor import anvil_ops, schedule


class TestMatcher(unittest.TestCase):
    def test_hourly(self):
        now = datetime(2026, 6, 9, 22, 30, 15)
        self.assertEqual(schedule.most_recent_due("@hourly", now),
                         datetime(2026, 6, 9, 22, 0, 0))

    def test_daily_after_time(self):
        now = datetime(2026, 6, 9, 22, 30)
        self.assertEqual(schedule.most_recent_due("@daily 22:00", now),
                         datetime(2026, 6, 9, 22, 0))

    def test_daily_before_time_rolls_back(self):
        now = datetime(2026, 6, 9, 6, 0)  # before 22:00 → yesterday's 22:00
        self.assertEqual(schedule.most_recent_due("@daily 22:00", now),
                         datetime(2026, 6, 8, 22, 0))

    def test_weekly_same_day_after(self):
        now = datetime(2026, 6, 9, 22, 30)  # 2026-06-09 is a Tuesday
        dow = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
        mrd = schedule.most_recent_due(f"@weekly {dow} 22:00", now)
        self.assertEqual(mrd, datetime(2026, 6, 9, 22, 0))

    def test_weekly_rolls_back_a_week(self):
        now = datetime(2026, 6, 9, 22, 30)  # Tuesday
        dow = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
        # same dow but a time later than now → must roll back 7 days
        mrd = schedule.most_recent_due(f"@weekly {dow} 23:00", now)
        self.assertEqual(mrd, datetime(2026, 6, 2, 23, 0))

    def test_is_due_never_fired(self):
        now = datetime(2026, 6, 9, 22, 30)
        self.assertTrue(schedule.is_due("@daily 22:00", now, None))

    def test_is_due_already_fired_this_window(self):
        now = datetime(2026, 6, 9, 22, 30)
        # last_fired after the most-recent-due (22:00) → not due
        self.assertFalse(schedule.is_due("@daily 22:00", now, "2026-06-09T22:00:05"))

    def test_is_due_missed_trigger_during_downtime(self):
        now = datetime(2026, 6, 9, 22, 30)
        # last_fired is yesterday → today's 22:00 passed unfired → due (fire once)
        self.assertTrue(schedule.is_due("@daily 22:00", now, "2026-06-08T22:00:01"))

    def test_unparseable_never_raises_not_due(self):
        now = datetime(2026, 6, 9, 22, 30)
        self.assertFalse(schedule.is_due("@nonsense xyz", now, None))


class TestPoll(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(self.db)
        anvil_ops.init_db(self.db)
        self.calls = []

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _dispatch(self, task):
        self.calls.append(task["task_id"]); return {"ok": True}

    def test_due_task_fires(self):
        anvil_ops.add_scheduled_task(self.db, "weekly", "@daily 22:00", "b.md")
        now = datetime(2026, 6, 9, 22, 30)
        res = schedule.poll(self.db, now=now, dispatch=self._dispatch)
        self.assertTrue(res["ok"])
        self.assertEqual(res["fired"], ["weekly"])
        self.assertEqual(self.calls, ["weekly"])
        # last_fired advanced + trigger_log row 'fired'
        self.assertIsNotNone(anvil_ops.list_scheduled_tasks(self.db)["result"][0]["last_fired"])
        self.assertEqual(anvil_ops.list_triggers(self.db)["result"][0]["disposition"], "fired")

    def test_idempotent_same_window(self):
        anvil_ops.add_scheduled_task(self.db, "t", "@daily 22:00", "b.md")
        now = datetime(2026, 6, 9, 22, 30)
        schedule.poll(self.db, now=now, dispatch=self._dispatch)
        # a second poll in the same window must not re-fire
        res2 = schedule.poll(self.db, now=now + timedelta(seconds=30), dispatch=self._dispatch)
        self.assertEqual(res2["fired"], [])
        self.assertEqual(self.calls, ["t"])  # dispatched once

    def test_not_due_does_not_fire(self):
        anvil_ops.add_scheduled_task(self.db, "t", "@daily 22:00", "b.md")
        now = datetime(2026, 6, 9, 6, 0)  # before 22:00; last_fired None → yesterday's 22:00 is due
        # but if last_fired is today-ish (after yesterday 22:00), not due:
        anvil_ops.mark_task_fired(self.db, "t", "2026-06-09T00:00:00")
        res = schedule.poll(self.db, now=now, dispatch=self._dispatch)
        self.assertEqual(res["fired"], [])

    def test_poll_never_raises_on_bad_db(self):
        res = schedule.poll("/nonexistent-dir-xyz/x.db", now=datetime.now(), dispatch=self._dispatch)
        self.assertFalse(res["ok"])
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()
