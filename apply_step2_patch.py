#!/usr/bin/env python3
"""Phase 3 Step 2 patch — Config gains vps_host/vps_user; .env.example documents both.

Idempotent: detects whether the patch has already been applied (by checking for
'vps_host' in config.py) and exits cleanly if so. Leaves .bak files for
modified files.

Applies edits to anvil/config.py:
  1. Add vps_host: str | None = None, vps_user: str = "root" to Config dataclass
  2. Config.load reads VPS_HOST and VPS_USER from env
  3. Pass through to the cls(...) constructor

Appends VPS_HOST and VPS_USER lines to .env.example.

Creates new tests/test_config.py with four test cases.

Run from ~/Downloads/anvil:
    .venv/bin/python apply_step2_patch.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
CONFIG_PY = REPO / "anvil" / "config.py"
ENV_EXAMPLE = REPO / ".env.example"
TEST_CONFIG_PY = REPO / "tests" / "test_config.py"


def fail(msg: str) -> None:
    print(f"[step2-patch] FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[step2-patch] {msg}")


def _backup(p: Path) -> None:
    bak = p.with_suffix(p.suffix + ".pre-phase-3-step-2.bak")
    if not bak.exists():
        shutil.copy2(p, bak)
        info(f"backed up {p.name} -> {bak.name}")


def _apply_unique(text: str, old: str, new: str, label: str) -> str:
    occurrences = text.count(old)
    if occurrences == 0:
        fail(f"{label}: anchor not found")
    if occurrences > 1:
        fail(f"{label}: anchor found {occurrences} times, expected 1")
    return text.replace(old, new, 1)


def patch_config_py() -> bool:
    text = CONFIG_PY.read_text()
    if "vps_host" in text:
        info("config.py already has vps_host — skipping")
        return False

    _backup(CONFIG_PY)

    # Edit 1: add vps_host and vps_user fields to dataclass (after coder_mode)
    old_fields = '    claude_binary: str | None = None\n    coder_mode: str = "manual"\n\n'
    new_fields = '    claude_binary: str | None = None\n    coder_mode: str = "manual"\n    # Phase 3 Step 2: VPS deployment config; required when running briefs with vps_deploy: yes\n    vps_host: str | None = None\n    vps_user: str = "root"\n\n'
    text = _apply_unique(text, old_fields, new_fields, "edit 1: dataclass fields")

    # Edit 2: load VPS_HOST and VPS_USER in Config.load, before the problems-raise gate
    old_load = "        planner_timeout = int_env(\"PLANNER_TIMEOUT_SECONDS\", 120)\n        anvil_defer_window_seconds = int_env(\"ANVIL_DEFER_WINDOW_SECONDS\", 300)\n\n        if problems:\n"
    new_load = "        planner_timeout = int_env(\"PLANNER_TIMEOUT_SECONDS\", 120)\n        anvil_defer_window_seconds = int_env(\"ANVIL_DEFER_WINDOW_SECONDS\", 300)\n        # Phase 3 Step 2: VPS deployment config (optional at load-time; runtime gate in\n        # orchestrator step 7 escalates deploy-config-missing when vps_deploy: yes but\n        # vps_host is None).\n        vps_host = os.environ.get(\"VPS_HOST\", \"\").strip() or None\n        vps_user = os.environ.get(\"VPS_USER\", \"\").strip() or \"root\"\n\n        if problems:\n"
    text = _apply_unique(text, old_load, new_load, "edit 2: load VPS env vars")

    # Edit 3: pass vps_host and vps_user to cls(...)
    old_ctor = "            claude_binary=claude_binary,\n            coder_mode=coder_mode,\n        )\n"
    new_ctor = "            claude_binary=claude_binary,\n            coder_mode=coder_mode,\n            vps_host=vps_host,\n            vps_user=vps_user,\n        )\n"
    text = _apply_unique(text, old_ctor, new_ctor, "edit 3: cls constructor kwargs")

    CONFIG_PY.write_text(text)
    info("patched config.py")
    return True


def patch_env_example() -> bool:
    text = ENV_EXAMPLE.read_text()
    if "VPS_HOST" in text:
        info(".env.example already has VPS_HOST — skipping")
        return False

    _backup(ENV_EXAMPLE)

    # Append two lines; the file uses fixed-column comments so match the style
    addition = (
        "VPS_HOST=                                            # VPS deployment — required when running briefs with vps_deploy: yes\n"
        "VPS_USER=root                                        # SSH user on VPS; default root matches Veronica's deploy pattern\n"
    )
    if not text.endswith("\n"):
        text += "\n"
    ENV_EXAMPLE.write_text(text + addition)
    info("appended VPS_HOST and VPS_USER to .env.example")
    return True


def create_test_config() -> bool:
    if TEST_CONFIG_PY.exists():
        info("tests/test_config.py already exists — skipping")
        return False

    content = '''"""Phase 3 Step 2 tests — Config gains vps_host and vps_user.

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
'''
    TEST_CONFIG_PY.write_text(content)
    info("created tests/test_config.py")
    return True


def main() -> int:
    if not CONFIG_PY.exists():
        fail(f"config.py not found at {CONFIG_PY}")
    if not ENV_EXAMPLE.exists():
        fail(f".env.example not found at {ENV_EXAMPLE}")

    changed_config = patch_config_py()
    changed_env = patch_env_example()
    changed_test = create_test_config()

    if not (changed_config or changed_env or changed_test):
        info("nothing to do — patch already fully applied")
        return 0

    import py_compile
    try:
        py_compile.compile(str(CONFIG_PY), doraise=True)
        py_compile.compile(str(TEST_CONFIG_PY), doraise=True)
        info("compile-check passed")
    except py_compile.PyCompileError as e:
        fail(f"compile-check failed: {e}")

    info("Step 2 patch applied. Next: run smoke")
    info("  .venv/bin/python -m unittest tests.test_config -v")
    return 0


if __name__ == "__main__":
    sys.exit(main())
