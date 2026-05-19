"""Phase 4 Step 4 tests — anvil/checkpoint.py.

Hermetic: tmp_path-style fixture per test; mocks planner for draft_and_preview;
no real Anthropic calls, no real vault writes.

Covers 5 public functions:
  - draft_and_preview: planner success / planner escalation
  - render_preview_message: layout check
  - derive_checkpoint_path: slug / outcome suffix / date handling
  - compose_checkpoint_frontmatter: field shape
  - execute_writes: happy path / setup-log fail / checkpoint fail (partial)
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from anvil import checkpoint, vault_ops


def _brief(project="anvil", build_name="Phase 4 — vault writes"):
    return SimpleNamespace(
        project=project,
        build_name=build_name,
        target_repo_path=Path("/tmp/fake-repo"),
    )


def _state(escalations=0, started="2026-05-19T14:22:00+01:00", deploy=None):
    return SimpleNamespace(
        started_at=started,
        brief_path="/vault/01-Projects/code-workspace/anvil/builds/2026-05-19-anvil-phase-4/brief.md",
        escalation_count=escalations,
        deploy=deploy,
        status="done",
    )


class TestDraftAndPreview(unittest.TestCase):

    def test_planner_success_returns_draft(self) -> None:
        planner = MagicMock()
        planner.draft_completion_artefacts.return_value = {
            "setup_log_entry": "## 2026-05-19\nbody",
            "checkpoint": "# Title\nbody",
        }
        draft, err = checkpoint.draft_and_preview(_brief(), _state(), planner)
        self.assertIsNotNone(draft)
        self.assertEqual(err, "")
        self.assertIn("setup_log_entry", draft)

    def test_planner_escalation_returns_none(self) -> None:
        planner = MagicMock()
        planner.draft_completion_artefacts.return_value = {
            "escalate": True,
            "reason": "completion-artefacts-draft-failed",
            "detail": "everything broke",
            "step_number": 0,
        }
        draft, err = checkpoint.draft_and_preview(_brief(), _state(), planner)
        self.assertIsNone(draft)
        self.assertEqual(err, "everything broke")


class TestRenderPreviewMessage(unittest.TestCase):

    def test_layout_includes_both_artefacts_and_prompt(self) -> None:
        draft = {
            "setup_log_entry": "## 2026-05-19 — entry\n\nfoo",
            "checkpoint": "# Title\n\n## What changed\n\nbar",
        }
        msg = checkpoint.render_preview_message(
            draft,
            Path("/vault/anvil/setup-log.md"),
            Path("/vault/2026-05-19-anvil-phase-4-shipped.md"),
        )
        self.assertTrue(msg.startswith("[ANVIL]"))
        self.assertIn("setup-log.md", msg)
        self.assertIn("2026-05-19-anvil-phase-4-shipped.md", msg)
        self.assertIn("## 2026-05-19 — entry", msg)
        self.assertIn("# Title", msg)
        self.assertIn("go / abort", msg)


class TestDeriveCheckpointPath(unittest.TestCase):

    def test_clean_run_gets_shipped_suffix(self) -> None:
        path = checkpoint.derive_checkpoint_path(
            _brief(), _state(escalations=0), Path("/vault"),
        )
        self.assertEqual(path.name, "2026-05-19-phase-4-vault-writes-shipped.md")

    def test_run_with_escalations_gets_caveats_suffix(self) -> None:
        path = checkpoint.derive_checkpoint_path(
            _brief(), _state(escalations=2), Path("/vault"),
        )
        self.assertEqual(
            path.name,
            "2026-05-19-phase-4-vault-writes-shipped-with-caveats.md",
        )

    def test_path_lives_under_checkpoints_active(self) -> None:
        path = checkpoint.derive_checkpoint_path(
            _brief(), _state(), Path("/vault"),
        )
        self.assertIn("01-Projects/second-brain/checkpoints/active", str(path))


class TestComposeCheckpointFrontmatter(unittest.TestCase):

    def test_seven_fields_present(self) -> None:
        fm = checkpoint.compose_checkpoint_frontmatter(
            _brief(), _state(), git_commit="abc1234",
        )
        self.assertEqual(set(fm.keys()), {
            "date", "source", "project", "tags", "author", "brief", "git_commit",
        })

    def test_source_is_anvil(self) -> None:
        fm = checkpoint.compose_checkpoint_frontmatter(_brief(), _state())
        self.assertEqual(fm["source"], "anvil")

    def test_tags_includes_project(self) -> None:
        fm = checkpoint.compose_checkpoint_frontmatter(
            _brief(project="veronica"), _state(),
        )
        self.assertIn("veronica", fm["tags"])
        self.assertIn("anvil", fm["tags"])
        self.assertIn("checkpoint", fm["tags"])

    def test_brief_path_made_relative_to_01_projects(self) -> None:
        fm = checkpoint.compose_checkpoint_frontmatter(_brief(), _state())
        self.assertTrue(fm["brief"].startswith("01-Projects/"),
                        f"got: {fm['brief']!r}")

    def test_date_extracted_from_iso_started_at(self) -> None:
        fm = checkpoint.compose_checkpoint_frontmatter(
            _brief(), _state(started="2026-05-19T14:22:00+01:00"),
        )
        self.assertEqual(fm["date"], "2026-05-19")


class TestExecuteWrites(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-cp-exec-"))
        self.setup_log = self.tmpdir / "setup-log.md"
        self.setup_log.write_text("# setup-log\n", encoding="utf-8")
        self.checkpoint_path = self.tmpdir / "2026-05-19-test-shipped.md"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_happy_path_both_writes_succeed(self) -> None:
        draft = {
            "setup_log_entry": "## 2026-05-19 — entry\n\nfoo",
            "checkpoint": "# Title\n\n## What changed\n\nbar",
        }
        fm = {"date": "2026-05-19", "source": "anvil"}
        ok, err = checkpoint.execute_writes(
            draft, self.setup_log, self.checkpoint_path, fm,
        )
        self.assertTrue(ok, f"unexpected error: {err}")
        self.assertIn("## 2026-05-19 — entry", self.setup_log.read_text())
        self.assertTrue(self.checkpoint_path.exists())

    def test_setup_log_missing_fails_no_checkpoint(self) -> None:
        """If setup-log doesn't exist, fail early; checkpoint not attempted."""
        draft = {"setup_log_entry": "## x\nb", "checkpoint": "# Title\nb"}
        ok, err = checkpoint.execute_writes(
            draft, self.tmpdir / "nope.md", self.checkpoint_path, {},
        )
        self.assertFalse(ok)
        self.assertIn("setup-log", err)
        self.assertFalse(self.checkpoint_path.exists())

    def test_checkpoint_failure_setup_log_persists(self) -> None:
        """If setup-log succeeds but checkpoint fails, setup-log entry stays."""
        draft = {
            "setup_log_entry": "## entry\n\nfoo",
            "checkpoint": "# Title\nbar",
        }
        # Inject failure on the write_checkpoint call only
        def _injected_write_checkpoint(*a, **k):
            return (False, "simulated disk error")
        with patch.object(vault_ops, "write_checkpoint", _injected_write_checkpoint):
            ok, err = checkpoint.execute_writes(
                draft, self.setup_log, self.checkpoint_path, {},
            )
        self.assertFalse(ok)
        self.assertIn("setup-log entry persisted", err)
        # Setup-log entry actually made it to disk
        self.assertIn("## entry", self.setup_log.read_text())


if __name__ == "__main__":
    unittest.main()
