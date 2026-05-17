"""Voice loading + outgoing-message formatting (implementation-notes
Component 11).

Voice consistency is binding (design Part 2). `load_voice_spec` has a real
three-tier fallback so an outgoing message never silently loses its
formatting constraints:

  1. Veronica's live spec:
     <vault_root>/01-Projects/second-brain/veronica/capabilities/reporting/prompts/_voice.md
  2. The frozen snapshot: <anvil_root>/prompts/_voice-snapshot.md
  3. A minimal hardcoded fallback (terse, direct, no preamble, no emoji)

Order is vault → snapshot → minimal, never inverted.

The Phase 0 step/escalation/completion messages are fixed-shape templates
that already satisfy the spec by construction (`[ANVIL]` prefix, terse, no
emoji, no preamble). The spec text is loaded at orchestrator startup and
kept available for the Phase 1 Planner, which generates free text that the
spec actually governs.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("anvil.voice")

_VAULT_REL = (
    "01-Projects/second-brain/veronica/capabilities/reporting/prompts/_voice.md"
)
_SNAPSHOT = Path(__file__).resolve().parent.parent / "prompts" / "_voice-snapshot.md"
_MINIMAL = (
    "Voice (minimal fallback): terse, direct, no preamble, no emoji, no "
    "exclamation marks, no service phrasing, no sign-offs. Plain English; "
    "say what changed and what's needed, nothing else."
)


def load_voice_spec(vault_root: Path) -> str:
    """Vault → snapshot → minimal. Never raises; never returns empty."""
    vault_path = Path(vault_root) / _VAULT_REL
    try:
        text = vault_path.read_text(encoding="utf-8").strip()
        if text:
            return text
        log.warning(f"voice spec at {vault_path} is empty; trying snapshot")
    except Exception as e:  # noqa: BLE001
        log.warning(f"voice spec vault read failed ({e}); trying snapshot")
    try:
        text = _SNAPSHOT.read_text(encoding="utf-8").strip()
        if text:
            return text
        log.warning(f"voice snapshot {_SNAPSHOT} is empty; using minimal")
    except Exception as e:  # noqa: BLE001
        log.warning(f"voice snapshot read failed ({e}); using minimal")
    log.error("voice spec unavailable from vault and snapshot; minimal fallback")
    return _MINIMAL


def _short(text: str, n: int = 120) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def format_step_completion(state, plan, commit_hash, smoke_result) -> str:
    """design Part 2 step-completion shape. Every line populated."""
    smoke = "pass" if smoke_result is True or smoke_result == "pass" else (
        f"FAIL: {_short(str(smoke_result), 200)}"
    )
    files = ", ".join(plan.files_to_touch) if plan.files_to_touch else "(none)"
    return (
        f"[ANVIL] Step {plan.step_number} complete — {plan.step_name}\n"
        f"- What: {_short(plan.approach)}\n"
        f"- Files: {files}\n"
        f"- Smoke: {smoke}\n"
        f"- Commit: {commit_hash or '(none — no-op step)'}\n"
        f"\n"
        f"Reply 'go' to continue, or anything else to pause."
    )


def format_escalation(state, reason, detail, options) -> str:
    """design Part 2 escalation shape."""
    if isinstance(options, (list, tuple)):
        opts = " / ".join(str(o) for o in options) if options else "your call"
    else:
        opts = str(options) if options else "your call"
    return (
        f"[ANVIL] Step {state.current_step} — escalation\n"
        f"- Why: {_short(reason)}\n"
        f"- Detail: {_short(detail, 400)}\n"
        f"- Options: {opts}\n"
        f"\n"
        f"Reply with your decision, or 'pause' to think."
    )


def format_completion(brief, state) -> str:
    done = sum(1 for s in state.steps if s.status == "done")
    return (
        f"[ANVIL] Build complete — {brief.build_name}\n"
        f"- Steps: {done}/{len(state.steps)} done\n"
        f"- Status: {state.status}\n"
        f"- Run log: {Path(state.run_log).name if state.run_log else '(none)'}"
    )
