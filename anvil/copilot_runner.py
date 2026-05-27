"""v4 Phase 3c Step 2 — the co-pilot session runner (DC7).

The SECOND orchestrator path: a bounded capture-interpret-guide loop over the
screen-aware substrate, separate from handle_brief (the mid-build path, Step 1).
Each capture grabs the declared screen surfaces, routes a Sonnet VISION digest
(the seam's vision consumer), persists via the visibility_session co-pilot
keyspace, emits screen.captured (mode=co-pilot), and guides the operator over
Telegram. Actuation is opt-in-gated (copilot.is_autonomous_enabled) and
default-off; this loop is observe-and-guide — it does not actuate.

Decoupled + capture-only: the substrate wrappers are never-raises, the seam
returns a structured error string (not an exception), and a per-capture failure
logs and continues (the Phase 2c observe-loop capture-only posture). The
session_id is reconciled to ONE id across copilot (the opt-in state) and
visibility_session (the co-pilot keyspace).
"""
from __future__ import annotations

import logging

from anvil import copilot
from anvil import events as _events
from anvil import routing
from anvil.brief import _observe_scheme
from anvil.integrations import screen_browser, screen_capture, visibility_session
from anvil.orchestrator import VISION_SYSTEM_PROMPT, _build_screen_summary

_log = logging.getLogger("anvil.copilot_runner")

_DEFAULT_SURFACES = ("frame", "accessibility")


def _open_substrate(scheme: str):
    """Return ``(session, open_fn, close_fn, frame_fn, accessibility_fn|None)``
    for the scheme — screen:// native (ScreenCaptureSession) or tab:// extension
    (BrowserExtensionSession). Mirrors the orchestrator _observe_screen_subphase
    dispatch; the extension surface has no accessibility tree (native-only)."""
    if scheme == "tab":
        s = screen_browser.BrowserExtensionSession()
        return s, s.connect_extension, s.disconnect, s.capture_tab, None
    s = screen_capture.ScreenCaptureSession()
    return (s, s.start_capture, s.stop_capture, s.snapshot_frame,
            s.query_accessibility)


def _capture_once(scheme: str, surfaces: list[str]) -> dict:
    """One capture window: open the substrate, grab the declared surfaces, close
    in finally. Returns ``{"frame": …|None, "accessibility": …|None}``. Never
    raises (the substrate wrappers are never-raises)."""
    sess, _open, _close, _frame, _ax = _open_substrate(scheme)
    observations: dict = {"frame": None, "accessibility": None}
    try:
        opened = _open()
        if not opened.get("ok"):
            _log.warning("[copilot] substrate (%s) open failed: %s",
                         scheme, opened.get("error"))
            return observations
        if "frame" in surfaces:
            res = _frame()
            if res.get("ok"):
                observations["frame"] = res.get("result")
            else:
                _log.warning("[copilot] frame capture failed: %s",
                             res.get("error"))
        if "accessibility" in surfaces and _ax is not None:
            res = _ax()
            if res.get("ok"):
                observations["accessibility"] = res.get("result")
            else:
                _log.warning("[copilot] accessibility capture failed: %s",
                             res.get("error"))
    finally:
        try:
            _close()
        except Exception:  # noqa: BLE001 — defensive; never-raises
            pass
    return observations


def _digest(observations: dict):
    """Route the capture to a Sonnet VISION digest. Returns
    ``(digest|None, vision_used)``. A frame digests WITH the image (vision); an
    accessibility-only capture digests text-only. A seam error / empty response
    yields digest=None (the observe-loop capture-only posture)."""
    frame = observations.get("frame")
    if frame is not None and frame.get("frame_png"):
        raw = routing.call_model_for_subtask(
            "sonnet", VISION_SYSTEM_PROMPT, _build_screen_summary(observations),
            image=frame["frame_png"],
        )
        ok = (not raw.startswith("[call_model_for_subtask error:")
              and bool(raw.strip()))
        return (raw if ok else None), True
    if observations.get("accessibility") is not None:
        raw = routing.call_model_for_subtask(
            "sonnet", VISION_SYSTEM_PROMPT, _build_screen_summary(observations),
        )
        ok = (not raw.startswith("[call_model_for_subtask error:")
              and bool(raw.strip()))
        return (raw if ok else None), False
    return None, False


def run(target: str, *, autonomous: bool = False, max_captures: int = 5,
        surfaces=None, telegram=None, poll_grant: bool = True) -> dict:
    """Run a bounded co-pilot session against `target`.

    Mints ONE session id reconciled across copilot (the opt-in state) and
    visibility_session (the co-pilot keyspace). Each of up to `max_captures`
    iterations: capture → Sonnet vision digest → write_copilot_capture → emit
    screen.captured (mode=co-pilot) → guide over Telegram. Between captures, if
    `telegram` is supplied and `poll_grant`, a reply matching the reserved opt-in
    token grants autonomous actuation mid-session (copilot.apply_telegram_grant).
    end_session on exit (the opt-in expires). Returns a summary dict."""
    surfaces = list(surfaces or _DEFAULT_SURFACES)
    scheme = _observe_scheme(target)

    # Reconcile to ONE session id — the visibility co-pilot keyspace id, passed
    # to copilot.start_session so the opt-in state and the persisted captures
    # share one identity (Q-C3).
    started = visibility_session.start_copilot_session(target)
    session_id = (
        (started.get("result") or {}).get("session_id")
        if started.get("ok") else None
    )
    if not session_id:
        session_id = "cp-fallback"
        _log.warning("[copilot] visibility session start failed (%s); using "
                     "fallback id", started.get("error"))
    session = copilot.start_session(
        target, session_id=session_id, autonomous=autonomous
    )

    captures = 0
    frames = 0
    granted = bool(autonomous)
    try:
        for capture_idx in range(max_captures):
            observations = _capture_once(scheme, surfaces)
            digest, vision_used = _digest(observations)
            written = visibility_session.write_copilot_capture(
                session_id, capture_idx, target, observations, digest=digest,
            )
            record_path = (
                (written.get("result") or {}).get("path", "")
                if written.get("ok") else ""
            )
            ax = observations.get("accessibility") or {}
            ax_count = len(ax.get("elements", [])) if isinstance(ax, dict) else 0
            frame_blob = observations.get("frame")
            if frame_blob is not None:
                frames += 1
            _events.emit(
                "screen.captured",
                {
                    "mode": "co-pilot",
                    "session_id": session_id,
                    "capture_idx": capture_idx,
                    "target": target,
                    "surfaces": surfaces,
                    "record_path": record_path,
                    "accessibility_element_count": ax_count,
                    "vision_used": vision_used,
                    "frame_count": 1 if frame_blob is not None else 0,
                    "digest_chars": len(digest) if digest else 0,
                    "ok": bool(written.get("ok")),
                },
            )
            captures += 1
            if telegram is not None:
                telegram.send(
                    f"[co-pilot {capture_idx}] {digest or '(no digest)'}"
                )
                if poll_grant:
                    reply = telegram.wait_for_reply(0)
                    rtext = getattr(reply, "text", None) if reply else None
                    if copilot.apply_telegram_grant(session, rtext):
                        granted = True
                        telegram.send("[co-pilot] autonomous actuation enabled")
    finally:
        copilot.end_session(session)
    return {
        "session_id": session_id,
        "target": target,
        "scheme": scheme,
        "captures": captures,
        "frames": frames,
        "autonomous_granted": granted,
    }
