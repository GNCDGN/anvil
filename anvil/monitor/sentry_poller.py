"""ANVIL monitor — the Sentry-polling trigger (v5 Phase 1c, item D).

The second trigger type. Polls Sentry on a cadence (5-min lean), reads recent
issues via the v4 ``sentry.py`` connector (``list_issues`` with ``since=``),
filters them **deterministically** (operator-configured project + severity/
``level`` threshold + a regex noise-filter — NO LLM in the gate, DC7), and
routes eligible alerts to Telegram as a notice (the operator decides whether to
investigate — explicit, the 1b precedent). Mirrors ``schedule.poll``'s
fire→log→dispatch flow; idempotent per issue (``trigger_id = sentry:<issue-id>``).
**Never-raises** (Contract 1): a bad issue row logs + continues, never aborts.

Mode-guard (Step 2): ``poll`` accepts an injected ``guard`` (the
``running_builds.mode_guard_check`` read) + ``on_stale`` (the escalation). When
a build is active the alert is deferred (``trigger_log`` disposition
``deferred-active-build``, under a distinct ``…:deferred`` id so the eventual
fire still logs cleanly) and re-attempts on the next poll once the build clears.
A *stale* active row escalates once (fail-closed, design Q8). The params default
off, so Step 1 ships + tests the poller with the mode-guard dormant.

Sentry live probe is **DEFERRED** (no Sentry account; brief Amendment 1) — this
ships **code-complete + unit-tested** (mock-first: tests mock ``sentry.list_issues``
and the Telegram send), the live ratification a named Phase 2 carry-forward.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from anvil.integrations import sentry
from anvil.monitor import anvil_ops

log = logging.getLogger("anvil.monitor.sentry_poller")
_API = "https://api.telegram.org/bot"

# Sentry severity ordering for the threshold filter (the API `level` field).
_LEVELS = {"debug": 0, "info": 1, "warning": 2, "error": 3, "fatal": 4}


def _min_level() -> int:
    return _LEVELS.get(os.environ.get("SENTRY_MIN_LEVEL", "error").lower(), 3)


def _noise_re():
    pat = os.environ.get("SENTRY_NOISE_REGEX", "")
    return re.compile(pat) if pat else None


def is_eligible(issue: dict, *, min_level: int | None = None, noise=None) -> bool:
    """The deterministic routing gate (DC7 — no model). Eligible iff the issue's
    ``level`` meets the threshold AND its title does not match the operator's
    noise regex. `issue` is a Sentry API issue dict."""
    min_level = _min_level() if min_level is None else min_level
    lvl = _LEVELS.get(str(issue.get("level", "error")).lower(), 3)
    if lvl < min_level:
        return False
    noise = _noise_re() if noise is None else noise
    title = issue.get("title") or issue.get("culprit") or ""
    if noise and noise.search(title):
        return False
    return True


def _alert_text(issue: dict) -> str:
    """The [ANVIL] Sentry notice (the wake.py [ANVIL] prefix convention)."""
    return (
        f"[ANVIL] Sentry — {issue.get('level', '?')}: "
        f"{issue.get('title', '(no title)')} (issue {issue.get('id', '?')}); "
        f"reply 'go <brief>' to investigate, 'skip' to defer"
    )


def _notify(text: str, *, token: str | None = None, chat_id: str | None = None,
            timeout: int = 10) -> dict:
    """Stdlib-urllib Telegram send (the wake.py shape; the monitor stays
    stdlib-only on the VPS). Never-raises."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("sentry _notify: missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return {"ok": False, "error": "missing telegram token/chat_id"}
    data = urlencode({"chat_id": str(chat_id), "text": text}).encode()
    try:
        with urlopen(Request(f"{_API}{token}/sendMessage", data=data), timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            return {"ok": False, "error": f"telegram: {body.get('description')}"}
        return {"ok": True, "message_id": body["result"]["message_id"]}
    except HTTPError as e:
        return {"ok": False, "error": f"telegram HTTP {e.code}"}
    except (URLError, TimeoutError, ConnectionError) as e:
        return {"ok": False, "error": f"telegram request failed: {e}"}
    except Exception as e:  # never-raises
        return {"ok": False, "error": f"sentry _notify: {type(e).__name__}: {e}"}


def poll(
    db_path: str,
    *,
    project: str | None = None,
    since=None,
    now: datetime | None = None,
    dispatch=None,
    guard=None,
    on_stale=None,
) -> dict:
    """One Sentry poll: fetch recent issues, filter deterministically, route
    eligible alerts to Telegram. Idempotent per issue. Never-raises. Returns
    ``{"ok", "routed": [...], "deferred": [...], "checked": int}``.

    `dispatch(issue)` is the route handler (default: the Telegram notice).
    `guard(db_path, now=)` is the mode-guard read (Step 2); when it reports an
    active build the alert is deferred; a stale row escalates once via
    `on_stale(text)`.
    """
    now = now or datetime.now()
    project = project or os.environ.get("SENTRY_PROJECT")
    dispatch = dispatch or (lambda issue: _notify(_alert_text(issue)))
    routed: list[str] = []
    deferred: list[str] = []
    try:
        if not project:
            return {"ok": False, "error": "SENTRY_PROJECT not set"}
        g = guard(db_path, now=now) if guard else {"active": False, "stale": False}
        since_q = since.isoformat() if hasattr(since, "isoformat") else since
        res = sentry.list_issues(project, since=since_q, scope="read")
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        issues = res.get("result") or []
        min_level, noise = _min_level(), _noise_re()
        for issue in issues:
            if not is_eligible(issue, min_level=min_level, noise=noise):
                continue
            base = f"sentry:{issue.get('id')}"
            note = _alert_text(issue)[:200]
            if g.get("stale"):
                logged = anvil_ops.log_trigger(
                    db_path, base + ":deferred-stale", source="sentry",
                    received_at=now.isoformat(), disposition="deferred-stale-build", notes=note)
                if on_stale and logged.get("inserted"):
                    on_stale(
                        f"[ANVIL] mode-guard — STALE running_build; sentry alert {base} "
                        f"deferred. Inspect/clear running_builds, then it re-fires.")
                deferred.append(base)
                continue
            if g.get("active"):
                anvil_ops.log_trigger(
                    db_path, base + ":deferred", source="sentry",
                    received_at=now.isoformat(), disposition="deferred-active-build", notes=note)
                deferred.append(base)
                continue
            logged = anvil_ops.log_trigger(
                db_path, base, source="sentry", received_at=now.isoformat(),
                disposition="firing", notes=note)
            if not logged["ok"] or not logged["inserted"]:
                continue  # already routed this issue (idempotent)
            try:
                d = dispatch(issue)
            except Exception as exc:  # never-raises: a bad dispatch logs + continues
                log.warning("sentry dispatch failed for %s: %s", base, exc)
                d = {"ok": False, "error": str(exc)}
            anvil_ops.update_trigger_disposition(
                db_path, base,
                disposition="fired" if d.get("ok") else "dispatch-failed",
                fired_at=now.isoformat())
            routed.append(base)
        return {"ok": True, "routed": routed, "deferred": deferred, "checked": len(issues)}
    except Exception as exc:  # never-raises
        log.warning("sentry poll failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "error": f"sentry poll: {type(exc).__name__}: {exc}"}
