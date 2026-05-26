"""Tests for anvil.integrations.browser — v4 Phase 2a Step 1.

Mock at the Playwright boundary (Q-D5): every test patches
``browser.sync_playwright`` so no live browser launches. The suite is hermetic —
green with Playwright importable but no browser process, and green even if the
bundled Chromium is absent (the boundary is mocked). Mirrors github_issues.py's
subprocess-mock precedent (patch the import boundary, assert the wrapper's
behaviour).
"""
import unittest
from unittest import mock

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

import anvil.integrations.browser as browser


def _wire(mock_sync_playwright, *, content="<html><body>ok</body></html>", goto_status=200):
    """Wire a patched ``sync_playwright`` into a full mock object graph
    (cm.start() -> pw.chromium.launch() -> browser.new_page() -> page). Returns a
    small controls object exposing the mock page plus ``fire_console`` /
    ``fire_response`` to trigger the handlers the wrapper registers via page.on."""
    cm = mock.MagicMock(name="context_manager")
    mock_sync_playwright.return_value = cm
    pw = mock.MagicMock(name="playwright")
    cm.start.return_value = pw
    mock_browser = mock.MagicMock(name="browser")
    pw.chromium.launch.return_value = mock_browser
    page = mock.MagicMock(name="page")
    mock_browser.new_page.return_value = page

    handlers = {}
    page.on.side_effect = lambda event, fn: handlers.__setitem__(event, fn)
    page.content.return_value = content
    resp = mock.MagicMock(name="response")
    resp.status = goto_status
    page.goto.return_value = resp

    class _Controls:
        def __init__(self):
            self.cm = cm
            self.pw = pw
            self.browser = mock_browser
            self.page = page
            self.handlers = handlers

        def fire_console(self, msg_type, text):
            m = mock.MagicMock()
            m.type = msg_type
            m.text = text
            handlers["console"](m)

        def fire_response(self, url, status):
            r = mock.MagicMock()
            r.url = url
            r.status = status
            handlers["response"](r)

    return _Controls()


def _all_primitives(obj) -> bool:
    """Recursively assert obj is dict/list/str/int/float/bool/None only — no
    MagicMock / Playwright object leaked through (DC4)."""
    if isinstance(obj, dict):
        return all(isinstance(k, str) and _all_primitives(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return all(_all_primitives(x) for x in obj)
    return obj is None or isinstance(obj, (str, int, float, bool))


class TestBrowserSessionHappyPath(unittest.TestCase):
    @mock.patch.object(browser, "sync_playwright")
    def test_launch_navigate_snapshot_close(self, msp):
        c = _wire(msp, content="<p>hi</p>", goto_status=200)
        s = browser.BrowserSession()

        r = s.launch()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], {"headless": True})

        r = s.navigate("https://example.test/")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], {"url": "https://example.test/", "status": 200})

        r = s.snapshot_dom()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"], {"html": "<p>hi</p>"})

        r = s.close()
        self.assertTrue(r["ok"], r)
        # teardown actually happened
        c.browser.close.assert_called_once()
        c.pw.stop.assert_called_once()

    @mock.patch.object(browser, "sync_playwright")
    def test_launch_headless_flag_threads(self, msp):
        _wire(msp)
        s = browser.BrowserSession()
        r = s.launch(headless=False)
        self.assertEqual(r["result"], {"headless": False})

    @mock.patch.object(browser, "sync_playwright")
    def test_navigate_returns_none_status_when_no_response(self, msp):
        c = _wire(msp)
        c.page.goto.return_value = None  # same-document navigations return None
        s = browser.BrowserSession()
        s.launch()
        r = s.navigate("https://example.test/#frag")
        self.assertTrue(r["ok"])
        self.assertIsNone(r["result"]["status"])


class TestBrowserSessionGuards(unittest.TestCase):
    @mock.patch.object(browser, "sync_playwright")
    def test_double_launch_errors(self, msp):
        _wire(msp)
        s = browser.BrowserSession()
        self.assertTrue(s.launch()["ok"])
        r = s.launch()
        self.assertFalse(r["ok"])
        self.assertIn("already launched", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_navigate_before_launch_errors(self, msp):
        _wire(msp)
        r = browser.BrowserSession().navigate("https://example.test/")
        self.assertFalse(r["ok"])
        self.assertIn("not launched", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_snapshot_before_launch_errors(self, msp):
        _wire(msp)
        r = browser.BrowserSession().snapshot_dom()
        self.assertFalse(r["ok"])
        self.assertIn("not launched", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_capture_before_launch_errors(self, msp):
        _wire(msp)
        s = browser.BrowserSession()
        self.assertFalse(s.capture_console()["ok"])
        self.assertFalse(s.capture_network()["ok"])


class TestBrowserSessionErrorPaths(unittest.TestCase):
    @mock.patch.object(browser, "sync_playwright")
    def test_launch_playwright_error(self, msp):
        c = _wire(msp)
        c.pw.chromium.launch.side_effect = PlaywrightError("kaboom")
        r = browser.BrowserSession().launch()
        self.assertFalse(r["ok"])
        self.assertIn("browser error (launch)", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_launch_missing_browser(self, msp):
        c = _wire(msp)
        c.pw.chromium.launch.side_effect = PlaywrightError(
            "Executable doesn't exist at /path/chrome\nLooks like Playwright was just "
            "installed. Please run: playwright install"
        )
        r = browser.BrowserSession().launch()
        self.assertFalse(r["ok"])
        self.assertIn("browser not installed", r["error"])
        self.assertIn("playwright install chromium", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_launch_timeout(self, msp):
        c = _wire(msp)
        c.pw.chromium.launch.side_effect = PlaywrightTimeout("slow")
        r = browser.BrowserSession().launch()
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_launch_unexpected_error(self, msp):
        c = _wire(msp)
        c.pw.chromium.launch.side_effect = RuntimeError("not a playwright error")
        r = browser.BrowserSession().launch()
        self.assertFalse(r["ok"])
        self.assertIn("unexpected error", r["error"])
        self.assertIn("RuntimeError", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_navigate_timeout(self, msp):
        c = _wire(msp)
        c.page.goto.side_effect = PlaywrightTimeout("slow")
        s = browser.BrowserSession()
        s.launch()
        r = s.navigate("https://example.test/")
        self.assertFalse(r["ok"])
        self.assertIn("timed out", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_navigate_playwright_error(self, msp):
        c = _wire(msp)
        c.page.goto.side_effect = PlaywrightError("net::ERR_NAME_NOT_RESOLVED")
        s = browser.BrowserSession()
        s.launch()
        r = s.navigate("https://nope.invalid/")
        self.assertFalse(r["ok"])
        self.assertIn("browser error (navigate)", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_snapshot_playwright_error(self, msp):
        c = _wire(msp)
        c.page.content.side_effect = PlaywrightError("page closed")
        s = browser.BrowserSession()
        s.launch()
        r = s.snapshot_dom()
        self.assertFalse(r["ok"])
        self.assertIn("browser error (snapshot_dom)", r["error"])

    @mock.patch.object(browser, "sync_playwright")
    def test_close_never_raises_on_teardown_error(self, msp):
        c = _wire(msp)
        c.browser.close.side_effect = PlaywrightError("already closed")
        s = browser.BrowserSession()
        s.launch()
        r = s.close()  # must not raise
        self.assertFalse(r["ok"])
        self.assertIn("close error", r["error"])


class TestBrowserSessionCaptureReset(unittest.TestCase):
    @mock.patch.object(browser, "sync_playwright")
    def test_capture_console_resets_per_call(self, msp):
        c = _wire(msp)
        s = browser.BrowserSession()
        s.launch()
        s.navigate("https://example.test/")
        c.fire_console("log", "first")
        c.fire_console("error", "second")
        r = s.capture_console()
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["entries"],
                         [{"type": "log", "text": "first"}, {"type": "error", "text": "second"}])
        # reset confirmed: a fresh event, then capture returns only the new one
        c.fire_console("warning", "third")
        r2 = s.capture_console()
        self.assertEqual(r2["result"]["entries"], [{"type": "warning", "text": "third"}])
        # and a capture with no new events returns empty
        self.assertEqual(s.capture_console()["result"]["entries"], [])

    @mock.patch.object(browser, "sync_playwright")
    def test_capture_network_resets_per_call(self, msp):
        c = _wire(msp)
        s = browser.BrowserSession()
        s.launch()
        s.navigate("https://example.test/")
        c.fire_response("https://example.test/a.js", 200)
        c.fire_response("https://example.test/missing", 404)
        r = s.capture_network()
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["entries"],
                         [{"url": "https://example.test/a.js", "status": 200},
                          {"url": "https://example.test/missing", "status": 404}])
        c.fire_response("https://example.test/b.css", 200)
        r2 = s.capture_network()
        self.assertEqual(r2["result"]["entries"], [{"url": "https://example.test/b.css", "status": 200}])
        self.assertEqual(s.capture_network()["result"]["entries"], [])

    @mock.patch.object(browser, "sync_playwright")
    def test_handlers_registered_once_not_per_navigate(self, msp):
        c = _wire(msp)
        s = browser.BrowserSession()
        s.launch()
        s.navigate("https://example.test/1")
        s.navigate("https://example.test/2")
        # page.on registered exactly twice total (console + response), at launch —
        # NOT re-registered per navigate (which would double-count events).
        self.assertEqual(c.page.on.call_count, 2)


class TestBrowserSessionNoTypeLeak(unittest.TestCase):
    """DC4 — wrapper-is-the-seam: no Playwright object crosses the boundary."""

    @mock.patch.object(browser, "sync_playwright")
    def test_all_returns_are_primitives(self, msp):
        c = _wire(msp, content="<p>x</p>", goto_status=201)
        s = browser.BrowserSession()
        results = [s.launch()]
        results.append(s.navigate("https://example.test/"))
        c.fire_console("log", "hi")
        c.fire_response("https://example.test/x", 200)
        results.append(s.snapshot_dom())
        results.append(s.capture_console())
        results.append(s.capture_network())
        results.append(s.close())
        for r in results:
            self.assertTrue(_all_primitives(r), f"non-primitive leaked: {r}")


class TestBrowserSessionNoActuation(unittest.TestCase):
    """F6 — observation-only: no actuation methods exist on the wrapper."""

    def test_no_actuation_methods(self):
        for name in ("click", "type", "fill", "press", "check", "uncheck",
                     "select_option", "hover", "tap", "drag_and_drop",
                     "set_input_files", "focus", "dispatch_event"):
            self.assertFalse(
                hasattr(browser.BrowserSession, name),
                f"actuation method {name!r} must not exist (F6 observation-only)",
            )

    def test_public_surface_is_observation_only(self):
        public = {n for n in dir(browser.BrowserSession) if not n.startswith("_")}
        self.assertEqual(
            public,
            {"launch", "navigate", "snapshot_dom", "capture_console",
             "capture_network", "close"},
        )


class TestBrowserSessionContextManager(unittest.TestCase):
    @mock.patch.object(browser, "sync_playwright")
    def test_context_manager_launches_and_closes(self, msp):
        c = _wire(msp)
        with browser.BrowserSession() as s:
            r = s.navigate("https://example.test/")
            self.assertTrue(r["ok"])
        # __exit__ tore the session down
        c.browser.close.assert_called_once()
        c.pw.stop.assert_called_once()

    @mock.patch.object(browser, "sync_playwright")
    def test_close_idempotent(self, msp):
        _wire(msp)
        s = browser.BrowserSession()
        s.launch()
        self.assertTrue(s.close()["ok"])
        # a second close is a no-op that still returns ok (never raises)
        self.assertTrue(s.close()["ok"])


if __name__ == "__main__":
    unittest.main()
