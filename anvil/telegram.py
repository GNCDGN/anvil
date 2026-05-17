"""Telegram send + long-poll (implementation-notes Component 6).

Option A (orchestrator-approved 2026-05-17): python-telegram-bot's low-level
`telegram.Bot`, sync-wrapped via `asyncio.run()` to satisfy Component 6's
synchronous contract — `send() -> int`, `wait_for_reply(timeout) -> Reply|None`,
`send_typing() -> None`. PTB 22.7 verified: `Bot.get_updates` and
`Bot.send_message` are present, async coroutines, not deprecated-to-removal,
so the poll uses `bot.get_updates` (no raw-requests fallback taken).

No lock file. The Component 1 mechanism (time-bounded `[ANVIL]`-prefix
deferral) replaced it; the Veronica-side deferral is its own one-step build.
ANVIL just sends `[ANVIL]`-prefixed messages (the prefix is the caller's /
voice.py's responsibility, matching the brief's smoke which passes it
explicitly) and long-polls for replies.

The PTB Application/run_polling event-loop pattern Veronica's bot_listener
uses is deliberately NOT used — it's the wrong shape for ANVIL's imperative
"send one message, then block for one reply" model. Veronica's *sender*
(telegram_sender.py) is raw urllib; we use PTB Bot per the committed 2X
decision while keeping the synchronous contract.

The async seams `_send_message` / `_poll_updates` / `_send_typing` are the
only network surface; tests patch them — no real network in unit tests.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from telegram import Bot

from anvil.state import state_dir  # marker file lives in the gitignored state/

log = logging.getLogger("anvil.telegram")


@dataclass
class Reply:
    text: str
    message_id: int
    timestamp: int  # unix epoch seconds


@dataclass
class _Upd:
    update_id: int
    chat_id: int | None
    message_id: int | None
    text: str | None
    date: int | None


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        chat_id: str | int,
        *,
        long_poll_seconds: int = 30,
        max_send_retries: int = 3,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.long_poll_seconds = long_poll_seconds
        self.max_send_retries = max_send_retries
        self._last_update_id: int | None = None

    # ---- async seams (mocked in unit tests; the only network surface) -----

    def _send_message(self, text: str) -> int:
        """One send attempt. Returns the Telegram message_id. May raise."""
        async def _do() -> int:
            async with Bot(self.bot_token) as bot:
                msg = await bot.send_message(chat_id=self.chat_id, text=text)
                return msg.message_id
        return asyncio.run(_do())

    def _poll_updates(self, offset: int | None, timeout: int) -> list[_Upd]:
        """One getUpdates call (server-side long poll up to `timeout`s).
        Returns normalised _Upd records. May raise."""
        async def _do() -> list[_Upd]:
            async with Bot(self.bot_token) as bot:
                updates = await bot.get_updates(offset=offset, timeout=timeout)
                out: list[_Upd] = []
                for u in updates:
                    m = u.message
                    if m is None:
                        out.append(_Upd(u.update_id, None, None, None, None))
                        continue
                    out.append(
                        _Upd(
                            update_id=u.update_id,
                            chat_id=m.chat.id,
                            message_id=m.message_id,
                            text=(m.text or ""),
                            date=int(m.date.timestamp()) if m.date else None,
                        )
                    )
                return out
        return asyncio.run(_do())

    def _send_typing(self) -> None:
        async def _do() -> None:
            async with Bot(self.bot_token) as bot:
                await bot.send_chat_action(chat_id=self.chat_id, action="typing")
        asyncio.run(_do())

    # ---- public API (Component 6 contract) --------------------------------

    def send(self, text: str) -> int:
        """Send a message. Returns the Telegram message_id, or -1 after
        `max_send_retries` failed attempts (3-retry exponential backoff),
        in which case a `telegram-down.marker` is written for Genco to spot.
        Never raises."""
        for attempt in range(self.max_send_retries):
            try:
                mid = self._send_message(text)
                log.info(f"sent message {mid} ({len(text)} chars)")
                return mid
            except Exception as e:  # noqa: BLE001 — never-raise contract
                log.warning(
                    f"telegram send attempt {attempt + 1}/"
                    f"{self.max_send_retries} failed: {e}"
                )
                if attempt < self.max_send_retries - 1:
                    time.sleep(2 ** attempt)
        marker = state_dir() / "telegram-down.marker"
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                f"{datetime.now(timezone.utc).isoformat()}: telegram send "
                f"failed after {self.max_send_retries} attempts\n"
                f"Message:\n{text}\n",
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.error(f"could not even write telegram-down.marker: {e}")
        log.error("telegram send failed after all retries; wrote marker")
        return -1

    def send_typing(self) -> None:
        """Best-effort 'typing' indicator for long Planner calls. Never
        raises; failure is non-fatal."""
        try:
            self._send_typing()
        except Exception as e:  # noqa: BLE001
            log.warning(f"send_typing failed (non-fatal): {e}")

    def wait_for_reply(self, timeout: int | None) -> Reply | None:
        """Long-poll until a text reply arrives in `chat_id`. Returns the
        Reply, or None if `timeout` (seconds) elapses first. `timeout=None`
        waits indefinitely. Tracks last_update_id so backlog and already-seen
        updates are never reprocessed. Never raises."""
        start = time.time()

        # Baseline: a non-blocking getUpdates so we ignore any backlog and
        # only react to replies that arrive after this call begins.
        if self._last_update_id is None:
            try:
                base = self._poll_updates(None, 0)
                self._last_update_id = max(
                    (u.update_id for u in base), default=0
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"baseline getUpdates failed: {e}; assuming 0")
                self._last_update_id = 0

        while True:
            try:
                updates = self._poll_updates(
                    self._last_update_id + 1, self.long_poll_seconds
                )
            except Exception as e:  # noqa: BLE001
                log.warning(f"getUpdates poll failed: {e}; backing off")
                updates = []
                time.sleep(2)

            for u in updates:
                self._last_update_id = max(self._last_update_id, u.update_id)
                if (
                    u.chat_id is not None
                    and str(u.chat_id) == self.chat_id
                    and u.text
                ):
                    log.info(f"reply received (update {u.update_id})")
                    return Reply(
                        text=u.text,
                        message_id=u.message_id or 0,
                        timestamp=u.date or 0,
                    )

            if timeout is not None and (time.time() - start) > timeout:
                log.info(f"wait_for_reply timed out after {timeout}s")
                return None
