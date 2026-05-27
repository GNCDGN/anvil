"""v4 Phase 3c Step 2 — the co-pilot session runner (DC7).

Hermetic: the screen substrate is MOCKED at the copilot_runner import site
(anvil.copilot_runner.screen_capture / screen_browser); the seam
(routing.call_model_for_subtask), the visibility_session co-pilot keyspace, and
events.emit are patched; a FakeTelegram stands in for the channel. The real
copilot opt-in state (in-memory) runs unmocked so the grant logic is exercised.
Covers: the capture-interpret-guide loop, session_id reconciliation, the
screen.captured (mode=co-pilot) emits, the Telegram opt-in grant, capture-only
continuation, the tab:// dispatch, and end_session expiry.
"""
from __future__ import annotations

import unittest
from collections import deque
from unittest import mock

from anvil import copilot, copilot_runner
from anvil.copilot import AUTONOMOUS_OPT_IN_TOKEN


def _ok_screen_session(*, frame_png=b"\x89PNGcp", width=2560, height=1440,
                       ax_elements=None, start_ok=True, frame_ok=True,
                       ax_ok=True):
    sess = mock.MagicMock(name="ScreenCaptureSession")
    sess.start_capture.return_value = (
        {"ok": True, "result": {"started": True}} if start_ok
        else {"ok": False, "error": "permission required"})
    sess.snapshot_frame.return_value = (
        {"ok": True, "result": {"frame_png": frame_png, "width": width,
                                "height": height}} if frame_ok
        else {"ok": False, "error": "no image"})
    sess.query_accessibility.return_value = (
        {"ok": True, "result": {"elements": ax_elements if ax_elements
                                is not None else [{"role": "AXWindow",
                                                   "label": "X", "frame": None}],
                                "query": None}} if ax_ok
        else {"ok": False, "error": "ax permission"})
    sess.stop_capture.return_value = {"ok": True, "result": {"stopped": True}}
    return sess


def _ok_tab_session(*, frame_png=b"\x89PNGtab"):
    sess = mock.MagicMock(name="BrowserExtensionSession")
    sess.connect_extension.return_value = {"ok": True, "result": {"connected": True}}
    sess.capture_tab.return_value = {"ok": True, "result": {
        "frame_png": frame_png, "width": 1280, "height": 720}}
    sess.disconnect.return_value = {"ok": True, "result": {"disconnected": True}}
    return sess


class FakeTelegram:
    def __init__(self, replies=()):
        self.sent: list[str] = []
        self._r = deque(replies)

    def send(self, text):
        self.sent.append(text)
        return len(self.sent)

    def wait_for_reply(self, timeout):
        if not self._r:
            return None
        return _R(self._r.popleft())


class _R:
    def __init__(self, text):
        self.text = text


class _RunnerBase(unittest.TestCase):
    def setUp(self):
        self.p_vis_start = mock.patch(
            "anvil.copilot_runner.visibility_session.start_copilot_session",
            return_value={"ok": True, "result": {"session_id": "cp-TEST",
                                                 "target": "screen://main"}})
        self.p_vis_write = mock.patch(
            "anvil.copilot_runner.visibility_session.write_copilot_capture",
            return_value={"ok": True, "result": {"path": "/p/record.json"}})
        self.p_seam = mock.patch(
            "anvil.copilot_runner.routing.call_model_for_subtask",
            return_value="GUIDE: the screen looks fine.")
        self.p_emit = mock.patch("anvil.copilot_runner._events.emit")
        self.mock_vis_start = self.p_vis_start.start()
        self.mock_vis_write = self.p_vis_write.start()
        self.mock_seam = self.p_seam.start()
        self.mock_emit = self.p_emit.start()
        for p in (self.p_vis_start, self.p_vis_write, self.p_seam, self.p_emit):
            self.addCleanup(p.stop)

    def _screen_emits(self):
        return [c for c in self.mock_emit.call_args_list
                if c[0][0] == "screen.captured"]


class TestCopilotRunnerScreen(_RunnerBase):
    @mock.patch("anvil.copilot_runner.screen_capture.ScreenCaptureSession")
    def test_capture_interpret_guide_loop(self, MockSC):
        MockSC.return_value = _ok_screen_session()
        tg = FakeTelegram()
        summary = copilot_runner.run("screen://main", max_captures=2, telegram=tg)
        self.assertEqual(summary["captures"], 2)
        self.assertEqual(summary["frames"], 2)
        self.assertEqual(summary["scheme"], "screen")
        # the seam was called for the vision digest with the image, model=sonnet
        self.assertEqual(self.mock_seam.call_args[0][0], "sonnet")
        self.assertEqual(self.mock_seam.call_args.kwargs.get("image"), b"\x89PNGcp")
        # write_copilot_capture called twice with the reconciled session id
        self.assertEqual(self.mock_vis_write.call_count, 2)
        self.assertEqual(self.mock_vis_write.call_args_list[0][0][0], "cp-TEST")
        # screen.captured emitted per capture, mode=co-pilot
        emits = self._screen_emits()
        self.assertEqual(len(emits), 2)
        payload = emits[0][0][1]
        self.assertEqual(payload["mode"], "co-pilot")
        self.assertEqual(payload["session_id"], "cp-TEST")
        self.assertTrue(payload["vision_used"])
        self.assertEqual(payload["accessibility_element_count"], 1)
        # the operator was guided
        self.assertEqual(len(tg.sent), 2)
        self.assertIn("GUIDE", tg.sent[0])

    @mock.patch("anvil.copilot_runner.screen_capture.ScreenCaptureSession")
    def test_session_id_reconciled_to_visibility_id(self, MockSC):
        MockSC.return_value = _ok_screen_session()
        with mock.patch("anvil.copilot_runner.copilot.start_session",
                        wraps=copilot.start_session) as spy_start:
            copilot_runner.run("screen://main", max_captures=1)
        # copilot.start_session received the visibility-minted id
        self.assertEqual(spy_start.call_args.kwargs.get("session_id"), "cp-TEST")

    @mock.patch("anvil.copilot_runner.screen_capture.ScreenCaptureSession")
    def test_default_off_no_grant(self, MockSC):
        MockSC.return_value = _ok_screen_session()
        summary = copilot_runner.run("screen://main", max_captures=1,
                                     telegram=FakeTelegram())
        self.assertFalse(summary["autonomous_granted"])

    @mock.patch("anvil.copilot_runner.screen_capture.ScreenCaptureSession")
    def test_cli_autonomous_flag_grants_at_start(self, MockSC):
        MockSC.return_value = _ok_screen_session()
        summary = copilot_runner.run("screen://main", max_captures=1,
                                     autonomous=True, telegram=FakeTelegram())
        self.assertTrue(summary["autonomous_granted"])

    @mock.patch("anvil.copilot_runner.screen_capture.ScreenCaptureSession")
    def test_telegram_token_grants_mid_session(self, MockSC):
        MockSC.return_value = _ok_screen_session()
        tg = FakeTelegram(replies=[AUTONOMOUS_OPT_IN_TOKEN])
        summary = copilot_runner.run("screen://main", max_captures=2, telegram=tg)
        self.assertTrue(summary["autonomous_granted"])
        self.assertTrue(any("autonomous actuation enabled" in s for s in tg.sent))

    @mock.patch("anvil.copilot_runner.screen_capture.ScreenCaptureSession")
    def test_frame_failure_continues_capture_only(self, MockSC):
        MockSC.return_value = _ok_screen_session(frame_ok=False)
        summary = copilot_runner.run("screen://main", max_captures=1,
                                     telegram=FakeTelegram())
        self.assertEqual(summary["captures"], 1)
        self.assertEqual(summary["frames"], 0)
        payload = self._screen_emits()[0][0][1]
        self.assertFalse(payload["vision_used"])  # no frame → no vision
        self.assertEqual(payload["accessibility_element_count"], 1)

    @mock.patch("anvil.copilot_runner.screen_browser.BrowserExtensionSession")
    def test_tab_scheme_uses_extension(self, MockBE):
        MockBE.return_value = _ok_tab_session()
        summary = copilot_runner.run("tab://active", max_captures=1,
                                     surfaces=["frame"], telegram=FakeTelegram())
        self.assertEqual(summary["scheme"], "tab")
        MockBE.return_value.connect_extension.assert_called_once()
        MockBE.return_value.capture_tab.assert_called_once()
        MockBE.return_value.disconnect.assert_called_once()

    @mock.patch("anvil.copilot_runner.screen_capture.ScreenCaptureSession")
    def test_session_ends_after_run(self, MockSC):
        MockSC.return_value = _ok_screen_session()
        captured = {}
        real_end = copilot.end_session

        def _spy_end(session):
            captured["ended_session"] = session
            return real_end(session)

        with mock.patch("anvil.copilot_runner.copilot.end_session",
                        side_effect=_spy_end):
            copilot_runner.run("screen://main", max_captures=1, autonomous=True)
        # end_session ran; the opt-in expired (no carry-over)
        self.assertTrue(captured["ended_session"].ended)
        self.assertFalse(copilot.is_autonomous_enabled(captured["ended_session"]))


if __name__ == "__main__":
    unittest.main()
