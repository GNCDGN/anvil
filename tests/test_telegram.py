"""Step 5 tests — TelegramClient, fully mocked (NO real network).

The async seams (_send_message / _poll_updates / _send_typing) are patched;
PTB/Telegram is never actually contacted. anvil.telegram.time.sleep is
patched to no-op so retry backoff is instant. The marker-file test is
hermetic via a tmp ANVIL_STATE_DIR.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anvil.telegram import (
    Reply,
    TelegramClient,
    _Upd,
    _on_sigint,
    clear_interrupt,
)

CHAT = "999"


class TestTelegramSend(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("ANVIL_STATE_DIR")
        self._dir = Path(tempfile.mkdtemp(prefix="anvil-test-tg-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._dir)
        self.c = TelegramClient("tok", CHAT)
        self._sleep = patch("anvil.telegram.time.sleep", lambda *_: None)
        self._sleep.start()

    def tearDown(self) -> None:
        self._sleep.stop()
        if self._prev is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_send_success(self) -> None:
        with patch.object(self.c, "_send_message", return_value=42) as m:
            self.assertEqual(self.c.send("[ANVIL] hi"), 42)
        m.assert_called_once_with("[ANVIL] hi")
        self.assertFalse((self._dir / "telegram-down.marker").exists())

    def test_send_retries_then_succeeds(self) -> None:
        with patch.object(
            self.c, "_send_message",
            side_effect=[RuntimeError("net"), RuntimeError("net"), 7],
        ) as m:
            self.assertEqual(self.c.send("x"), 7)
        self.assertEqual(m.call_count, 3)
        self.assertFalse((self._dir / "telegram-down.marker").exists())

    def test_send_all_fail_returns_minus1_and_writes_marker(self) -> None:
        with patch.object(
            self.c, "_send_message", side_effect=RuntimeError("down")
        ) as m:
            self.assertEqual(self.c.send("payload-text"), -1)
        self.assertEqual(m.call_count, 3)  # max_send_retries
        marker = self._dir / "telegram-down.marker"
        self.assertTrue(marker.exists())
        self.assertIn("payload-text", marker.read_text())

    def test_send_typing_nonfatal(self) -> None:
        with patch.object(
            self.c, "_send_typing", side_effect=RuntimeError("boom")
        ):
            self.c.send_typing()  # must not raise


class TestTelegramPoll(unittest.TestCase):
    def setUp(self) -> None:
        self.c = TelegramClient("tok", CHAT, long_poll_seconds=30)
        self._sleep = patch("anvil.telegram.time.sleep", lambda *_: None)
        self._sleep.start()

    def tearDown(self) -> None:
        self._sleep.stop()

    def test_wait_for_reply_returns_reply(self) -> None:
        seq = [
            [],  # baseline (offset=None, timeout=0)
            [_Upd(5, int(CHAT), 11, "ack", 1234)],  # loop poll
        ]
        with patch.object(self.c, "_poll_updates", side_effect=seq):
            r = self.c.wait_for_reply(timeout=5)
        self.assertIsInstance(r, Reply)
        self.assertEqual(r.text, "ack")
        self.assertEqual(r.message_id, 11)
        self.assertEqual(r.timestamp, 1234)

    def test_filters_other_chat_and_tracks_update_ids(self) -> None:
        # baseline []; poll1 → a message from a DIFFERENT chat (id 8);
        # poll2 must be called with offset=9 (8+1) and returns the real
        # reply from our chat → proves last_update_id tracking / no reprocess.
        seq = [
            [],                                   # baseline
            [_Upd(8, 111, 80, "not for anvil", 1)],   # foreign chat
            [_Upd(9, int(CHAT), 90, "ack", 2)],       # real reply
        ]
        with patch.object(self.c, "_poll_updates", side_effect=seq) as m:
            r = self.c.wait_for_reply(timeout=5)
        self.assertEqual(r.text, "ack")
        self.assertEqual(r.message_id, 90)
        # call 0 = baseline (None, 0); call 1 = (last+1, 30) where last=0;
        # call 2 = (9, 30) because the foreign update_id 8 advanced last_id.
        offsets = [call.args[0] for call in m.call_args_list]
        self.assertEqual(offsets[0], None)   # baseline
        self.assertEqual(offsets[1], 1)      # 0 + 1
        self.assertEqual(offsets[2], 9)      # 8 + 1  (foreign update consumed)

    def test_timeout_returns_none(self) -> None:
        seq = [[], [], []]  # baseline + empty polls
        with patch.object(self.c, "_poll_updates", side_effect=lambda *a, **k: []):
            self.assertIsNone(self.c.wait_for_reply(timeout=0))

    def test_poll_exception_is_swallowed_then_recovers(self) -> None:
        calls = {"n": 0}

        def flaky(offset, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return []  # baseline
            if calls["n"] == 2:
                raise RuntimeError("transient poll error")
            return [_Upd(3, int(CHAT), 30, "ack", 9)]

        with patch.object(self.c, "_poll_updates", side_effect=flaky):
            r = self.c.wait_for_reply(timeout=5)
        self.assertEqual(r.text, "ack")  # recovered after the swallowed error


class TestTelegramInterrupt(unittest.TestCase):
    """Phase B hotfix: a SIGINT (simulated by invoking the real _on_sigint
    handler, which only sets the module flag) must make wait_for_reply raise
    KeyboardInterrupt between poll cycles — never swallowed, never deferred
    past the next get_updates."""

    def setUp(self) -> None:
        clear_interrupt()
        self.c = TelegramClient("tok", CHAT, long_poll_seconds=30)
        self._sleep = patch("anvil.telegram.time.sleep", lambda *_: None)
        self._sleep.start()

    def tearDown(self) -> None:
        self._sleep.stop()
        clear_interrupt()  # never leak the flag into other tests

    def test_interrupt_before_wait_raises_before_any_poll(self) -> None:
        _on_sigint(2, None)  # SIGINT arrived before we even start waiting
        with patch.object(self.c, "_poll_updates") as m:
            with self.assertRaises(KeyboardInterrupt):
                self.c.wait_for_reply(timeout=None)
        m.assert_not_called()  # raised before issuing any get_updates

    def test_interrupt_mid_loop_raises_on_next_iteration(self) -> None:
        calls = {"n": 0}

        def poll(offset, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return []          # baseline (offset=None, timeout=0)
            if calls["n"] == 2:
                _on_sigint(2, None)  # SIGINT lands during this long-poll
                return []          # poll returns normally, no reply
            raise AssertionError("a 3rd poll must NOT be issued after interrupt")

        with patch.object(self.c, "_poll_updates", side_effect=poll):
            with self.assertRaises(KeyboardInterrupt):
                self.c.wait_for_reply(timeout=None)
        self.assertEqual(calls["n"], 2)  # baseline + 1 loop poll, then raise


if __name__ == "__main__":
    unittest.main()
