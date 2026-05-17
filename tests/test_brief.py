"""Step 3 tests — brief parsing, the 12 validation rules, context resolution.

Hermetic: creates a temp git repo for the trivial brief's target_repo_path
(only the trivial fixture's literal path /tmp/anvil-test-repo, created in
setUp and removed in tearDown iff this test created it) and a temp vault for
context-link resolution. No network, no commits to ~/Downloads/anvil.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from anvil.brief import (
    Brief,
    parse_brief,
    parse_brief_raw,
    validate_or_reject,
    resolve_context_paths,
)
from anvil.errors import BriefValidationError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRIVIAL = FIXTURES / "trivial-test-brief.md"
INVALID = FIXTURES / "invalid-brief.md"
TRIVIAL_REPO = Path("/tmp/anvil-test-repo")


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)


class TestBrief(unittest.TestCase):
    def setUp(self) -> None:
        # Trivial brief's target_repo_path is the literal /tmp/anvil-test-repo
        # (it's the exact brief Step 10 runs). Rule 3 needs it to be a git
        # repo; stand one up, remembering whether we created it.
        self._created_trivial_repo = not TRIVIAL_REPO.exists()
        if not (TRIVIAL_REPO / ".git").is_dir():
            _git_init(TRIVIAL_REPO)
        # Temp vault for context-link resolution tests.
        self._vault = Path(tempfile.mkdtemp(prefix="anvil-test-vault-"))
        (self._vault / "notes").mkdir()
        (self._vault / "notes" / "mynote.md").write_text("# mynote\n")

    def tearDown(self) -> None:
        if self._created_trivial_repo and TRIVIAL_REPO.exists():
            shutil.rmtree(TRIVIAL_REPO, ignore_errors=True)
        shutil.rmtree(self._vault, ignore_errors=True)

    # --- parsing -----------------------------------------------------------

    def test_trivial_parses(self) -> None:
        brief, fm = parse_brief_raw(TRIVIAL)
        self.assertEqual(brief.brief_version, 1)
        self.assertEqual(brief.project, "anvil-test")
        self.assertEqual(brief.vps_deploy, "no")
        self.assertIsNone(brief.end_to_end_test)
        self.assertEqual(brief.context_links, [])
        self.assertEqual([s.number for s in brief.steps], [1, 2, 3])
        self.assertEqual(brief.steps[0].name, "Create a file")
        self.assertEqual(
            [s.confirm for s in brief.steps], ["explicit", "auto", "explicit"]
        )
        self.assertEqual(brief.steps[0].scope_files, ["test.txt"])
        self.assertEqual(
            brief.steps[2].scope_operations, ["read", "smoke-test", "commit"]
        )
        # parse_brief (Component 2 API) returns the same Brief
        self.assertEqual(parse_brief(TRIVIAL).steps[1].name, "Modify the file")

    # --- clean validation --------------------------------------------------

    def test_trivial_validates_clean(self) -> None:
        brief, fm = parse_brief_raw(TRIVIAL)
        # /tmp/anvil-test-repo is a git repo (setUp). Should not raise.
        validate_or_reject(brief, raw_frontmatter=fm, vault_root=self._vault)

    # --- the validator lists ALL violations, not just the first -----------

    def test_invalid_rejected_lists_all(self) -> None:
        brief, fm = parse_brief_raw(INVALID)
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief, raw_frontmatter=fm, vault_root=self._vault)
        errs = ctx.exception.errors
        joined = "\n".join(errs).lower()
        # At least the major distinct violations, all present together:
        self.assertGreaterEqual(len(errs), 5, f"too few violations: {errs}")
        self.assertIn("project", joined)            # rule 1 missing key
        self.assertIn("build_name", joined)         # rule 1 missing key
        self.assertIn("brief_version must be 1", joined)   # rule 2
        self.assertTrue(                            # rule 3
            "not a git repo" in joined or "does not exist" in joined
        )
        self.assertIn("service_name", joined)       # rule 4
        self.assertIn("escapes target_repo_path", joined)  # rule 7
        self.assertIn("unknown operations", joined)        # rule 8
        self.assertIn("does/not/exist-xyzzy", joined)      # rule 11

    # --- context path resolution ------------------------------------------

    def test_resolve_context_paths_unresolved_raises(self) -> None:
        brief, _ = parse_brief_raw(INVALID)
        with self.assertRaises(BriefValidationError) as ctx:
            resolve_context_paths(brief, self._vault)
        self.assertIn("does/not/exist-xyzzy", "\n".join(ctx.exception.errors))

    def test_resolve_context_paths_resolves(self) -> None:
        # Positive: a link that exists in the temp vault.
        b = Brief(
            brief_version=1,
            project="x",
            build_name="x",
            target_repo="x",
            target_repo_path=Path("/tmp"),
            vps_deploy="no",
            context_links=["mynote"],
        )
        out = resolve_context_paths(b, self._vault)
        self.assertEqual(len(out.context_paths), 1)
        self.assertEqual(out.context_paths[0].name, "mynote.md")
        # Trivial brief has no context links — resolves to empty, no raise.
        trivial = parse_brief(TRIVIAL)
        self.assertEqual(resolve_context_paths(trivial, self._vault).context_paths, [])


if __name__ == "__main__":
    unittest.main()
