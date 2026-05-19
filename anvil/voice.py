"""Voice loading + outgoing-message formatting (design Part 8 / impl-notes
Component 11).

Voice consistency is binding. `load_voice_spec` resolves the spec with two
tiers and no third invented tier:

  1. Canonical: $VAULT_PATH/01-Projects/second-brain/veronica/capabilities/
     reporting/prompts/_voice.md, read at runtime
  2. Snapshot: anvil/prompts/_voice-snapshot.md, committed in this repo

If the canonical is reachable it wins. If not, the snapshot is used. If
neither is available the function returns an empty string and logs an
error. It does not raise, and it does not substitute a hardcoded spec —
an empty voice spec is the design's accepted failure mode (the Planner
runs with no voice constraints rather than with a fabricated one).

VAULT_PATH from the environment is the source of truth. (Step 6 migrated
the Phase 0 orchestrator call site to this zero-arg form and removed the
backward-compat vault_root shim — decision #1 closed.)

Drift: when both canonical and snapshot are readable, a warning is logged
if the canonical's mtime is more than 30 days newer than the snapshot's.
No auto-update; the warning is the signal.

The Phase 0 step/escalation/completion formatters below are fixed-shape
templates that satisfy the spec by construction. The spec text is loaded
at orchestrator startup and consumed by the Phase 1 Planner, which
generates free text the spec actually governs.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("anvil.voice")

_VAULT_REL = (
    "01-Projects/second-brain/veronica/capabilities/reporting/prompts/_voice.md"
)
_SNAPSHOT = Path(__file__).resolve().parent / "prompts" / "_voice-snapshot.md"
_DRIFT_SECONDS = 30 * 24 * 60 * 60


def load_voice_spec() -> str:
    """Canonical (VAULT_PATH) → snapshot → empty string. Never raises.
    VAULT_PATH from the environment is the source of truth."""
    vault_path = os.environ.get("VAULT_PATH", "").strip()
    canonical = None
    canonical_text = None
    if vault_path:
        canonical = Path(vault_path) / _VAULT_REL
        if canonical.is_file():
            try:
                canonical_text = canonical.read_text(encoding="utf-8")
            except OSError as e:
                log.warning(
                    f"[voice] canonical _voice.md unreadable: {e}; "
                    f"falling back to snapshot"
                )
                canonical_text = None

    if canonical_text is not None:
        if _SNAPSHOT.is_file():
            try:
                if (
                    canonical.stat().st_mtime - _SNAPSHOT.stat().st_mtime
                    > _DRIFT_SECONDS
                ):
                    log.warning(
                        f"[voice] canonical _voice.md is more than 30 days "
                        f"newer than snapshot ({_SNAPSHOT}); snapshot is "
                        f"likely stale"
                    )
            except OSError:
                pass
        return canonical_text

    if _SNAPSHOT.is_file():
        log.info("[voice] using snapshot _voice.md")
        try:
            return _SNAPSHOT.read_text(encoding="utf-8")
        except OSError as e:
            log.error(
                f"[voice] snapshot _voice.md unreadable: {e}; voice spec empty"
            )
            return ""

    log.error(
        "[voice] neither canonical nor snapshot _voice.md found; "
        "voice spec empty"
    )
    return ""


def _short(text: str, n: int = 120) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def _prefix() -> str:
    """Return the Telegram message prefix.

    Default is `"[ANVIL]"`. The v2 Phase 1 Step 6 calibration_runner
    sets `CALIBRATION_TELEGRAM_PREFIX=[ANVIL-calibration]` for the
    duration of the sweep so calibration messages are distinguishable
    in Genco's Telegram scrollback. Default empty → "[ANVIL]" unchanged
    (per notes.md Finding 8, the prefix does not interact with Veronica's
    defer logic, so the choice is purely a scrollback discriminator).
    """
    override = os.environ.get("CALIBRATION_TELEGRAM_PREFIX", "").strip()
    return override or "[ANVIL]"


def format_step_completion(state, plan, commit_hash, smoke_result) -> str:
    """design Part 2 step-completion shape. Every line populated."""
    smoke = "pass" if smoke_result is True or smoke_result == "pass" else (
        f"FAIL: {_short(str(smoke_result), 200)}"
    )
    files = ", ".join(plan.files_to_touch) if plan.files_to_touch else "(none)"
    return (
        f"{_prefix()} Step {plan.step_number} complete — {plan.step_name}\n"
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
        f"{_prefix()} Step {state.current_step} — escalation\n"
        f"- Why: {_short(reason)}\n"
        f"- Detail: {_short(detail, 400)}\n"
        f"- Options: {opts}\n"
        f"\n"
        f"Reply with your decision, or 'pause' to think."
    )


def format_completion(brief, state) -> str:
    done = sum(1 for s in state.steps if s.status == "done")
    msg = (
        f"{_prefix()} Build complete — {brief.build_name}\n"
        f"- Steps: {done}/{len(state.steps)} done\n"
        f"- Status: {state.status}\n"
        f"- Run log: {Path(state.run_log).name if state.run_log else '(none)'}"
    )
    # Phase 3 Step 6: deploy verification block when state.deploy populated
    deploy = getattr(state, "deploy", None)
    if deploy:
        sha = deploy.get("vps_head_sha") or ""
        sha_short = sha[:7] if sha else "-"
        status = deploy.get("service_status") or "-"
        stage = deploy.get("stage", "?")
        ok = deploy.get("ok", False)
        msg += (
            f"\n\nDeploy:\n"
            f"- Stage: {stage} ({'ok' if ok else 'failed'})\n"
            f"- VPS HEAD: {sha_short}\n"
            f"- Service: {brief.service_name or '-'} ({status})"
        )
    # Phase 4 Step 6: Vault writes block when state.vault_writes_outcome
    # is populated. Success → list both basenames. Block is omitted
    # entirely when None (skip path, abort path, or build did not
    # reach step 9 — keeps the completion message tight in the common
    # case where step 9 ran but writes were deferred).
    vwo = getattr(state, "vault_writes_outcome", None)
    if vwo:
        from pathlib import Path as _P
        sl = _P(vwo.get("setup_log_path", "")).name or "-"
        cp = _P(vwo.get("checkpoint_path", "")).name or "-"
        ok_w = vwo.get("ok", False)
        if ok_w:
            msg += (
                f"\n\nVault writes:\n"
                f"- Setup-log: {sl}\n"
                f"- Checkpoint: {cp}"
            )
        else:
            err = vwo.get("error") or "(unspecified)"
            msg += (
                f"\n\nVault writes: deferred to manual\n"
                f"- Error: {_short(err, 200)}"
            )
    return msg


def format_artefact_preview(draft, setup_log_path, checkpoint_path) -> str:
    """Phase 4 Step 5: voice-bound preview for the artefact-confirmation
    gate. Wraps anvil.checkpoint.render_preview_message — the layout
    lives there; this function exists in voice.py for the [ANVIL]
    prefix and any future voice-spec touches.
    """
    from anvil.checkpoint import render_preview_message
    return render_preview_message(draft, setup_log_path, checkpoint_path)
