"""ANVIL configuration loading and validation.

Loads from the repo-root `.env` via python-dotenv. Required-secret keys must
be present and non-empty; everything else has a sensible default sourced from
implementation-notes "Environment". `anvil_lock_file` is deliberately absent —
the lock-file mechanism was removed in the Component 1 patch (time-bounded
`[ANVIL]`-prefix deferral, `anvil_defer_window_seconds`, replaces it).

`ConfigError` is imported from `anvil.errors` (folded into the AnvilError
hierarchy in Step 3 per orchestrator instruction; it was defined locally in
Step 2 as a flagged follow-up).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from anvil.errors import ConfigError

__all__ = ["Config", "ConfigError"]


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
    claude_binary: str | None = None
    coder_mode: str = "manual"
    # Phase 3 Step 2: VPS deployment config; required when running briefs with vps_deploy: yes
    vps_host: str | None = None
    vps_user: str = "root"
    # v5 Phase 1c: the mode-guard reverse channel. When `mode_guard` is on, the
    # orchestrator SSH-writes running_builds (active/complete) on the VPS at
    # build start/end (Q-C1 — ssh_ops.ssh_run, NOT Telegram). Off by default so
    # the calibration sweeps + local runs don't touch the VPS ledger; the
    # production Mac sets ANVIL_MODE_GUARD=1. The mark CLI runs from
    # vps_monitor_path with vps_ops_db as ANVIL_OPS_DB_PATH.
    mode_guard: bool = False
    vps_monitor_path: str = "/home/vault-reporter/anvil-monitor"
    vps_ops_db: str = "/home/vault-reporter/anvil-ops.db"
    # v5 Phase 2a: TTS output (item B). Per-session opt-in — OFF by default so
    # the build is silent unless the operator declares TTS for the session.
    # ANVIL_TTS=1 enables it; ANVIL_TTS_BACKEND picks say (default) | elevenlabs.
    tts_enabled: bool = False
    tts_backend: str = "say"
    # Phase 4 Step 1: checkpoint write target; derived from vault_path at load time
    checkpoint_active_path: Path = Path("/dev/null")  # replaced in load()
    # v2 Phase 1 Step 5: calibration-framework flags. Selected by env
    # (MOCKED_PLANNER=1, MOCKED_CODER=1) — see anvil/mocked.py. Default
    # off, production behaviour unchanged. The Orchestrator's
    # _build_planner / _build_coder seams switch implementations on
    # these flags at construction time.
    mocked_planner: bool = False
    mocked_coder: bool = False

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
        claude_binary = os.environ.get("CLAUDE_BINARY", "").strip() or None
        coder_mode = os.environ.get("CODER_MODE", "manual").strip() or "manual"
        if coder_mode not in ("manual", "auto"):
            problems.append(
                f"CODER_MODE (must be 'manual' or 'auto', got {coder_mode!r})"
            )
            coder_mode = "manual"
        planner_timeout = int_env("PLANNER_TIMEOUT_SECONDS", 120)
        anvil_defer_window_seconds = int_env("ANVIL_DEFER_WINDOW_SECONDS", 300)
        # Phase 3 Step 2: VPS deployment config (optional at load-time; runtime gate in
        # orchestrator step 7 escalates deploy-config-missing when vps_deploy: yes but
        # vps_host is None).
        vps_host = os.environ.get("VPS_HOST", "").strip() or None
        vps_user = os.environ.get("VPS_USER", "").strip() or "root"
        # v5 Phase 1c mode-guard (opt-in reverse channel).
        mode_guard = (os.environ.get("ANVIL_MODE_GUARD", "0").strip() == "1")
        vps_monitor_path = (
            os.environ.get("ANVIL_VPS_MONITOR_PATH", "").strip()
            or "/home/vault-reporter/anvil-monitor"
        )
        vps_ops_db = (
            os.environ.get("ANVIL_VPS_OPS_DB", "").strip()
            or "/home/vault-reporter/anvil-ops.db"
        )
        # v5 Phase 2a TTS output (per-session opt-in).
        tts_enabled = (os.environ.get("ANVIL_TTS", "0").strip() == "1")
        tts_backend = os.environ.get("ANVIL_TTS_BACKEND", "").strip() or "say"

        # v2 Phase 1 Step 5: calibration-framework env flags. "1" enables
        # the mocked subclass; anything else (default empty) keeps the
        # production class. Mirrors the existing coder_mode read pattern.
        mocked_planner = (os.environ.get("MOCKED_PLANNER", "0").strip() == "1")
        mocked_coder = (os.environ.get("MOCKED_CODER", "0").strip() == "1")

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
            claude_binary=claude_binary,
            coder_mode=coder_mode,
            vps_host=vps_host,
            vps_user=vps_user,
            mode_guard=mode_guard,
            vps_monitor_path=vps_monitor_path,
            vps_ops_db=vps_ops_db,
            tts_enabled=tts_enabled,
            tts_backend=tts_backend,
            checkpoint_active_path=(
                vault_path / "01-Projects/second-brain/checkpoints/active"
            ),
            mocked_planner=mocked_planner,
            mocked_coder=mocked_coder,
        )
