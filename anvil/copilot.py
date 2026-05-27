"""Co-pilot session opt-in state — v4 Phase 3b Step 2 (brief item E; Phase 3
design DC8; Step 0 Q-B5/Q-B6).

The per-session **autonomous-actuation opt-in** for the screen-aware co-pilot
mode. Desktop/in-page actuation has a wider blast radius than anything ANVIL does
today (v4 §7), so it is gated behind an explicit, per-session opt-in that is
**stricter than the per-step `confirm:` binary**: default-OFF, granted per session
(via the CLI `--autonomous` flag at start OR the Telegram reserved token
mid-session), and **expires at session end** (no carry-over to the next session).

This module holds the opt-in STATE + the grant/guard/expiry logic only. It ships
**available-but-not-consumed** in Phase 3b: the substrate's actuation methods
(`screen_browser.run_script`, the native path — refusing stubs from Phase 3a) are
wired to `is_autonomous_enabled` in Phase 3c; the CLI parsing + the Telegram
polling that call `enable_autonomous`, and the co-pilot observe-loop that reads
the session, are also Phase 3c. It is NOT an external-surface integration (so it
lives at `anvil/` top-level, the `routing.py` precedent, not `integrations/`) and
it is NOT persisted (unlike `visibility_session` — in-memory, discarded at session
end), so it does not need the never-raises Contract 1 ladder.

Refuses-at-wrapper-when-off: `is_autonomous_enabled` returns False on a default
(un-opted-in) or ended session, so the Phase 3c wrapper refuses actuation unless
an explicit, live opt-in is in force.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

_log = logging.getLogger("anvil.copilot")

# The reserved Telegram reply token that grants autonomous actuation mid-session
# (DC8 / Q-B6). A distinct, namespaced phrase — no collision with the go/resume/
# abort reply vocabulary (design Part 4); Phase 3c recognizes it within a co-pilot
# session thread.
AUTONOMOUS_OPT_IN_TOKEN = "autonomous: on"


@dataclass
class CopilotSession:
    """In-memory state for one co-pilot session. The opt-in defaults OFF and is
    per-session — it expires at ``end_session`` with no carry-over to the next
    session. Not persisted (discarded when the session ends)."""

    session_id: str
    target: str
    autonomous_actuation_enabled: bool = False
    started_at: str = ""
    ended: bool = False


def start_session(
    target: str,
    *,
    session_id: str | None = None,
    autonomous: bool = False,
) -> CopilotSession:
    """Mint a fresh co-pilot session for `target`. The opt-in defaults OFF; pass
    ``autonomous=True`` for the CLI ``--autonomous`` grant-at-start path (DC8). A
    fresh session has no carry-over from any prior session. `session_id` may be
    supplied so Phase 3c can align it with the ``visibility_session`` co-pilot
    keyspace id; otherwise a ``cp-<uuid>`` id is minted."""
    sid = session_id or ("cp-" + uuid.uuid4().hex[:16])
    return CopilotSession(
        session_id=sid,
        target=target,
        autonomous_actuation_enabled=bool(autonomous),
        started_at=datetime.now(timezone.utc).isoformat(),
    )


def enable_autonomous(session: CopilotSession) -> None:
    """Grant autonomous actuation mid-session (the Telegram ``AUTONOMOUS_OPT_IN_
    TOKEN`` path, DC8). Idempotent. A no-op on an ended session — the opt-in
    cannot be granted after the session has expired."""
    if session.ended:
        _log.warning(
            "[copilot] enable_autonomous on ended session %s — ignored",
            session.session_id,
        )
        return
    session.autonomous_actuation_enabled = True


def is_autonomous_enabled(session: CopilotSession) -> bool:
    """The guard the screen substrate's actuation methods check in Phase 3c.
    Returns True only on a live session with an explicit opt-in — False on a
    default (un-opted-in) or ended session (refuses-at-wrapper when off)."""
    return session.autonomous_actuation_enabled and not session.ended


def end_session(session: CopilotSession) -> None:
    """End the session — the opt-in expires with no carry-over. Marks the session
    ended and clears the flag, so `is_autonomous_enabled` is False thereafter."""
    session.ended = True
    session.autonomous_actuation_enabled = False


def apply_telegram_grant(session: CopilotSession, reply_text: str | None) -> bool:
    """v4 Phase 3c Step 2: the Telegram opt-in grant path (DC8 / Q-B6). If
    `reply_text` is exactly the reserved AUTONOMOUS_OPT_IN_TOKEN (case- and
    whitespace-insensitive), grant the mid-session opt-in via enable_autonomous
    and return True; otherwise a no-op returning False. Distinct from the
    go/resume/abort reply vocabulary, so the co-pilot loop can recognize the
    grant token without colliding with the build-loop reply grammar. Never
    raises (a None / non-string reply returns False)."""
    if not reply_text:
        return False
    if reply_text.strip().lower() == AUTONOMOUS_OPT_IN_TOKEN.lower():
        enable_autonomous(session)
        return True
    return False
