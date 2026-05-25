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
    EndToEndTest,
    Step,
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


class TestRule12ScriptDefensive(unittest.TestCase):
    """v4 Phase 1a housekeeping: rule 12 must reject a non-path
    end_to_end_test.script value cleanly (BriefValidationError), never crash
    on os.stat. The 2026-05-20-anvil-v2-phase-1 brief carried a prose sentence
    there, which raised OSError [Errno 63] File name too long before the
    _script_exists guard."""

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-rule12-"))
        _git_init(self._repo)

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)

    def _brief_with_e2e_script(self, script: str) -> Brief:
        # Otherwise-valid brief so rule 12 is the only violation: real git
        # repo (setUp), one in-scope step, inline smoke (the space makes rule 9
        # skip its own is_file path).
        return Brief(
            brief_version=1,
            project="p",
            build_name="b",
            target_repo="r",
            target_repo_path=self._repo,
            vps_deploy="no",
            steps=[
                Step(
                    number=1,
                    name="s",
                    scope_files=[],
                    scope_operations=["read"],
                    smoke="echo hi",
                    confirm="explicit",
                )
            ],
            end_to_end_test=EndToEndTest(script=script),
        )

    def test_prose_script_rejected_cleanly(self) -> None:
        # Short prose value — the shape of the offending v2 Phase 1 brief.
        brief = self._brief_with_e2e_script(
            "(no script — this brief has vps_deploy: no; prose, not a path)"
        )
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief)
        self.assertIn(
            "end_to_end_test.script does not exist",
            " ".join(ctx.exception.errors),
        )

    def test_pathologically_long_script_rejected_not_crash(self) -> None:
        # A single path component longer than NAME_MAX makes os.stat raise
        # OSError [Errno 63]; without the _script_exists guard this crashed
        # validate_or_reject instead of producing a clean rejection.
        brief = self._brief_with_e2e_script("X" * 5000)
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief)
        self.assertIn(
            "end_to_end_test.script does not exist",
            " ".join(ctx.exception.errors),
        )


class TestStepModelField(unittest.TestCase):
    """v4 Phase 1a Step 2: per-step `model:` field — parse, validate (rule 13),
    the Q-A4 non-LLM-step warning, defensive empty/null handling, and the
    existing-briefs backwards-compat guard (reframed criterion 2: every parsed
    brief must have model=None on every step)."""

    _BUILDS = Path(
        "/Users/gencodoganay/vaults/second-brain/01-Projects/"
        "code-workspace/anvil/builds"
    )

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-model-"))
        _git_init(self._repo)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-model-brief-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _parse(self, *, operations: str = "read", model_line: str = ""):
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {self._repo}
vps_deploy: no
---

## Goal

Exercise the per-step model field.

## Steps

### Step 1 — A step

- **scope.files:**
- **scope.operations:** {operations}
- **smoke:** `echo hi`
- **confirm:** explicit
{model_line}
"""
        p = self._tmpdir / "brief.md"
        p.write_text(body, encoding="utf-8")
        return parse_brief_raw(p)

    # --- parsing + validation -----------------------------------------------
    def test_valid_alias_parses_and_validates(self) -> None:
        brief, fm = self._parse(model_line="- **model:** haiku")
        self.assertEqual(brief.steps[0].model, "haiku")
        validate_or_reject(brief, fm)  # no raise

    def test_valid_version_string_parses_and_validates(self) -> None:
        brief, fm = self._parse(
            model_line="- **model:** claude-haiku-4-5-20251001")
        self.assertEqual(brief.steps[0].model, "claude-haiku-4-5-20251001")
        validate_or_reject(brief, fm)  # no raise

    def test_unknown_model_rejected(self) -> None:
        brief, fm = self._parse(model_line="- **model:** gpt-4")
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief, fm)
        self.assertIn("model 'gpt-4'", " ".join(ctx.exception.errors))

    def test_sonnet_alias_rejected(self) -> None:
        # Amendment 1 negative test: sonnet was dropped from MODEL_ALIASES.
        brief, fm = self._parse(model_line="- **model:** sonnet")
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief, fm)
        self.assertIn("model 'sonnet'", " ".join(ctx.exception.errors))

    def test_missing_model_is_none_and_validates(self) -> None:
        brief, fm = self._parse(model_line="")
        self.assertIsNone(brief.steps[0].model)
        validate_or_reject(brief, fm)  # no raise

    # --- defensive edge cases -----------------------------------------------
    def test_empty_string_model_treated_as_absent(self) -> None:
        brief, fm = self._parse(model_line="- **model:** ")
        self.assertIsNone(brief.steps[0].model)
        validate_or_reject(brief, fm)  # no raise

    def test_null_model_treated_as_absent(self) -> None:
        brief, fm = self._parse(model_line="- **model:** null")
        self.assertIsNone(brief.steps[0].model)
        validate_or_reject(brief, fm)  # no raise

    # --- Q-A4: model on a non-LLM-calling step ------------------------------
    def test_model_on_non_llm_step_warns_and_validates(self) -> None:
        brief, fm = self._parse(
            operations="smoke-test, commit", model_line="- **model:** haiku")
        # Field still parsed (not stripped); brief still validates (warning,
        # not error)...
        self.assertEqual(brief.steps[0].model, "haiku")
        validate_or_reject(brief, fm)  # no raise
        # ...and a Q-A4 warning is recorded on the brief.
        self.assertIn(
            "model-on-non-llm-step",
            [w.get("kind") for w in brief.parse_warnings],
        )

    def test_model_on_llm_step_no_warning(self) -> None:
        brief, _ = self._parse(
            operations="write", model_line="- **model:** haiku")
        self.assertNotIn(
            "model-on-non-llm-step",
            [w.get("kind") for w in brief.parse_warnings],
        )

    # --- reframed criterion 2: existing-briefs backwards-compat guard -------
    def test_existing_briefs_model_field_none(self) -> None:
        briefs = sorted(self._BUILDS.glob("*/brief.md"))
        self.assertTrue(briefs, "no existing briefs found to guard")
        parsed = 0
        parse_failed: list[tuple[str, str]] = []
        for b in briefs:
            try:
                brief, _ = parse_brief_raw(b)
            except Exception as exc:  # pre-existing parse issue — out of scope
                parse_failed.append((b.parent.name, type(exc).__name__))
                continue
            parsed += 1
            for step in brief.steps:
                self.assertIsNone(
                    step.model,
                    f"{b.parent.name} step {step.number}: model must be None "
                    f"(Step 2 must not populate it), got {step.model!r}",
                )
                # v4 Phase 1b Step 1: the new issues: field must also default to
                # None on every existing brief (backwards-compat — the field is
                # opt-in; no existing brief declares it).
                self.assertIsNone(
                    step.issues,
                    f"{b.parent.name} step {step.number}: issues must be None "
                    f"(opt-in field; no existing brief declares it), got "
                    f"{step.issues!r}",
                )
        # Success: every parsed brief had model=None AND issues=None on every
        # step. Briefs that fail to parse are pre-existing drift, not a regression.
        self.assertGreater(parsed, 0, f"no briefs parsed; failures={parse_failed}")


class TestStepIssuesField(unittest.TestCase):
    """v4 Phase 1b Step 1 (Q-B3): per-step `issues:` connector-scope field —
    parse, validate (rule 14), defensive empty/null/none handling, and the
    Q-B3 coexistence rule: model: and issues: validate independently on the
    same step, and the model: Q-A4 warning keys on scope.operations, NOT on
    issues: presence."""

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-issues-"))
        _git_init(self._repo)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-issues-brief-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _parse(self, *, operations: str = "read",
               issues_line: str = "", model_line: str = ""):
        extra = "\n".join(x for x in (model_line, issues_line) if x)
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {self._repo}
vps_deploy: no
---

## Goal

Exercise the per-step issues field.

## Steps

### Step 1 — A step

- **scope.files:**
- **scope.operations:** {operations}
- **smoke:** `echo hi`
- **confirm:** explicit
{extra}
"""
        p = self._tmpdir / "brief.md"
        p.write_text(body, encoding="utf-8")
        return parse_brief_raw(p)

    # --- parsing + validation -----------------------------------------------
    def test_read_parses_and_validates(self) -> None:
        brief, fm = self._parse(issues_line="- **issues:** read")
        self.assertEqual(brief.steps[0].issues, "read")
        validate_or_reject(brief, fm)  # no raise

    def test_write_parses_and_validates(self) -> None:
        brief, fm = self._parse(
            operations="write", issues_line="- **issues:** write")
        self.assertEqual(brief.steps[0].issues, "write")
        validate_or_reject(brief, fm)  # no raise

    def test_unknown_value_rejected(self) -> None:
        for bad in ("delete", "edit", "merge"):
            with self.subTest(value=bad):
                brief, fm = self._parse(issues_line=f"- **issues:** {bad}")
                with self.assertRaises(BriefValidationError) as ctx:
                    validate_or_reject(brief, fm)
                self.assertIn(f"issues '{bad}'", " ".join(ctx.exception.errors))

    def test_missing_is_none_and_validates(self) -> None:
        brief, fm = self._parse(issues_line="")
        self.assertIsNone(brief.steps[0].issues)
        validate_or_reject(brief, fm)  # no raise

    # --- defensive edge cases (mirror model:'s null/none/empty handling) -----
    def test_blank_values_treated_as_absent(self) -> None:
        for line in ("- **issues:** ", "- **issues:** null", "- **issues:** none"):
            with self.subTest(line=line):
                brief, fm = self._parse(issues_line=line)
                self.assertIsNone(brief.steps[0].issues)
                validate_or_reject(brief, fm)  # no raise

    # --- Q-B3 coexistence: model: + issues: on the same step ----------------
    def test_model_and_issues_coexist(self) -> None:
        brief, fm = self._parse(
            operations="write",
            model_line="- **model:** haiku",
            issues_line="- **issues:** write",
        )
        self.assertEqual(brief.steps[0].model, "haiku")
        self.assertEqual(brief.steps[0].issues, "write")
        validate_or_reject(brief, fm)  # both validate independently

    def test_model_warning_independent_of_issues(self) -> None:
        # A non-LLM step (smoke-test/commit) carrying BOTH model: and issues:
        # write must still emit the Q-A4 model-on-non-llm-step warning — the
        # issues: declaration does not suppress it (the model: warning keys on
        # scope.operations ∩ LLM_CALLING_OPERATIONS, not on issues: presence).
        brief, fm = self._parse(
            operations="smoke-test, commit",
            model_line="- **model:** haiku",
            issues_line="- **issues:** write",
        )
        validate_or_reject(brief, fm)  # no raise (warning, not error)
        self.assertIn(
            "model-on-non-llm-step",
            [w.get("kind") for w in brief.parse_warnings],
        )
