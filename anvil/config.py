"""ANVIL configuration loading and validation.

Loads from the repo-root `.env` via python-dotenv. Required-secret keys must
be present and non-empty; everything else has a sensible default sourced from
implementation-notes "Environment". `anvil_lock_file` is deliberately absent —
the lock-file mechanism was removed in the Component 1 patch (time-bounded
`[ANVIL]`-prefix deferral, `anvil_defer_window_seconds`, replaces it).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed.

    Defined locally for Step 2. errors.py (Step 3, implementation-notes
    Component 12) introduces the AnvilError hierarchy; Component 12 does not
    currently list ConfigError — aligning it into that hierarchy is a flagged
    Step 3 follow-up.
    """


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    vault_path: Path
    anvil_root: Path
    anvil_defer_window_seconds: int
    planner_model: str
    planner_timeout: int
    coder_timeout: int

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Config":
        """Load and validate. Raises ConfigError listing ALL problems, not
        just the first."""
        # anvil_root: the repo root (the dir containing the `anvil` package),
        # overridable via ANVIL_ROOT.
        default_root = Path(__file__).resolve().parent.parent
        anvil_root = _expand(os.environ.get("ANVIL_ROOT", str(default_root)))

        dotenv_path = Path(env_path) if env_path else (anvil_root / ".env")
        if dotenv_path.is_file():
            load_dotenv(dotenv_path)

        problems: list[str] = []

        def required(key: str) -> str:
            val = os.environ.get(key, "").strip()
            if not val:
                problems.append(f"{key} (required, missing or empty)")
            return val

        def int_env(key: str, default: int) -> int:
            raw = os.environ.get(key, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                problems.append(f"{key} (must be an integer, got {raw!r})")
                return default

        anthropic_api_key = required("ANTHROPIC_API_KEY")
        telegram_bot_token = required("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = required("TELEGRAM_CHAT_ID")

        vault_path = _expand(os.environ.get("VAULT_PATH", "~/vaults/second-brain"))
        planner_model = (
            os.environ.get("PLANNER_MODEL", "").strip() or "claude-opus-4-7"
        )
        coder_timeout = int_env("CODER_TIMEOUT_SECONDS", 600)
        planner_timeout = int_env("PLANNER_TIMEOUT_SECONDS", 120)
        anvil_defer_window_seconds = int_env("ANVIL_DEFER_WINDOW_SECONDS", 300)

        if problems:
            raise ConfigError(
                "Invalid ANVIL configuration (looked in "
                f"{dotenv_path}):\n  - " + "\n  - ".join(problems)
            )

        return cls(
            anthropic_api_key=anthropic_api_key,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            vault_path=vault_path,
            anvil_root=anvil_root,
            anvil_defer_window_seconds=anvil_defer_window_seconds,
            planner_model=planner_model,
            planner_timeout=planner_timeout,
            coder_timeout=coder_timeout,
        )
