"""Tests for anvil.voice.load_voice_spec (design Part 8 / Phase 1 Step 1).

No real vault access. The canonical file is built under a tmp VAULT_PATH;
the snapshot constant is patched to a tmp file. unittest, not pytest:
the suite runs via `python -m unittest`, so tmp dirs come from
tempfile.TemporaryDirectory rather than a pytest tmp_path fixture.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from anvil import voice

_CANON = "canonical voice spec contents — read me at runtime\n"
_SNAP = "snapshot voice spec contents — committed fallback\n"


def _write_canonical(vault_dir: str, text: str) -> Path:
    path = Path(vault_dir) / voice._VAULT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class LoadVoiceSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self._vault = tempfile.TemporaryDirectory()
        self._snapdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._vault.cleanup)
        self.addCleanup(self._snapdir.cleanup)
        self.snapshot_path = Path(self._snapdir.name) / "_voice-snapshot.md"

    def _no_vault_path(self):
        env = mock.patch.dict(os.environ)
        env.start()
        os.environ.pop("VAULT_PATH", None)
        self.addCleanup(env.stop)

    def test_canonical_reachable_returns_canonical(self):
        _write_canonical(self._vault.name, _CANON)
        self.snapshot_path.write_text(_SNAP, encoding="utf-8")
        with mock.patch.dict(os.environ, {"VAULT_PATH": self._vault.name}), \
                mock.patch.object(voice, "_SNAPSHOT", self.snapshot_path):
            self.assertEqual(voice.load_voice_spec(), _CANON)

    def test_vault_path_unset_returns_snapshot(self):
        self._no_vault_path()
        self.snapshot_path.write_text(_SNAP, encoding="utf-8")
        with mock.patch.object(voice, "_SNAPSHOT", self.snapshot_path):
            self.assertEqual(voice.load_voice_spec(), _SNAP)

    def test_vault_path_set_file_missing_returns_snapshot(self):
        # VAULT_PATH points at a dir with no _voice.md under it.
        self.snapshot_path.write_text(_SNAP, encoding="utf-8")
        with mock.patch.dict(os.environ, {"VAULT_PATH": self._vault.name}), \
                mock.patch.object(voice, "_SNAPSHOT", self.snapshot_path):
            self.assertEqual(voice.load_voice_spec(), _SNAP)

    def test_canonical_oserror_returns_snapshot_with_warning(self):
        canonical = _write_canonical(self._vault.name, _CANON)
        self.snapshot_path.write_text(_SNAP, encoding="utf-8")
        os.chmod(canonical, 0o000)
        self.addCleanup(os.chmod, canonical, 0o644)
        if os.access(canonical, os.R_OK):
            self.skipTest("running with read override (root?); cannot force OSError")
        with mock.patch.dict(os.environ, {"VAULT_PATH": self._vault.name}), \
                mock.patch.object(voice, "_SNAPSHOT", self.snapshot_path):
            with self.assertLogs("anvil.voice", level="WARNING") as cm:
                result = voice.load_voice_spec()
        self.assertEqual(result, _SNAP)
        self.assertTrue(
            any("canonical _voice.md unreadable" in m for m in cm.output)
        )

    def test_neither_available_returns_empty_with_error(self):
        self._no_vault_path()
        missing = Path(self._snapdir.name) / "does-not-exist.md"
        with mock.patch.object(voice, "_SNAPSHOT", missing):
            with self.assertLogs("anvil.voice", level="ERROR") as cm:
                result = voice.load_voice_spec()
        self.assertEqual(result, "")
        self.assertTrue(
            any("neither canonical nor snapshot" in m for m in cm.output)
        )

    def test_drift_warning_fires_when_canonical_significantly_newer(self):
        canonical = _write_canonical(self._vault.name, _CANON)
        self.snapshot_path.write_text(_SNAP, encoding="utf-8")
        old = 1_000_000.0
        new = old + voice._DRIFT_SECONDS + 86_400  # 31 days newer
        os.utime(self.snapshot_path, (old, old))
        os.utime(canonical, (new, new))
        with mock.patch.dict(os.environ, {"VAULT_PATH": self._vault.name}), \
                mock.patch.object(voice, "_SNAPSHOT", self.snapshot_path):
            with self.assertLogs("anvil.voice", level="WARNING") as cm:
                result = voice.load_voice_spec()
        self.assertEqual(result, _CANON)
        self.assertTrue(
            any("more than 30 days newer" in m for m in cm.output)
        )


if __name__ == "__main__":
    unittest.main()
