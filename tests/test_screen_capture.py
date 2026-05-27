"""Tests for anvil.integrations.screen_capture — v4 Phase 3a Step 1.

Mock-first at the pyobjc boundary (Q-A2 / Amendment 2): every test patches the
module-level framework refs (``_SCK`` / ``_AS`` / ``_CG``) and/or the capture/AX
internals, so no live ScreenCaptureKit / AXUIElement call runs. The live
multi-async pyobjc graph (SCShareableContent enumeration, the SCScreenshotManager
completion-handler→sync bridge, the AX tree walk, the CGImage→PNG encode) is the
Phase 3c live-ratification surface; these tests exercise the CONTRACT — never-raises,
permission-gating, not-started guards, wrapper-is-the-seam (no type leak, DC4),
observation-only (F6 / Q-A0b), and the result shapes. Hermetic: green whether or
not the frameworks import. Mirrors test_browser.py.
"""
import unittest
from unittest import mock

import anvil.integrations.screen_capture as sc


def _all_primitives(obj) -> bool:
    """Recursively assert obj is dict/list/str/int/float/bool/bytes/None only —
    no MagicMock / ScreenCaptureKit / AXUIElement / CGImage object leaked (DC4)."""
    if isinstance(obj, dict):
        return all(isinstance(k, str) and _all_primitives(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return all(_all_primitives(x) for x in obj)
    return obj is None or isinstance(obj, (str, int, float, bool, bytes))


def _frameworks_present():
    """Patch the module-level pyobjc refs to MagicMocks + frameworks-importable."""
    return mock.patch.multiple(
        sc,
        _SCK=mock.MagicMock(name="ScreenCaptureKit"),
        _AS=mock.MagicMock(name="ApplicationServices"),
        _CG=mock.MagicMock(name="Quartz"),
        _IMPORT_ERROR=None,
    )


_ELEMENTS = [{"role": "AXButton", "label": "CX22", "frame": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}}]
_PNG = b"\x89PNG\r\n\x1a\n_fake_frame_bytes"


class TestScreenCaptureHappyPath(unittest.TestCase):
    def test_start_query_snapshot_stop(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            sc._AS.AXIsProcessTrusted.return_value = True
            s = sc.ScreenCaptureSession()

            r = s.start_capture()
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"], {"started": True})

            with mock.patch.object(s, "_walk_accessibility", return_value=_ELEMENTS):
                r = s.query_accessibility("cx22")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"]["elements"][0]["role"], "AXButton")
            self.assertEqual(r["result"]["query"], "cx22")

            with mock.patch.object(s, "_capture_single_frame", return_value=(_PNG, 1440, 900)):
                r = s.snapshot_frame()
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"], {"frame_png": _PNG, "width": 1440, "height": 900})

            r = s.stop_capture()
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"], {"stopped": True})


class TestScreenCaptureErrorPaths(unittest.TestCase):
    def test_frameworks_absent(self):
        with mock.patch.multiple(sc, _SCK=None, _AS=None, _CG=None,
                                 _IMPORT_ERROR=ImportError("No module named 'ScreenCaptureKit'")):
            r = sc.ScreenCaptureSession().start_capture()
            self.assertFalse(r["ok"])
            self.assertIn("not available", r["error"])

    def test_screen_permission_denied(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = False
            r = sc.ScreenCaptureSession().start_capture()
            self.assertFalse(r["ok"])
            self.assertIn("screen recording permission required", r["error"])

    def test_accessibility_permission_denied(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            sc._AS.AXIsProcessTrusted.return_value = False
            s = sc.ScreenCaptureSession()
            s.start_capture()
            r = s.query_accessibility()
            self.assertFalse(r["ok"])
            self.assertIn("accessibility permission required", r["error"])

    def test_snapshot_capture_raises_is_caught(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            s = sc.ScreenCaptureSession()
            s.start_capture()
            with mock.patch.object(s, "_capture_single_frame", side_effect=RuntimeError("SCK boom")):
                r = s.snapshot_frame()  # must NOT raise
            self.assertFalse(r["ok"])
            self.assertIn("unexpected error (snapshot_frame)", r["error"])

    def test_snapshot_no_image(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            s = sc.ScreenCaptureSession()
            s.start_capture()
            with mock.patch.object(s, "_capture_single_frame", return_value=(b"", 0, 0)):
                r = s.snapshot_frame()
            self.assertFalse(r["ok"])
            self.assertIn("no image", r["error"])

    def test_accessibility_walk_raises_is_caught(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            sc._AS.AXIsProcessTrusted.return_value = True
            s = sc.ScreenCaptureSession()
            s.start_capture()
            with mock.patch.object(s, "_walk_accessibility", side_effect=RuntimeError("AX boom")):
                r = s.query_accessibility()  # must NOT raise
            self.assertFalse(r["ok"])
            self.assertIn("unexpected error (query_accessibility)", r["error"])


class TestScreenCaptureGuards(unittest.TestCase):
    def test_query_before_start(self):
        with _frameworks_present():
            r = sc.ScreenCaptureSession().query_accessibility()
            self.assertFalse(r["ok"])
            self.assertIn("not started", r["error"])

    def test_snapshot_before_start(self):
        with _frameworks_present():
            r = sc.ScreenCaptureSession().snapshot_frame()
            self.assertFalse(r["ok"])
            self.assertIn("not started", r["error"])

    def test_double_start(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            s = sc.ScreenCaptureSession()
            self.assertTrue(s.start_capture()["ok"])
            r = s.start_capture()
            self.assertFalse(r["ok"])
            self.assertIn("already started", r["error"])

    def test_stop_is_idempotent_and_never_raises(self):
        with _frameworks_present():
            s = sc.ScreenCaptureSession()
            self.assertTrue(s.stop_capture()["ok"])  # stop before start: fine
            self.assertTrue(s.stop_capture()["ok"])  # idempotent


class TestScreenCaptureNoTypeLeak(unittest.TestCase):
    def test_all_returns_are_primitives(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            sc._AS.AXIsProcessTrusted.return_value = True
            s = sc.ScreenCaptureSession()
            results = [s.start_capture(), s.stop_capture()]
            s.start_capture()
            with mock.patch.object(s, "_walk_accessibility", return_value=_ELEMENTS):
                results.append(s.query_accessibility("x"))
            with mock.patch.object(s, "_capture_single_frame", return_value=(_PNG, 10, 20)):
                results.append(s.snapshot_frame())
            for r in results:
                self.assertTrue(_all_primitives(r), f"non-primitive leaked: {r}")


class TestScreenCaptureNoActuation(unittest.TestCase):
    def test_observation_only_surface(self):
        # The public surface is exactly the observation lifecycle — no actuation.
        public = {n for n in dir(sc.ScreenCaptureSession) if not n.startswith("_")}
        self.assertEqual(
            public, {"start_capture", "query_accessibility", "snapshot_frame", "stop_capture"}
        )
        forbidden = {"click", "type", "fill", "press", "move", "move_to", "key",
                     "key_down", "scroll", "drag", "tap", "actuate", "send_keys"}
        self.assertEqual(public & forbidden, set())


class TestScreenCaptureContextManager(unittest.TestCase):
    def test_context_manager_starts_and_stops(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            with sc.ScreenCaptureSession() as s:
                self.assertTrue(s._started)
            self.assertFalse(s._started)  # __exit__ stopped it

    def test_context_manager_never_suppresses(self):
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            with self.assertRaises(ValueError):
                with sc.ScreenCaptureSession():
                    raise ValueError("propagates")


class TestScreenCaptureAccessibilityFirst(unittest.TestCase):
    def test_query_and_snapshot_are_separate_methods(self):
        # Accessibility-first / vision-fallback (Q-A4): query_accessibility does
        # NOT trigger a frame capture; vision is opted-in only via snapshot_frame.
        with _frameworks_present():
            sc._CG.CGPreflightScreenCaptureAccess.return_value = True
            sc._AS.AXIsProcessTrusted.return_value = True
            s = sc.ScreenCaptureSession()
            s.start_capture()
            with mock.patch.object(s, "_capture_single_frame") as cap, \
                 mock.patch.object(s, "_walk_accessibility", return_value=_ELEMENTS):
                s.query_accessibility()
            cap.assert_not_called()  # accessibility path never invokes vision


if __name__ == "__main__":
    unittest.main()
