"""Mock-first tests for the monitor wake-send (v5 Phase 1b, item E). No network."""
import io
import unittest
from unittest import mock

from anvil.monitor import wake


def _fake_resp(payload: bytes):
    cm = mock.MagicMock()
    cm.__enter__.return_value = io.BytesIO(payload)
    cm.__exit__.return_value = False
    return cm


class TestWakeText(unittest.TestCase):
    def test_format(self):
        t = wake.wake_text({"task_id": "weekly", "brief_path": "builds/x/brief.md"})
        self.assertIn("[ANVIL] Wake", t)
        self.assertIn("task weekly", t)
        self.assertIn("brief builds/x/brief.md", t)
        self.assertIn("reply 'go builds/x/brief.md'", t)
        self.assertIn("'skip'", t)


class TestSendWake(unittest.TestCase):
    def test_success_returns_message_id(self):
        with mock.patch.object(
            wake, "urlopen",
            return_value=_fake_resp(b'{"ok": true, "result": {"message_id": 42}}'),
        ):
            r = wake.send_wake({"task_id": "t", "brief_path": "b.md"},
                               token="TOK", chat_id="123")
        self.assertTrue(r["ok"])
        self.assertEqual(r["message_id"], 42)

    def test_missing_token_structured_error(self):
        r = wake.send_wake({"task_id": "t", "brief_path": "b.md"}, token="", chat_id="")
        self.assertFalse(r["ok"])
        self.assertIn("missing", r["error"])

    def test_telegram_not_ok_structured_error(self):
        with mock.patch.object(
            wake, "urlopen",
            return_value=_fake_resp(b'{"ok": false, "description": "chat not found"}'),
        ):
            r = wake.send_wake({"task_id": "t", "brief_path": "b.md"}, token="T", chat_id="1")
        self.assertFalse(r["ok"])
        self.assertIn("chat not found", r["error"])

    def test_http_error_never_raises(self):
        from urllib.error import HTTPError
        with mock.patch.object(
            wake, "urlopen",
            side_effect=HTTPError("u", 401, "unauth", {}, None),
        ):
            r = wake.send_wake({"task_id": "t", "brief_path": "b.md"}, token="T", chat_id="1")
        self.assertFalse(r["ok"])
        self.assertIn("401", r["error"])

    def test_unexpected_exception_never_raises(self):
        with mock.patch.object(wake, "urlopen", side_effect=RuntimeError("boom")):
            r = wake.send_wake({"task_id": "t", "brief_path": "b.md"}, token="T", chat_id="1")
        self.assertFalse(r["ok"])
        self.assertIn("send_wake", r["error"])


if __name__ == "__main__":
    unittest.main()
