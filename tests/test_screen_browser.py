"""Tests for anvil.integrations.screen_browser — v4 Phase 3a Step 2.

Mock-first at the Native Messaging IPC boundary (Q-A3): every test patches
``BrowserExtensionSession._send`` so no extension / host process is spawned. The
live framing + transport + the connectNative lifecycle are the Phase 3c
live-ratification surface; these tests exercise the CONTRACT — never-raises,
not-connected guards, the actuation-refusing stub (Q-A0b / F6),
wrapper-is-the-seam (no type leak, DC4), and the result shapes (symmetric with
screen_capture.snapshot_frame). Hermetic. Mirrors test_screen_capture.py.
"""
import base64
import unittest
from unittest import mock

import anvil.integrations.screen_browser as sb


def _all_primitives(obj) -> bool:
    if isinstance(obj, dict):
        return all(isinstance(k, str) and _all_primitives(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return all(_all_primitives(x) for x in obj)
    return obj is None or isinstance(obj, (str, int, float, bool, bytes))


_PNG = b"\x89PNG\r\n\x1a\n_fake_tab_frame"
_CAPTURE_OK = {
    "ok": True,
    "result": {
        "frame_png_b64": base64.b64encode(_PNG).decode("ascii"),
        "width": 1280,
        "height": 720,
    },
}


def _connected_session(send):
    """Return a session with _send patched to `send` and already connected."""
    s = sb.BrowserExtensionSession()
    s._send = send  # type: ignore[method-assign]
    s._connected = True
    return s


class TestBrowserExtensionHappyPath(unittest.TestCase):
    def test_connect_capture_disconnect(self):
        send = mock.MagicMock(side_effect=[{"ok": True}, _CAPTURE_OK])
        s = sb.BrowserExtensionSession()
        with mock.patch.object(s, "_send", send):
            r = s.connect_extension()
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"], {"connected": True})

            r = s.capture_tab()
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"], {"frame_png": _PNG, "width": 1280, "height": 720})

        r = s.disconnect()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], {"disconnected": True})


class TestBrowserExtensionActuationRefused(unittest.TestCase):
    def test_run_script_is_a_refusing_stub(self):
        # Q-A0b / F6: run_script performs NO actuation in 3a — gated behind the
        # per-session opt-in (3b/3c).
        s = _connected_session(mock.MagicMock())
        r = s.run_script("document.body.click()")
        self.assertFalse(r["ok"])
        self.assertIn("actuation not enabled", r["error"])

    def test_run_script_before_connect(self):
        s = sb.BrowserExtensionSession()
        r = s.run_script("noop")
        self.assertFalse(r["ok"])
        self.assertIn("not connected", r["error"])


class TestBrowserExtensionErrorPaths(unittest.TestCase):
    def test_connect_ping_fails(self):
        s = sb.BrowserExtensionSession()
        with mock.patch.object(s, "_send", return_value={"ok": False, "error": "no host"}):
            r = s.connect_extension()
        self.assertFalse(r["ok"])
        self.assertIn("extension unreachable", r["error"])
        self.assertFalse(s._connected)

    def test_capture_send_raises_is_caught(self):
        s = _connected_session(mock.MagicMock(side_effect=RuntimeError("pipe broke")))
        r = s.capture_tab()  # must NOT raise
        self.assertFalse(r["ok"])
        self.assertIn("unexpected error (capture_tab)", r["error"])

    def test_capture_extension_error(self):
        s = _connected_session(mock.MagicMock(return_value={"ok": False, "error": "tab gone"}))
        r = s.capture_tab()
        self.assertFalse(r["ok"])
        self.assertIn("capture_tab failed", r["error"])

    def test_capture_no_image(self):
        s = _connected_session(mock.MagicMock(return_value={"ok": True, "result": {}}))
        r = s.capture_tab()
        self.assertFalse(r["ok"])
        self.assertIn("no image", r["error"])


class TestBrowserExtensionGuards(unittest.TestCase):
    def test_capture_before_connect(self):
        r = sb.BrowserExtensionSession().capture_tab()
        self.assertFalse(r["ok"])
        self.assertIn("not connected", r["error"])

    def test_double_connect(self):
        s = sb.BrowserExtensionSession()
        with mock.patch.object(s, "_send", return_value={"ok": True}):
            self.assertTrue(s.connect_extension()["ok"])
            r = s.connect_extension()
        self.assertFalse(r["ok"])
        self.assertIn("already connected", r["error"])

    def test_disconnect_idempotent_never_raises(self):
        s = sb.BrowserExtensionSession()
        self.assertTrue(s.disconnect()["ok"])  # before connect: fine
        self.assertTrue(s.disconnect()["ok"])  # idempotent


class TestBrowserExtensionNoTypeLeak(unittest.TestCase):
    def test_all_returns_are_primitives(self):
        send = mock.MagicMock(side_effect=[{"ok": True}, _CAPTURE_OK])
        s = sb.BrowserExtensionSession()
        results = []
        with mock.patch.object(s, "_send", send):
            results.append(s.connect_extension())
            results.append(s.capture_tab())
        results.append(s.run_script("x"))
        results.append(s.disconnect())
        for r in results:
            self.assertTrue(_all_primitives(r), f"non-primitive leaked: {r}")


class TestBrowserExtensionContextManager(unittest.TestCase):
    def test_context_manager_connects_and_disconnects(self):
        with mock.patch.object(sb.BrowserExtensionSession, "_send", return_value={"ok": True}):
            with sb.BrowserExtensionSession() as s:
                self.assertTrue(s._connected)
            self.assertFalse(s._connected)

    def test_context_manager_never_suppresses(self):
        with mock.patch.object(sb.BrowserExtensionSession, "_send", return_value={"ok": True}):
            with self.assertRaises(ValueError):
                with sb.BrowserExtensionSession():
                    raise ValueError("propagates")


class TestBrowserExtensionSurface(unittest.TestCase):
    def test_public_surface(self):
        public = {n for n in dir(sb.BrowserExtensionSession) if not n.startswith("_")}
        self.assertEqual(
            public,
            {"connect_extension", "capture_tab", "run_script", "disconnect"},
        )


if __name__ == "__main__":
    unittest.main()
