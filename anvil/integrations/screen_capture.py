"""Screen-aware native capture substrate — v4 Phase 3a Step 1 (brief Step 1;
Phase 3 design items A + DC3/DC4; Step 0 Q-A1/Q-A2/Q-A4 resolutions).

A never-raises wrapper around macOS ScreenCaptureKit (single-frame pixel capture)
+ AXUIElement (the accessibility tree) that OBSERVES screen state for the
screen-aware observe-loop. The load-bearing Phase 3 native primitive; the
observe-loop that consumes it lands in Phase 3c (this module ships
available-but-not-consumed, like browser.py before Phase 2c).

Two parallel wrappers, not one (DC3): this is the NATIVE layer; the browser layer
is screen_browser.py. The orchestrator (3c) dispatches by target URI scheme
(``screen://`` → here; ``tab://`` → screen_browser). The two share no substrate —
only the never-raises ``_ok``/``_err`` contract pattern is mirrored.

Accessibility-first / vision-fallback (Q-A4, substrate-level): ``query_accessibility``
is the PRIMARY method (cheap, structured — the AXUIElement tree); ``snapshot_frame``
is the FALLBACK (a pixel frame for a vision model, used only where the
accessibility tree is empty/unhelpful — canvas / Figma / custom-Electron). Vision
is opted-in by CALLING snapshot_frame(), never the silent default.

Contract (the connector-wrapper contract; browser.py / visibility_session.py are
the precedents): every public method NEVER raises — ``{"ok": True, "result": …}``
on success, ``{"ok": False, "error": "<reason>"}`` on any failure. The caller
inspects ``["ok"]``; no try/except.

Wrapper-is-the-seam (DC4): the only platform imports are the three pyobjc
frameworks, GUARDED so the module imports even when they are absent (non-macOS /
not installed) — mock-first: the tests patch this boundary. No ScreenCaptureKit /
AXUIElement / CGImage object is ever returned — every method returns dicts of
primitives (str/int/bytes/list/dict/None/bool). A future capture-layer swap
reimplements these methods and nothing else changes.

Observation-only (Q-A0b / F6): there are NO actuation methods (no click/type/
move/key). On-screen actuation (CGEvent / AX actuation) is gated behind the
per-session opt-in (Phase 3b/3c); Phase 3a ships observation substrate only — the
absence of actuation methods IS the no-actuation enforcement (the browser.py /
sentry.py precedent).

Dependency (Amendment 2 / Q-A2): pyobjc — pyobjc-framework-ScreenCaptureKit +
pyobjc-framework-ApplicationServices (pinned exact in requirements.txt), the
SECOND new v4 third-party dependency (after Playwright's F3). Quartz / Cocoa /
CoreMedia come transitively. The screen-recording permission preflight lives in
Quartz (``CGPreflightScreenCaptureAccess``); the accessibility-trust check in
ApplicationServices (``AXIsProcessTrusted``) — a two-framework permission split
(Step 0 Q-A2; the brief assumed one).

Live-ratification deferred to Phase 3c (Q-A2 / the brief's mock-first posture):
the live ScreenCaptureKit capture (incl. the SCScreenshotManager async
completion-handler → sync bridge + its timeout), the live AX tree walk, the
CGImage→PNG encode, and the TCC permission grants are ratified at first live
capture (3c) — the Phase 1c CLI-absence / Phase 2a Playwright-install precedent.
Phase 3a is mock-first: every test patches the pyobjc boundary; the suite is green
whether or not the frameworks import.

No new event kinds here (the ``screen.captured`` kind is Step 3, events.py).
"""
from __future__ import annotations

import logging
import threading

# Guarded platform import (the mock-first boundary; DC4 seam). The module must
# import on any OS / with the frameworks absent — tests patch _SCK / _AS / _CG.
try:
    import ScreenCaptureKit as _SCK  # type: ignore
    import ApplicationServices as _AS  # type: ignore
    import Quartz as _CG  # type: ignore  # CoreGraphics — the screen-permission home

    _IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # ImportError on non-macOS / frameworks absent
    _SCK = _AS = _CG = None  # type: ignore
    _IMPORT_ERROR = _exc

_log = logging.getLogger("anvil.integrations.screen_capture")

# Single-frame capture wall-clock cap (the async completion-handler bridge below;
# the live timeout is 3c-ratified, mirroring browser.py's _DEFAULT_TIMEOUT_MS).
_DEFAULT_TIMEOUT_S = 30.0


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    _log.warning("[screen_capture] %s", reason)
    return {"ok": False, "error": reason}


class ScreenCaptureSession:
    """Never-raises native screen-capture session. Lifecycle:
    ``start_capture`` → ``query_accessibility`` / ``snapshot_frame`` →
    ``stop_capture`` (or use as a context manager). Accessibility-first; vision
    (``snapshot_frame``) is the fallback. No platform type crosses the boundary.
    """

    def __init__(self, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        self._started = False

    # --- lifecycle -----------------------------------------------------------
    def start_capture(self) -> dict:
        """Preflight the screen-recording permission and arm the session. Returns
        a structured error (never raises) if the pyobjc frameworks are absent or
        the TCC grant is missing."""
        if self._started:
            return _err("already started")
        if _IMPORT_ERROR is not None or _SCK is None:
            return _err(
                "screen capture not available: pyobjc frameworks not importable "
                f"({type(_IMPORT_ERROR).__name__ if _IMPORT_ERROR else 'absent'})"
            )
        try:
            if not _CG.CGPreflightScreenCaptureAccess():
                return _err(
                    "screen recording permission required: grant ANVIL in System "
                    "Settings > Privacy & Security > Screen Recording"
                )
            self._started = True
            return _ok({"started": True})
        except Exception as exc:  # noqa: BLE001 — never-raise floor
            return self._unexpected("start_capture", exc)

    def query_accessibility(self, query: str | None = None) -> dict:
        """Accessibility-first PRIMARY method: walk the AXUIElement tree and
        return a structured element list — ``{"elements": [{"role", "label",
        "frame"}, …], "query": <query>}``. ``query`` (optional) filters by a
        role/label substring; ``None`` returns the top-level elements. Checks the
        Accessibility TCC grant first; never raises."""
        if not self._started:
            return _err("not started")
        try:
            if not _AS.AXIsProcessTrusted():
                return _err(
                    "accessibility permission required: grant ANVIL in System "
                    "Settings > Privacy & Security > Accessibility"
                )
            elements = self._walk_accessibility(query)
            return _ok({"elements": elements, "query": query})
        except Exception as exc:  # noqa: BLE001
            return self._unexpected("query_accessibility", exc)

    def snapshot_frame(self) -> dict:
        """Vision FALLBACK: a single PNG frame of the main display. Returns
        ``{"frame_png": bytes, "width": int, "height": int}``. Called by the
        observe-loop only where the accessibility tree is unavailable (Q-A4) —
        never the silent default. Never raises."""
        if not self._started:
            return _err("not started")
        try:
            png, width, height = self._capture_single_frame()
            if not png:
                return _err("snapshot_frame: capture returned no image")
            return _ok({"frame_png": png, "width": width, "height": height})
        except Exception as exc:  # noqa: BLE001
            return self._unexpected("snapshot_frame", exc)

    def stop_capture(self) -> dict:
        """Tear down the session. Idempotent; never raises (the browser.py
        ``close`` precedent — safe even if start failed)."""
        self._started = False
        return _ok({"stopped": True})

    # --- context manager -----------------------------------------------------
    def __enter__(self) -> "ScreenCaptureSession":
        self.start_capture()
        return self

    def __exit__(self, *exc_info) -> bool:
        self.stop_capture()
        return False  # never suppress

    # --- internals (the 3c live-ratification surface; mocked in 3a) ----------
    def _capture_single_frame(self):
        """SCScreenshotManager single-frame capture of the main display.

        ScreenCaptureKit's screenshot API is ASYNC (a completion handler), so this
        bridges async→sync via a ``threading.Event`` (Step 0 Q-A2 surprise: the
        brief assumed a clean sync call like browser.py's ``page.content()``). The
        live bridge + timeout + the SCShareableContent display enumeration are
        3c-ratified; in 3a this is reached only through mocked tests.

        Returns ``(png_bytes | None, width, height)`` — primitives only, no
        SC/CGImage type crosses back to the caller (DC4)."""
        # Enumerate shareable displays (async → sync bridge).
        content = self._await_async(
            _SCK.SCShareableContent.getShareableContentWithCompletionHandler_
        )
        displays = list(content.displays())
        if not displays:
            return None, 0, 0
        display = displays[0]  # main display (single-display first-pass)
        filt = _SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            display, []
        )
        config = _SCK.SCStreamConfiguration.alloc().init()
        config.setWidth_(display.width())
        config.setHeight_(display.height())
        image = self._await_async(
            lambda handler: _SCK.SCScreenshotManager
            .captureImageWithFilter_configuration_completionHandler_(
                filt, config, handler
            )
        )
        png = _cgimage_to_png(image)
        return png, int(display.width()), int(display.height())

    def _await_async(self, call):
        """Run a completion-handler-style ObjC call to completion synchronously.
        `call` is invoked with a single-arg handler ``handler(result)`` or a
        bound method that takes the handler; returns the result the handler
        received. 3c-ratified (the runloop/queue servicing is the live wrinkle)."""
        done = threading.Event()
        box: dict = {}

        def _handler(result, *rest):
            box["result"] = result
            box["error"] = rest[0] if rest else None
            done.set()

        call(_handler)
        if not done.wait(self._timeout_s):
            raise TimeoutError(f"screen capture timed out after {self._timeout_s}s")
        if box.get("error") is not None:
            raise RuntimeError(f"ScreenCaptureKit error: {box['error']}")
        return box.get("result")

    def _walk_accessibility(self, query: str | None) -> list[dict]:
        """Walk the system-wide AXUIElement tree one level deep, returning
        ``[{"role": str, "label": str, "frame": {x,y,w,h} | None}, …]`` — all
        primitives (DC4). ``AXUIElementCopyAttributeValue`` uses pyobjc's
        ``(error, value)`` out-param convention. The traversal depth + the
        AXValue geometry unpack are 3c-ratified; mocked in 3a."""
        system = _AS.AXUIElementCreateSystemWide()
        err, children = _AS.AXUIElementCopyAttributeValue(
            system, _AS.kAXChildrenAttribute, None
        )
        out: list[dict] = []
        for el in list(children or []):
            role = _ax_str(el, _AS.kAXRoleAttribute)
            label = _ax_str(el, _AS.kAXTitleAttribute) or _ax_str(
                el, _AS.kAXDescriptionAttribute
            )
            if query and query.lower() not in f"{role} {label}".lower():
                continue
            out.append({"role": role, "label": label, "frame": _ax_frame(el)})
        return out

    def _unexpected(self, op: str, exc: Exception) -> dict:
        _log.warning(
            "[screen_capture] unexpected error in %s: %s", op, exc, exc_info=True
        )
        return _err(f"screen_capture unexpected error ({op}): {type(exc).__name__}")


# --- module-level AX helpers (primitives out; 3c-ratified) -------------------
def _ax_str(element, attribute) -> str:
    """Copy a string AX attribute; '' on absence/error (never raises here — the
    caller's ladder is the floor, but AX reads should degrade, not crash)."""
    try:
        err, value = _AS.AXUIElementCopyAttributeValue(element, attribute, None)
        return str(value) if value is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _ax_frame(element):
    """Copy AX position + size into a primitive ``{x, y, w, h}`` dict, or None.
    The AXValue→CGPoint/CGSize unpack is 3c-ratified."""
    try:
        _, pos = _AS.AXUIElementCopyAttributeValue(
            element, _AS.kAXPositionAttribute, None
        )
        _, size = _AS.AXUIElementCopyAttributeValue(
            element, _AS.kAXSizeAttribute, None
        )
        if pos is None or size is None:
            return None
        ok_p, point = _AS.AXValueGetValue(pos, _AS.kAXValueCGPointType, None)
        ok_s, dims = _AS.AXValueGetValue(size, _AS.kAXValueCGSizeType, None)
        if not (ok_p and ok_s):
            return None
        return {
            "x": float(point.x),
            "y": float(point.y),
            "w": float(dims.width),
            "h": float(dims.height),
        }
    except Exception:  # noqa: BLE001
        return None


def _cgimage_to_png(image) -> bytes:
    """Encode a CGImageRef to PNG bytes via a CGImageDestination (Quartz). Returns
    primitives only (no CG type crosses back). 3c-ratified (the encode path + the
    UTType identifier are validated against a real frame at first live capture)."""
    if image is None:
        return b""
    data = _CG.CFDataCreateMutable(None, 0)
    dest = _CG.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    if dest is None:
        return b""
    _CG.CGImageDestinationAddImage(dest, image, None)
    _CG.CGImageDestinationFinalize(dest)
    return bytes(data)
