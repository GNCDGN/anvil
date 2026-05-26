"""Browser-aware substrate — v4 Phase 2a Step 1 (brief Step 1; Phase 2 design
items A + DC3/DC4/DC8; Step 0 Q-D1/Q-D4/Q-D5 ratifications).

A never-raises wrapper around Playwright's **sync** API (DC3) that drives a
headless Chromium (DC8) to OBSERVE a page's runtime behaviour — DOM, console,
network — and nothing else. It is the load-bearing Phase 2 primitive; the
observe-loop that consumes it lands in Phase 2c (this module ships
available-but-not-consumed, like the Phase 1 connectors).

Contract (the connector-wrapper contract, github_issues.py / sentry.py): every
public method **never raises** — a structured ``{"ok": True, "result": …}`` on
success, ``{"ok": False, "error": "<reason>"}`` on any failure. The caller
inspects ``["ok"]``; no try/except. The error ladder mirrors
``github_issues._run_gh`` / ``sentry._get`` but is adapted to Playwright's
exception types (Step 0 Q-D1): ``playwright.sync_api.TimeoutError`` (a subclass
of ``Error``, so it is caught FIRST), then ``playwright.sync_api.Error`` (the
base — a missing browser bundle surfaces here as an ``Error`` whose message
names the missing executable, not a distinct class), then a final broad
``except`` for the never-raise floor.

Wrapper-is-the-seam (DC4): the only Playwright import is ``sync_playwright`` (+
the two exception types for the ladder). No ``Page`` / ``Browser`` /
``BrowserContext`` / ``Locator`` object is ever returned — every method returns
dicts of primitives (str/int/list/dict/None/bool). A future CDP-direct or
alternative-library swap reimplements these methods and nothing else changes.

Observation-only (F6): there are NO actuation methods (no click/type/fill). The
absence of actuation methods IS the no-actuation enforcement (the sentry.py
"absence of write methods is the write-scope enforcement" precedent). ``navigate``
opens the observation target (the F6-sanctioned navigate-to-observe path); it is
not in-page actuation.

No new event kinds (DC7): the wrapper logs via the ``anvil.integrations`` logger
but emits no operations-table event; ``VALID_KINDS`` stays 51. An ``observe.*``
kind lands in Phase 2c when the observe-loop consumes this wrapper during a build
(with a ``run_id`` context) — the connector-pattern.md Contract 5 condition.

Lifecycle (Step 0 Q-D1): ``sync_playwright().start()`` → ``chromium.launch`` →
``new_page`` → register console/response handlers ONCE here → ``navigate`` /
``snapshot_dom`` / ``capture_*`` → ``browser.close()`` → ``pw.stop()`` (teardown
is on the started Playwright object, not the context manager). ``capture_console``
/ ``capture_network`` reset their accumulator per call (option b): they return
the window and clear it for the next observation window.
"""
from __future__ import annotations

import logging

from playwright.sync_api import (
    Error as _PlaywrightError,
    TimeoutError as _PlaywrightTimeout,
    sync_playwright,
)

_log = logging.getLogger("anvil.integrations.browser")

# Wall-clock cap per browser op, matching the order of the gh/sentry timeouts
# (Playwright takes milliseconds, so 30s = 30000ms).
_DEFAULT_TIMEOUT_MS = 30_000

# A missing bundled browser surfaces as a `playwright.sync_api.Error` whose
# message names the absent executable (Step 0 Q-D1: not a distinct exception
# class — detected by message content).
_MISSING_BROWSER_HINTS = ("Executable doesn't exist", "playwright install")


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    _log.warning("[browser] %s", reason)
    return {"ok": False, "error": reason}


class BrowserSession:
    """A never-raises, observation-only sync Playwright session.

    Use either explicitly (``s = BrowserSession(); s.launch(); … s.close()``) to
    inspect each step's structured result, or as a context manager
    (``with BrowserSession() as s: …``) for guaranteed teardown. Both never
    raise: a failed ``launch`` inside ``__enter__`` is discarded (the body's
    first ``navigate`` returns a structured "not launched" error), and
    ``__exit__`` always tears the session down.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None
        # Private accumulators — never cross the wrapper boundary (DC4). The
        # handlers append here; capture_* returns a snapshot and clears (option b).
        self._console_entries: list[dict] = []
        self._network_entries: list[dict] = []

    # --- event handlers (registered once at page creation) --------------------

    def _on_console(self, msg) -> None:
        """Accumulate a console entry as a plain dict (no Playwright object kept).
        Defensive: a handler must never raise into Playwright's event loop."""
        try:
            self._console_entries.append({"type": msg.type, "text": msg.text})
        except Exception:  # noqa: BLE001 — a handler must not raise
            pass

    def _on_response(self, response) -> None:
        """Accumulate a network entry as a plain dict (url + status; Step 0 Q-D1
        ratified fields). Defensive: never raise into the event loop."""
        try:
            self._network_entries.append(
                {"url": response.url, "status": response.status}
            )
        except Exception:  # noqa: BLE001 — a handler must not raise
            pass

    # --- lifecycle ------------------------------------------------------------

    def launch(self, *, headless: bool = True, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> dict:
        """Start Playwright, launch headless Chromium, open a page, and register
        the console/response handlers ONCE (so they accumulate across navigations
        without double-registration). Returns ``_ok({"headless": …})`` or a
        structured error. Refuses a double-launch."""
        if self._pw is not None:
            return _err("browser already launched")
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=headless, timeout=timeout_ms)
            self._page = self._browser.new_page()
            # Register handlers here (not per-navigate) so a repeated navigate()
            # does not re-add them (which would double-count events). Step 1
            # refinement over the brief sketch's "register in navigate".
            self._page.on("console", self._on_console)
            self._page.on("response", self._on_response)
            return _ok({"headless": headless})
        except _PlaywrightTimeout:
            self._teardown_quiet()
            return _err(f"browser operation timed out: launch (>{timeout_ms}ms)")
        except _PlaywrightError as exc:
            self._teardown_quiet()
            msg = str(exc)
            if any(hint in msg for hint in _MISSING_BROWSER_HINTS):
                return _err("browser not installed: run `playwright install chromium`")
            return _err(f"browser error (launch): {msg.splitlines()[0][:200]}")
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            self._teardown_quiet()
            _log.warning("[browser] unexpected error (launch): %s", exc, exc_info=True)
            return _err(f"browser unexpected error (launch): {type(exc).__name__}")

    def navigate(self, url: str, *, timeout_ms: int = _DEFAULT_TIMEOUT_MS) -> dict:
        """Navigate the page to ``url`` to observe it (navigate-to-observe, F6).
        Returns ``_ok({"url": …, "status": int|None})`` or a structured error."""
        if self._page is None:
            return _err("browser not launched")
        try:
            resp = self._page.goto(url, timeout=timeout_ms)
            status = resp.status if resp is not None else None
            return _ok({"url": url, "status": status})
        except _PlaywrightTimeout:
            return _err(f"browser operation timed out: navigate {url!r} (>{timeout_ms}ms)")
        except _PlaywrightError as exc:
            return _err(f"browser error (navigate): {str(exc).splitlines()[0][:200]}")
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            _log.warning("[browser] unexpected error (navigate %s): %s", url, exc, exc_info=True)
            return _err(f"browser unexpected error (navigate): {type(exc).__name__}")

    # --- observation ----------------------------------------------------------

    def snapshot_dom(self) -> dict:
        """Return the live DOM as ``_ok({"html": str})`` or a structured error."""
        if self._page is None:
            return _err("browser not launched")
        try:
            html = self._page.content()
            return _ok({"html": html})
        except _PlaywrightTimeout:
            return _err("browser operation timed out: snapshot_dom")
        except _PlaywrightError as exc:
            return _err(f"browser error (snapshot_dom): {str(exc).splitlines()[0][:200]}")
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            _log.warning("[browser] unexpected error (snapshot_dom): %s", exc, exc_info=True)
            return _err(f"browser unexpected error (snapshot_dom): {type(exc).__name__}")

    def capture_console(self) -> dict:
        """Return the console entries accumulated since the last call and reset
        the accumulator (per-capture-call reset, option b — Step 0 Q-D1). Returns
        ``_ok({"entries": [{"type": str, "text": str}, …]})``."""
        if self._page is None:
            return _err("browser not launched")
        entries = list(self._console_entries)
        # clear() mutates the same list object the handler appends to, so the
        # handler's reference stays valid for the next window.
        self._console_entries.clear()
        return _ok({"entries": entries})

    def capture_network(self) -> dict:
        """Return the network entries accumulated since the last call and reset
        the accumulator (option b). Returns
        ``_ok({"entries": [{"url": str, "status": int}, …]})``."""
        if self._page is None:
            return _err("browser not launched")
        entries = list(self._network_entries)
        self._network_entries.clear()
        return _ok({"entries": entries})

    # --- teardown -------------------------------------------------------------

    def close(self) -> dict:
        """Close the browser and stop Playwright. Idempotent and never-raises —
        a double close (or a close before launch) is a no-op that returns
        ``_ok``. A teardown error is logged and returned as a structured error,
        but the session is nulled out regardless."""
        err = None
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            err = type(exc).__name__
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            err = err or type(exc).__name__
        self._browser = None
        self._pw = None
        self._page = None
        if err:
            _log.warning("[browser] close encountered %s (session torn down anyway)", err)
            return _err(f"browser close error: {err}")
        return _ok({"closed": True})

    def _teardown_quiet(self) -> None:
        """Best-effort release of partially-acquired resources after a failed
        launch. Swallows everything (the launch error is what the caller sees)."""
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._pw = None
        self._page = None

    # --- context manager ------------------------------------------------------

    def __enter__(self) -> "BrowserSession":
        # launch() never raises; a failed launch leaves _page None, so the body's
        # first navigate/observe returns a structured "not launched" error.
        self.launch()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
        return None
