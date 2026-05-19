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



# ---------------------------------------------------------------------------
# Phase 3 Step 1 — vps_target_path field + validation rule 4 extension
# ---------------------------------------------------------------------------

class TestPhase3VpsTargetPath(unittest.TestCase):
    """Phase 3 Step 1: brief schema gains vps_target_path; validation rule 4
    extends so vps_deploy: yes requires service_name AND vps_target_path."""

    def setUp(self) -> None:
        self._created_repo = not TRIVIAL_REPO.exists()
        if not (TRIVIAL_REPO / ".git").is_dir():
            _git_init(TRIVIAL_REPO)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-vps-target-"))

    def tearDown(self) -> None:
        if self._created_repo and TRIVIAL_REPO.exists():
            shutil.rmtree(TRIVIAL_REPO)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_brief(self, frontmatter_body: str) -> Path:
        p = self._tmpdir / "brief.md"
        p.write_text(frontmatter_body)
        return p

    _BASE_BODY = """
## Goal
Trivial.

## Context

## Steps

### Step 1 — noop
- **scope.files:**
- **scope.operations:** read
- **smoke:** `true`
- **confirm:** auto
"""

    def test_vps_deploy_yes_missing_vps_target_path_rejected(self) -> None:
        """vps_deploy: yes + service_name present + vps_target_path missing -> rejected."""
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {TRIVIAL_REPO}
vps_deploy: yes
service_name: test.service
---
{self._BASE_BODY}"""
        p = self._write_brief(body)
        brief = parse_brief(p)
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief)
        msg = str(ctx.exception)
        self.assertIn("vps_target_path", msg)

    def test_vps_deploy_yes_missing_service_name_still_rejected(self) -> None:
        """Regression: rule 4's existing service_name check still fires."""
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {TRIVIAL_REPO}
vps_deploy: yes
vps_target_path: /home/test/test
---
{self._BASE_BODY}"""
        p = self._write_brief(body)
        brief = parse_brief(p)
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief)
        msg = str(ctx.exception)
        self.assertIn("service_name", msg)

    def test_vps_deploy_yes_missing_both_reports_both(self) -> None:
        """vps_deploy: yes + both missing -> rejection lists both errors."""
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {TRIVIAL_REPO}
vps_deploy: yes
---
{self._BASE_BODY}"""
        p = self._write_brief(body)
        brief = parse_brief(p)
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief)
        msg = str(ctx.exception)
        self.assertIn("service_name", msg)
        self.assertIn("vps_target_path", msg)

    def test_vps_deploy_yes_both_present_accepted(self) -> None:
        """vps_deploy: yes + both fields present -> accepted."""
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {TRIVIAL_REPO}
vps_deploy: yes
service_name: test.service
vps_target_path: /home/test/test
---
{self._BASE_BODY}"""
        p = self._write_brief(body)
        brief = parse_brief(p)
        validate_or_reject(brief)  # no exception
        self.assertEqual(brief.vps_target_path, "/home/test/test")
        self.assertEqual(brief.service_name, "test.service")

    def test_vps_deploy_no_vps_target_path_absent_accepted(self) -> None:
        """vps_deploy: no + vps_target_path absent -> accepted (field is conditional)."""
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {TRIVIAL_REPO}
vps_deploy: no
---
{self._BASE_BODY}"""
        p = self._write_brief(body)
        brief = parse_brief(p)
        validate_or_reject(brief)  # no exception
        self.assertIsNone(brief.vps_target_path)

    def test_vps_deploy_no_vps_target_path_present_accepted(self) -> None:
        """vps_deploy: no + vps_target_path populated -> accepted, unused but harmless."""
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {TRIVIAL_REPO}
vps_deploy: no
vps_target_path: /home/test/test
---
{self._BASE_BODY}"""
        p = self._write_brief(body)
        brief = parse_brief(p)
        validate_or_reject(brief)  # no exception
        self.assertEqual(brief.vps_target_path, "/home/test/test")
