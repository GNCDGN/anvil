"""Phase 3 Step 2 tests — Config gains vps_host and vps_user.

Hermetic: writes a temp .env file and a temp anvil_root, no real env mutation
beyond the in-test monkey-patching. Uses unittest.mock.patch.dict on os.environ
to isolate from the caller's actual env (which may have VPS_HOST set from
Step 0 — pre-build setup).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anvil.config import Config


# Minimum env vars Config.load requires (without these, ConfigError fires
# before VPS-side fields are even reached).
_REQUIRED_ENV = {
    "ANTHROPIC_API_KEY": "sk-test",
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_CHAT_ID": "12345",
}


class TestPhase3VpsConfig(unittest.TestCase):
    """Phase 3 Step 2: Config.vps_host and Config.vps_user load from env."""

    def setUp(self) -> None:
        # Use a temp directory as anvil_root so we don't pick up the repo's
        # real .env file. ANVIL_ROOT is read by Config.load to locate .env.
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-config-"))
        # Create an empty .env so Config.load doesn't try to load a real one
        (self._tmpdir / ".env").write_text("")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _load(self, extra_env: dict | None = None) -> Config:
        """Load Config with isolated env. `extra_env` adds/overrides keys."""
        env = {**_REQUIRED_ENV, "ANVIL_ROOT": str(self._tmpdir)}
        if extra_env:
            env.update(extra_env)
        # Use clear=True to start from an empty env (only what we pass)
        with patch.dict(os.environ, env, clear=True):
            return Config.load()

    def test_vps_fields_unset_defaults(self) -> None:
        """No VPS_HOST or VPS_USER in env -> vps_host=None, vps_user='root'."""
        config = self._load()
        self.assertIsNone(config.vps_host)
        self.assertEqual(config.vps_user, "root")

    def test_vps_host_loads_from_env(self) -> None:
        """VPS_HOST=1.2.3.4 -> Config.vps_host == '1.2.3.4'."""
        config = self._load({"VPS_HOST": "1.2.3.4"})
        self.assertEqual(config.vps_host, "1.2.3.4")
        self.assertEqual(config.vps_user, "root")  # default holds

    def test_vps_user_override(self) -> None:
        """VPS_USER=admin -> Config.vps_user == 'admin'."""
        config = self._load({"VPS_HOST": "1.2.3.4", "VPS_USER": "admin"})
        self.assertEqual(config.vps_host, "1.2.3.4")
        self.assertEqual(config.vps_user, "admin")

    def test_existing_config_fields_still_load(self) -> None:
        """Regression: existing fields (claude_binary, coder_mode, etc.) still
        load correctly alongside the new VPS fields."""
        config = self._load({
            "VPS_HOST": "1.2.3.4",
            "CODER_MODE": "auto",
            "CLAUDE_BINARY": "/usr/local/bin/claude",
            "CODER_TIMEOUT_SECONDS": "900",
        })
        self.assertEqual(config.vps_host, "1.2.3.4")
        self.assertEqual(config.coder_mode, "auto")
        self.assertEqual(config.claude_binary, "/usr/local/bin/claude")
        self.assertEqual(config.coder_timeout, 900)

    def test_empty_string_vps_host_is_none(self) -> None:
        """Empty VPS_HOST (e.g. unset placeholder in .env) -> None."""
        config = self._load({"VPS_HOST": ""})
        self.assertIsNone(config.vps_host)

    def test_whitespace_vps_host_is_none(self) -> None:
        """Whitespace-only VPS_HOST -> None (strip applied)."""
        config = self._load({"VPS_HOST": "   "})
        self.assertIsNone(config.vps_host)


if __name__ == "__main__":
    unittest.main()
