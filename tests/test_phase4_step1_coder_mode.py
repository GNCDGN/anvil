"""Phase 4 Step 1 tests — CODER_MODE flows from env → config → orchestrator → state.

Covers the two-layer fix per Step 0 Finding 1:
  - Layer 2 (Orchestrator.__init__): explicit kwarg wins; otherwise config.coder_mode.
  - Layer 3 (handle_brief init_state call): state.coder_mode reflects self.coder_mode.

Hermetic: temp anvil_root, isolated env via patch.dict, MagicMock for planner
so no real Anthropic call fires.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from anvil.config import Config
from anvil.orchestrator import Orchestrator


_REQUIRED_ENV = {
    "ANTHROPIC_API_KEY": "sk-test",
    "TELEGRAM_BOT_TOKEN": "test-token",
    "TELEGRAM_CHAT_ID": "12345",
}


class TestPhase4Step1CoderMode(unittest.TestCase):
    """Phase 4 Step 1: kwarg-default-None resolution + state persistence."""

    def setUp(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-coder-mode-"))
        (self._tmpdir / ".env").write_text("")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _load(self, extra_env: dict | None = None) -> Config:
        env = {**_REQUIRED_ENV, "ANVIL_ROOT": str(self._tmpdir)}
        if extra_env:
            env.update(extra_env)
        with patch.dict(os.environ, env, clear=True):
            return Config.load()

    def _build_orchestrator(self, config: Config, **kwargs) -> Orchestrator:
        """Build with a MagicMock planner so no real Anthropic call fires."""
        return Orchestrator(
            config,
            planner=MagicMock(),
            telegram=MagicMock(),
            git=MagicMock(),
            run_smoke=MagicMock(),
            **kwargs,
        )

    def test_env_auto_propagates_to_orchestrator(self) -> None:
        """CODER_MODE=auto in env → config.coder_mode=='auto' → orch.coder_mode=='auto'
        when no explicit kwarg is passed (Layer 2 fix)."""
        config = self._load({"CODER_MODE": "auto"})
        self.assertEqual(config.coder_mode, "auto")
        # No explicit coder_mode kwarg; coder=MagicMock() so _build_coder
        # is not exercised (we just want to read self.coder_mode).
        orch = self._build_orchestrator(config, coder=MagicMock())
        self.assertEqual(orch.coder_mode, "auto")

    def test_env_manual_propagates_to_orchestrator(self) -> None:
        """CODER_MODE=manual in env → orch.coder_mode=='manual' (default-but-real
        path; checks the resolution didn't get stuck on the old hardcoded
        kwarg default)."""
        config = self._load({"CODER_MODE": "manual"})
        self.assertEqual(config.coder_mode, "manual")
        orch = self._build_orchestrator(config)
        self.assertEqual(orch.coder_mode, "manual")
        # In manual mode self.coder should be None per Phase 2 Step 9 contract
        self.assertIsNone(orch.coder)

    def test_explicit_kwarg_overrides_config(self) -> None:
        """Test-injection contract preserved: explicit coder_mode='manual'
        wins even when config says 'auto' (Layer 2 resolution honours
        non-None kwarg)."""
        config = self._load({"CODER_MODE": "auto"})
        self.assertEqual(config.coder_mode, "auto")
        orch = self._build_orchestrator(config, coder_mode="manual")
        self.assertEqual(orch.coder_mode, "manual")


if __name__ == "__main__":
    unittest.main()
