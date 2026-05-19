"""v2 Phase 1 Step 3 — telegram send + wait_for_reply event instrumentation.

Mocks the async seams (_send_message, _poll_updates) so no real network
fires. Hermetic ANVIL_ROOT redirect under tmp_path; backoff sleep
patched so retry tests are instant.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from anvil import events
from anvil.telegram import (
    Reply,
    TelegramClient,
    _Upd,
    clear_interrupt,
)

CHAT = "12345"


class _TelegramEventsBase(unittest.TestCase):

    def setUp(self) -> None:
        # Module state reset.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()
        clear_interrupt()

        # ANVIL_STATE_DIR governs the telegram-down.marker path.
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-tg-events-"))
        self._prev_state = os.environ.get("ANVIL_STATE_DIR")
        self._prev_root = os.environ.get("ANVIL_ROOT")
        os.environ["ANVIL_STATE_DIR"] = str(self._tmp / "state")
        os.environ["ANVIL_ROOT"] = str(self._tmp)

        # Instant retries.
        self._sleep_patch = mock.patch("anvil.telegram.time.sleep",
                                       lambda *_: None)
        self._sleep_patch.start()

        events.begin_run("tg-events-test")
        self.c = TelegramClient("tok", CHAT, long_poll_seconds=30)

    def tearDown(self) -> None:
        events.end_run()
        self._sleep_patch.stop()
        for k, prev in (("ANVIL_STATE_DIR", self._prev_state),
                        ("ANVIL_ROOT", self._prev_root)):
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _events(self) -> list[dict]:
        path = (self._tmp / "state" / "runs" / "tg-events-test"
                / "events.jsonl")
        if not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]


class TestTelegramSendEvents(_TelegramEventsBase):

    def test_send_happy_first_attempt(self) -> None:
        with mock.patch.object(self.c, "_send_message", return_value=42):
            mid = self.c.send("hello")
        self.assertEqual(mid, 42)
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("telegram.send.start", kinds)
        self.assertIn("telegram.send.end", kinds)
        end = next(e for e in self._events() if e["kind"] == "telegram.send.end")
        self.assertTrue(end["data"]["ok"])
        self.assertEqual(end["data"]["retry_count"], 0)
        self.assertEqual(end["data"]["message_chars"], len("hello"))

    def test_send_all_retries_fail_emit_ok_false(self) -> None:
        with mock.patch.object(
            self.c, "_send_message", side_effect=RuntimeError("down"),
        ):
            mid = self.c.send("payload-text")
        self.assertEqual(mid, -1)
        end = next(e for e in self._events() if e["kind"] == "telegram.send.end")
        self.assertFalse(end["data"]["ok"])
        self.assertEqual(end["data"]["retry_count"], self.c.max_send_retries)
        # Exactly one start + one end — retries don't emit per-attempt pairs.
        sends = [e for e in self._events() if e["kind"].startswith("telegram.send.")]
        self.assertEqual(len(sends), 2)


class TestTelegramPollEvents(_TelegramEventsBase):

    def test_wait_for_reply_emits_poll_start_and_reply(self) -> None:
        seq = [
            [],                                          # baseline
            [_Upd(5, int(CHAT), 11, "ack", 1234)],        # real reply
        ]
        with mock.patch.object(self.c, "_poll_updates", side_effect=seq):
            r = self.c.wait_for_reply(timeout=5)
        self.assertIsInstance(r, Reply)
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("telegram.poll.start", kinds)
        self.assertIn("telegram.poll.reply", kinds)
        reply_evt = next(
            e for e in self._events() if e["kind"] == "telegram.poll.reply"
        )
        self.assertEqual(reply_evt["data"]["reply_text_chars"], len("ack"))
        self.assertEqual(reply_evt["data"]["update_id"], 5)
        self.assertGreaterEqual(reply_evt["data"]["duration_ms"], 0)

    def test_wait_for_reply_timeout_emits_only_poll_start(self) -> None:
        # Baseline + empty polls; timeout=0 → first cycle's post-poll check
        # fires the timeout-no-reply path. No poll.reply emit expected.
        with mock.patch.object(self.c, "_poll_updates",
                               side_effect=lambda *a, **k: []):
            r = self.c.wait_for_reply(timeout=0)
        self.assertIsNone(r)
        poll_kinds = [
            e["kind"] for e in self._events()
            if e["kind"].startswith("telegram.poll.")
        ]
        self.assertEqual(poll_kinds, ["telegram.poll.start"])


if __name__ == "__main__":
    unittest.main()
