"""Screen-aware browser-extension capture substrate — v4 Phase 3a Step 2 (brief
Step 2; Phase 3 design items A + DC3; Step 0 Q-A0b/Q-A3 resolutions).

A never-raises wrapper around a Chrome extension that captures the active tab
(``chrome.tabCapture`` — Q-A3) and, in a later phase, actuates it
(``chrome.scripting``). The browser half of the two-capture-layer substrate; the
native half is screen_capture.py. The orchestrator (3c) dispatches by target URI
scheme (``tab://`` → here; ``screen://`` → screen_capture). Ships
available-but-not-consumed (the observe-loop is Phase 3c).

Two parallel wrappers, not one (DC3): this layer shares no substrate with
screen_capture.py — only the never-raises ``_ok``/``_err`` contract pattern is
mirrored. The orchestrator picks the wrapper by scheme.

IPC = Native Messaging (Q-A3): commands cross to the extension as length-prefixed
JSON over a native-messaging host (chosen over a local HTTP server / WebSocket —
no port, local-only, the standard Chrome↔native channel). ANVIL-initiated capture
uses a long-lived ``connectNative`` port (the extension opens it at startup; ANVIL
pushes capture commands). The host-manifest registration + the 4-byte stdio
framing + the connectNative lifecycle are the Phase 3c live-ratification surface;
3a is mock-first (every test patches ``_send``).

Contract (the connector-wrapper contract; browser.py / screen_capture.py are the
precedents): every public method NEVER raises — ``{"ok": True, "result": …}`` on
success, ``{"ok": False, "error": "<reason>"}`` on any failure. The caller
inspects ``["ok"]``.

Wrapper-is-the-seam (DC4): no extension / IPC object crosses the boundary —
``capture_tab`` returns ``{"frame_png": bytes, "width": int, "height": int}``
(symmetric with screen_capture.snapshot_frame, so both feed the same
visibility-session ``frame.png`` blob). The extension-side Chrome API choice
(``chrome.tabCapture`` stream-frame vs ``chrome.tabs.captureVisibleTab``
single-shot) is extension-side JS, not this wrapper's concern — a 3c decision
(lean: captureVisibleTab for a single frame).

Actuation gating (Q-A0b / F6): ``run_script`` (``chrome.scripting``) is PRESENT in
the surface but is a REFUSING STUB in Phase 3a — it returns a structured
"actuation not enabled" error until the per-session opt-in state exists (Phase
3b/3c). Unlike the native layer (screen_capture.py ships NO actuation method at
all — CGEvent deferred entirely), the browser layer keeps the actuation method as
a gated stub because chrome.scripting is the extension's wired actuation path that
3c switches on behind the opt-in. No actuation is PERFORMED in 3a.

No new event kinds here (the ``screen.captured`` kind is Step 3, events.py).
"""
from __future__ import annotations

import logging

_log = logging.getLogger("anvil.integrations.screen_browser")

# Native-messaging round-trip wall-clock cap (the live framing/transport is
# 3c-ratified; mirrors browser.py's _DEFAULT_TIMEOUT_MS / screen_capture's cap).
_DEFAULT_TIMEOUT_S = 30.0

_ACTUATION_DISABLED = (
    "actuation not enabled: chrome.scripting actuation is gated behind the "
    "per-session co-pilot opt-in (default-off); grant it with `anvil copilot "
    "start --autonomous` or the Telegram opt-in token"
)


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    _log.warning("[screen_browser] %s", reason)
    return {"ok": False, "error": reason}


class BrowserExtensionSession:
    """Never-raises browser-extension capture session. Lifecycle:
    ``connect_extension`` → ``capture_tab`` (+ ``run_script``, a gated stub) →
    ``disconnect`` (or use as a context manager). No extension/IPC type crosses
    the boundary (DC4)."""

    def __init__(self, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._connected = False

    # --- lifecycle -----------------------------------------------------------
    def connect_extension(self) -> dict:
        """Open the Native Messaging channel to the extension (the connectNative
        long-lived port). Structured error (never raises) if the host is
        unreachable. The live handshake is 3c-ratified; mock-first in 3a."""
        if self._connected:
            return _err("already connected")
        try:
            resp = self._send({"cmd": "ping"})
            if not resp.get("ok"):
                return _err(
                    f"extension unreachable: {resp.get('error', 'no ping response')}"
                )
            self._connected = True
            return _ok({"connected": True})
        except Exception as exc:  # noqa: BLE001 — never-raise floor
            return self._unexpected("connect_extension", exc)

    def capture_tab(self) -> dict:
        """Capture the active tab as a single PNG frame (observation). Returns
        ``{"frame_png": bytes, "width": int, "height": int}`` — symmetric with
        screen_capture.snapshot_frame. Never raises."""
        if not self._connected:
            return _err("not connected")
        try:
            resp = self._send({"cmd": "capture_tab"})
            if not resp.get("ok"):
                return _err(f"capture_tab failed: {resp.get('error', 'unknown')}")
            png, width, height = self._decode_frame(resp.get("result") or {})
            if not png:
                return _err("capture_tab: extension returned no image")
            return _ok({"frame_png": png, "width": width, "height": height})
        except Exception as exc:  # noqa: BLE001
            return self._unexpected("capture_tab", exc)

    def run_script(self, script: str, *, session=None) -> dict:
        """chrome.scripting actuation — v4 Phase 3c promotes the Phase 3a refusing
        stub to a LIVE, opt-in-gated path (Q-C0b / DC8). The gate is the security
        boundary: actuation is refused unless `session` is a live co-pilot session
        with an explicit autonomous opt-in (`copilot.is_autonomous_enabled`) —
        default-off, so the absence of a session (mid-build, or an un-opted-in
        co-pilot) refuses. When the opt-in is in force, the chrome.scripting send
        is attempted (live `tab://` actuation rides the extension transport, which
        is itself the deferred BAF-4 surface). Never raises."""
        if not self._connected:
            return _err("not connected")
        from anvil import copilot  # lazy: keep integrations import-light
        if session is None or not copilot.is_autonomous_enabled(session):
            return _err(_ACTUATION_DISABLED)
        try:
            resp = self._send({"cmd": "run_script", "script": script})
            if not resp.get("ok"):
                return _err(f"run_script failed: {resp.get('error', 'unknown')}")
            return _ok({"executed": True, "result": resp.get("result")})
        except Exception as exc:  # noqa: BLE001 — never-raise floor
            return self._unexpected("run_script", exc)

    def disconnect(self) -> dict:
        """Close the channel. Idempotent; never raises (the browser.py ``close`` /
        screen_capture ``stop_capture`` precedent)."""
        self._connected = False
        return _ok({"disconnected": True})

    # --- context manager -----------------------------------------------------
    def __enter__(self) -> "BrowserExtensionSession":
        self.connect_extension()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.disconnect()
        return False  # never suppress

    # --- internals (the 3c live-ratification surface; mocked in 3a) ----------
    def _send(self, message: dict) -> dict:
        """One Native Messaging round-trip: frame ``message`` as a 4-byte
        little-endian length prefix + JSON body, write to the host's stdin, read
        the framed response, parse JSON. Returns the parsed response dict
        (expected ``{"ok": bool, "result"/"error": …}``). The live framing +
        transport + the connectNative lifecycle are 3c-ratified; mocked in 3a so
        the suite is hermetic (no extension, no host process)."""
        raise NotImplementedError(
            "Native Messaging transport is wired at Phase 3c live-ratification; "
            "Phase 3a is mock-first (tests patch _send)."
        )

    def _decode_frame(self, result: dict):
        """Decode the extension's capture response into ``(png_bytes, width,
        height)`` — primitives only (DC4). The extension base64-encodes the PNG
        for JSON transport; the base64 alphabet + the width/height keys are
        3c-ratified."""
        import base64

        b64 = result.get("frame_png_b64")
        if not b64:
            return b"", 0, 0
        return (
            base64.b64decode(b64),
            int(result.get("width", 0)),
            int(result.get("height", 0)),
        )

    def _unexpected(self, op: str, exc: Exception) -> dict:
        _log.warning(
            "[screen_browser] unexpected error in %s: %s", op, exc, exc_info=True
        )
        return _err(f"screen_browser unexpected error ({op}): {type(exc).__name__}")
