"""ANVIL monitor — the wake send (v5 Phase 1b, item E, monitor side).

Sends an `[ANVIL] Wake` message to the ANVIL bot chat via Telegram's
sendMessage, using **stdlib `urllib`** (no python-telegram-bot — the monitor
stays stdlib-only on the VPS; Veronica's `reporter/telegram_sender.py` is the
precedent, Q-B3). Never-raises (Contract 1/5): a missing token or an HTTP
failure returns a structured error, never an exception.

**Explicit-mode only (Amendment 1 + 2):** the wake asks the operator to reply
`go <path>`; the operator relays the brief path to the Mac. The bot cannot
deliver the path itself — a bot's getUpdates never returns its own outbound
sends, so the Mac sees the operator's reply, not the wake. `confirm_mode: auto`
is deferred (no Telegram path for a VPS→Mac trigger that bypasses the operator).
"""
from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

log = logging.getLogger("anvil.monitor.wake")
_API = "https://api.telegram.org/bot"


def wake_text(task: dict) -> str:
    """The [ANVIL] Wake message (design.md Part 2 prefix convention)."""
    brief = task.get("brief_path")
    return (
        f"[ANVIL] Wake — task {task.get('task_id')}, brief {brief}; "
        f"reply 'go {brief}' to start, 'skip' to defer"
    )


def send_wake(
    task: dict,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    timeout: int = 10,
) -> dict:
    """Send the wake for a due scheduled task. Never-raises. Returns
    {"ok": True, "message_id": int} or {"ok": False, "error": str}."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("send_wake: missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return {"ok": False, "error": "missing telegram token/chat_id"}
    if (task.get("confirm_mode") or "explicit") == "auto":
        # Amendment 2: auto-mode has no Telegram VPS->Mac path in Phase 1b.
        log.warning("send_wake: confirm_mode=auto deferred (Phase 1b explicit-only); "
                    "task %s sent as an explicit wake", task.get("task_id"))
    data = urlencode({"chat_id": str(chat_id), "text": wake_text(task)}).encode()
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
        return {"ok": False, "error": f"send_wake: {type(e).__name__}: {e}"}
