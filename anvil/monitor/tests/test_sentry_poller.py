"""Mock-first tests for the v5 Phase 1c Sentry trigger. Local temp SQLite;
sentry.list_issues + the Telegram send are mocked — no Sentry account, no
network (brief Amendment 1: the live probe is deferred to Phase 2)."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from anvil.monitor import anvil_ops, sentry_poller


def _issue(id_, level="error", title="boom"):
    return {"id": str(id_), "level": level, "title": title}


class TestEligibility(unittest.TestCase):
    def test_level_threshold_default_error(self):
        self.assertTrue(sentry_poller.is_eligible(_issue(1, "error"), min_level=3))
        self.assertTrue(sentry_poller.is_eligible(_issue(2, "fatal"), min_level=3))
        self.assertFalse(sentry_poller.is_eligible(_issue(3, "warning"), min_level=3))
        self.assertFalse(sentry_poller.is_eligible(_issue(4, "info"), min_level=3))

    def test_noise_regex_filters_title(self):
        import re
        noise = re.compile(r"healthcheck")
        self.assertFalse(
            sentry_poller.is_eligible(_issue(1, "error", "healthcheck ping"),
                                      min_level=3, noise=noise))
        self.assertTrue(
            sentry_poller.is_eligible(_issue(2, "error", "real crash"),
                                      min_level=3, noise=noise))

    def test_unknown_level_defaults_error(self):
        # an unrecognised level is treated as error (3) — fail-open on severity
        # so a malformed level never silently drops a real alert.
        self.assertTrue(sentry_poller.is_eligible(_issue(1, "weird"), min_level=3))


class TestPoll(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(self.db)
        anvil_ops.init_db(self.db)
        self.routed = []

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _dispatch(self, issue):
        self.routed.append(issue["id"]); return {"ok": True}

    def _ok(self, issues):
        return {"ok": True, "result": issues}

    def test_routes_eligible_skips_ineligible(self):
        issues = [_issue(1, "error"), _issue(2, "warning"), _issue(3, "fatal")]
        with mock.patch.object(sentry_poller.sentry, "list_issues", return_value=self._ok(issues)):
            res = sentry_poller.poll(self.db, project="proj", dispatch=self._dispatch)
        self.assertTrue(res["ok"])
        self.assertEqual(set(res["routed"]), {"sentry:1", "sentry:3"})
        self.assertEqual(self.routed, ["1", "3"])

    def test_idempotent_no_double_route(self):
        issues = [_issue(1, "error")]
        with mock.patch.object(sentry_poller.sentry, "list_issues", return_value=self._ok(issues)):
            sentry_poller.poll(self.db, project="proj", dispatch=self._dispatch)
            res2 = sentry_poller.poll(self.db, project="proj", dispatch=self._dispatch)
        self.assertEqual(res2["routed"], [])          # already routed last poll
        self.assertEqual(self.routed, ["1"])          # dispatched exactly once

    def test_dispatch_failure_marks_disposition(self):
        issues = [_issue(1, "error")]
        with mock.patch.object(sentry_poller.sentry, "list_issues", return_value=self._ok(issues)):
            sentry_poller.poll(self.db, project="proj",
                               dispatch=lambda i: {"ok": False, "error": "telegram down"})
        rows = anvil_ops.list_triggers(self.db)["result"]
        self.assertEqual(rows[0]["disposition"], "dispatch-failed")

    def test_missing_project_errors(self):
        # no SENTRY_PROJECT, no project arg → structured error, never raises
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SENTRY_PROJECT", None)
            res = sentry_poller.poll(self.db, dispatch=self._dispatch)
        self.assertFalse(res["ok"])
        self.assertIn("SENTRY_PROJECT", res["error"])

    def test_list_issues_error_propagates(self):
        with mock.patch.object(sentry_poller.sentry, "list_issues",
                               return_value={"ok": False, "error": "HTTP 401"}):
            res = sentry_poller.poll(self.db, project="proj", dispatch=self._dispatch)
        self.assertFalse(res["ok"])
        self.assertIn("401", res["error"])

    def test_never_raises_on_exploding_dispatch(self):
        issues = [_issue(1, "error")]
        def boom(issue):
            raise RuntimeError("kaboom")
        with mock.patch.object(sentry_poller.sentry, "list_issues", return_value=self._ok(issues)):
            res = sentry_poller.poll(self.db, project="proj", dispatch=boom)
        self.assertTrue(res["ok"])
        rows = anvil_ops.list_triggers(self.db)["result"]
        self.assertEqual(rows[0]["disposition"], "dispatch-failed")


class TestModeGuardDefer(unittest.TestCase):
    """The Step-2 mode-guard hooks, exercised here with injected guards so the
    poller's defer/stale branches are covered at Step 1 (dormant in production
    until Step 2 wires running_builds.mode_guard_check)."""
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.unlink(self.db)
        anvil_ops.init_db(self.db)
        self.routed = []

    def tearDown(self):
        if os.path.exists(self.db):
            os.unlink(self.db)

    def _ok(self, issues):
        return {"ok": True, "result": issues}

    def test_active_build_defers_not_routes(self):
        issues = [_issue(1, "error")]
        guard = lambda db, now=None: {"active": True, "stale": False}
        with mock.patch.object(sentry_poller.sentry, "list_issues", return_value=self._ok(issues)):
            res = sentry_poller.poll(self.db, project="proj",
                                     dispatch=lambda i: self.routed.append(i["id"]) or {"ok": True},
                                     guard=guard)
        self.assertEqual(res["routed"], [])
        self.assertEqual(res["deferred"], ["sentry:1"])
        self.assertEqual(self.routed, [])
        rows = {r["trigger_id"]: r["disposition"] for r in anvil_ops.list_triggers(self.db)["result"]}
        self.assertEqual(rows["sentry:1:deferred"], "deferred-active-build")

    def test_clears_after_defer_then_fires(self):
        issues = [_issue(1, "error")]
        active = {"active": True, "stale": False}
        guard = lambda db, now=None: active
        with mock.patch.object(sentry_poller.sentry, "list_issues", return_value=self._ok(issues)):
            sentry_poller.poll(self.db, project="proj",
                               dispatch=lambda i: self.routed.append(i["id"]) or {"ok": True}, guard=guard)
            active["active"] = False  # build completes
            res2 = sentry_poller.poll(self.db, project="proj",
                                      dispatch=lambda i: self.routed.append(i["id"]) or {"ok": True}, guard=guard)
        self.assertEqual(res2["routed"], ["sentry:1"])
        self.assertEqual(self.routed, ["1"])

    def test_stale_escalates_once(self):
        issues = [_issue(1, "error")]
        guard = lambda db, now=None: {"active": True, "stale": True}
        escalations = []
        with mock.patch.object(sentry_poller.sentry, "list_issues", return_value=self._ok(issues)):
            sentry_poller.poll(self.db, project="proj", dispatch=lambda i: {"ok": True},
                               guard=guard, on_stale=lambda t: escalations.append(t))
            sentry_poller.poll(self.db, project="proj", dispatch=lambda i: {"ok": True},
                               guard=guard, on_stale=lambda t: escalations.append(t))
        self.assertEqual(len(escalations), 1)  # escalate-once per stale incident


class TestNotify(unittest.TestCase):
    def test_missing_token_never_raises(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            res = sentry_poller._notify("hi")
        self.assertFalse(res["ok"])
        self.assertIn("missing telegram", res["error"])

    def test_send_ok(self):
        fake = mock.MagicMock()
        fake.read.return_value = b'{"ok": true, "result": {"message_id": 42}}'
        cm = mock.MagicMock(); cm.__enter__.return_value = fake
        with mock.patch.object(sentry_poller, "urlopen", return_value=cm):
            res = sentry_poller._notify("hi", token="t", chat_id="c")
        self.assertTrue(res["ok"])
        self.assertEqual(res["message_id"], 42)


if __name__ == "__main__":
    unittest.main()
