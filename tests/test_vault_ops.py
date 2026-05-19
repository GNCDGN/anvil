"""Phase 4 Step 2 tests — anvil/vault_ops.py.

Hermetic: tmp_path-style fixture per test via tempfile.mkdtemp. Patches
anvil.vault_ops._real_write for failure injection. No real vault writes.

Covers 4 public functions across 13 tests:
  - atomic_write_text: happy / permission-denied / encoding-error / tmp-cleanup
  - append_setup_log_entry: happy / source-missing / source-empty
  - write_checkpoint: happy / frontmatter-rendering / idempotent-skip / write-failure
  - derive_setup_log_path: anvil brief / veronica brief
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anvil import vault_ops


class TestAtomicWriteText(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-vault-aw-"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_happy_path_writes_file(self) -> None:
        target = self.tmpdir / "out.md"
        ok, err = vault_ops.atomic_write_text(target, "hello world\n")
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world\n")

    def test_write_failure_returns_false_no_raise(self) -> None:
        """Patch _real_write to raise OSError; verify (False, error) returned."""
        target = self.tmpdir / "fail.md"
        def _raise(self, *a, **k):
            raise OSError("permission denied (simulated)")
        with patch.object(vault_ops, "_real_write", _raise):
            ok, err = vault_ops.atomic_write_text(target, "content")
        self.assertFalse(ok)
        self.assertIn("OSError", err)
        self.assertIn("permission denied", err)
        # Tmp file should be cleaned up
        self.assertFalse((self.tmpdir / "fail.md.tmp").exists())

    def test_encoding_error_caught(self) -> None:
        """Encoding errors surface as (False, error), not exceptions."""
        target = self.tmpdir / "enc.md"
        def _raise(self, *a, **k):
            raise UnicodeError("simulated encoding error")
        with patch.object(vault_ops, "_real_write", _raise):
            ok, err = vault_ops.atomic_write_text(target, "x")
        self.assertFalse(ok)
        self.assertIn("UnicodeError", err)

    def test_tmp_cleanup_on_failure(self) -> None:
        """If write succeeds but os.replace fails (e.g. target dir vanished),
        tmp file is best-effort cleaned up."""
        target = self.tmpdir / "subdir" / "out.md"
        # Parent doesn't exist — write_text will fail
        ok, err = vault_ops.atomic_write_text(target, "content")
        self.assertFalse(ok)
        # Tmp shouldn't linger (best-effort; cleanup may itself fail silently
        # but the directory at least shouldn't have a stray .tmp at this path)
        self.assertFalse((self.tmpdir / "subdir" / "out.md.tmp").exists())


class TestAppendSetupLogEntry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-vault-append-"))
        self.setup_log = self.tmpdir / "setup-log.md"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_appends_with_separator(self) -> None:
        """Existing setup-log gets new entry appended with blank-line separator."""
        self.setup_log.write_text("# setup-log\n\n## prior entry\n\nfoo\n", encoding="utf-8")
        new_entry = "## new entry\n\nbar"
        ok, err = vault_ops.append_setup_log_entry(self.setup_log, new_entry)
        self.assertTrue(ok, f"unexpected error: {err}")
        content = self.setup_log.read_text(encoding="utf-8")
        # Both entries present
        self.assertIn("## prior entry", content)
        self.assertIn("## new entry", content)
        # Order preserved (append, not prepend)
        self.assertLess(content.index("## prior entry"), content.index("## new entry"))

    def test_missing_source_refuses(self) -> None:
        """No existing setup-log → (False, error), no creation."""
        missing = self.tmpdir / "does-not-exist.md"
        ok, err = vault_ops.append_setup_log_entry(missing, "## entry")
        self.assertFalse(ok)
        self.assertIn("setup-log not found", err)
        self.assertFalse(missing.exists())

    def test_empty_source_appends_cleanly(self) -> None:
        """Empty setup-log file → entry written without leading blanks."""
        self.setup_log.write_text("", encoding="utf-8")
        ok, _ = vault_ops.append_setup_log_entry(self.setup_log, "## first entry\n\nbody")
        self.assertTrue(ok)
        content = self.setup_log.read_text(encoding="utf-8")
        self.assertEqual(content, "## first entry\n\nbody\n")


class TestWriteCheckpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-vault-cp-"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_happy_path_writes_frontmatter_and_body(self) -> None:
        target = self.tmpdir / "2026-05-19-test-shipped.md"
        fm = {
            "date": "2026-05-19",
            "source": "anvil",
            "project": "anvil",
            "tags": ["checkpoint", "anvil", "phase-4"],
            "author": "claude",
        }
        body = "# Checkpoint\n\n## What changed\n\nfoo"
        ok, err = vault_ops.write_checkpoint(target, fm, body)
        self.assertTrue(ok, f"unexpected error: {err}")
        content = target.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        # Frontmatter has all five keys
        for key in fm.keys():
            self.assertIn(f"{key}:", content)
        # Body present
        self.assertIn("## What changed", content)
        # Closing frontmatter delimiter exists
        self.assertEqual(content.count("---\n"), 2)

    def test_list_value_renders_as_inline_array(self) -> None:
        """Tags list serialises as `tags: [checkpoint, anvil, phase-4]`."""
        target = self.tmpdir / "2026-05-19-tag-test.md"
        fm = {"tags": ["checkpoint", "anvil", "phase-4"]}
        ok, _ = vault_ops.write_checkpoint(target, fm, "body")
        self.assertTrue(ok)
        content = target.read_text(encoding="utf-8")
        self.assertIn("tags: [checkpoint, anvil, phase-4]", content)

    def test_idempotent_skip_when_file_exists(self) -> None:
        """Re-run with existing file → (True, 'exists; skipped'), no overwrite."""
        target = self.tmpdir / "2026-05-19-already-here.md"
        target.write_text("ORIGINAL CONTENT", encoding="utf-8")
        ok, err = vault_ops.write_checkpoint(target, {"date": "2026-05-19"}, "new body")
        self.assertTrue(ok)
        self.assertEqual(err, "exists; skipped")
        # Original content preserved
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL CONTENT")

    def test_write_failure_returns_false(self) -> None:
        """_real_write raises → (False, error). No file created."""
        target = self.tmpdir / "fail.md"
        def _raise(self, *a, **k):
            raise OSError("disk full (simulated)")
        with patch.object(vault_ops, "_real_write", _raise):
            ok, err = vault_ops.write_checkpoint(target, {"date": "2026-05-19"}, "body")
        self.assertFalse(ok)
        self.assertIn("OSError", err)
        self.assertFalse(target.exists())


class TestDeriveSetupLogPath(unittest.TestCase):
    def test_anvil_build_brief(self) -> None:
        """ANVIL Phase 4 brief → anvil/setup-log.md."""
        brief = Path("/vault/01-Projects/code-workspace/anvil/builds/2026-05-19-anvil-phase-4/brief.md")
        result = vault_ops.derive_setup_log_path(brief)
        self.assertEqual(
            result,
            Path("/vault/01-Projects/code-workspace/anvil/setup-log.md"),
        )

    def test_veronica_build_brief(self) -> None:
        """Veronica Phase 4a brief → veronica/setup-log.md."""
        brief = Path("/vault/01-Projects/second-brain/veronica/builds/2026-05-19-veronica-v4-phase-4a-deploy/brief.md")
        result = vault_ops.derive_setup_log_path(brief)
        self.assertEqual(
            result,
            Path("/vault/01-Projects/second-brain/veronica/setup-log.md"),
        )


if __name__ == "__main__":
    unittest.main()
