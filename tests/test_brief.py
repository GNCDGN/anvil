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
        # rule 2: the invalid-brief.md fixture carries brief_version: 2, which is
        # now VALID (v4 Phase 2b relaxed rule 2 to {1, 2} — Q-E3-F1). The
        # version-rejection path moved to the dedicated TestRule2Relaxation class;
        # this omnibus fixture is still rejected for its ~6 other violations below.
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

    def test_sonnet_alias_accepted(self) -> None:
        # v4 Phase 3a (Step 0 Q-A5 / DC4): sonnet restored to MODEL_ALIASES, so a
        # `model: sonnet` step now validates (was rejected in Phase 1a,
        # Amendment 1, when sonnet was dropped).
        brief, fm = self._parse(model_line="- **model:** sonnet")
        validate_or_reject(brief, fm)  # must not raise
        self.assertEqual(brief.steps[0].model, "sonnet")

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
                # v4 Phase 1b Step 2: likewise the new sentry: field defaults to
                # None on every existing brief.
                self.assertIsNone(
                    step.sentry,
                    f"{b.parent.name} step {step.number}: sentry must be None "
                    f"(opt-in field; no existing brief declares it), got "
                    f"{step.sentry!r}",
                )
                # v4 Phase 2b Step 1: the new per-step observe: block must also
                # default to None on every existing brief (backwards-compat — the
                # field is opt-in, version-gated to brief_version: 2, and no
                # existing brief declares it; they are all v1). Per the reframed
                # criterion 2 (V4P1A-2): assertion is on PARSE, not validate.
                self.assertIsNone(
                    step.observe,
                    f"{b.parent.name} step {step.number}: observe must be None "
                    f"(opt-in field; no existing brief declares it), got "
                    f"{step.observe!r}",
                )
            # v4 Phase 1c Step 1: the new per-brief deploy_target: field defaults
            # to None on every existing brief (none declares it yet). (Briefs with
            # vps_deploy: yes emit the vps-deploy-deprecated warning here — that is
            # the expected operational signal, not a field-population; deploy_target
            # stays None.)
            self.assertIsNone(
                brief.deploy_target,
                f"{b.parent.name}: deploy_target must be None (opt-in field; no "
                f"existing brief declares it), got {brief.deploy_target!r}",
            )
        # Success: every parsed brief had model/issues/sentry = None on every step
        # AND deploy_target = None. Briefs that fail to parse are pre-existing
        # drift, not a regression.
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


class TestStepSentryField(unittest.TestCase):
    """v4 Phase 1b Step 2 (Q-B3): per-step `sentry:` connector-scope field —
    parse, validate (rule 15: read-only), defensive empty/null/none handling,
    and the Q-B3 triple-coexistence rule (model: + issues: + sentry: validate
    independently on the same step)."""

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-sentry-"))
        _git_init(self._repo)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-sentry-brief-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _parse(self, *, operations: str = "read", model_line: str = "",
               issues_line: str = "", sentry_line: str = ""):
        extra = "\n".join(
            x for x in (model_line, issues_line, sentry_line) if x)
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {self._repo}
vps_deploy: no
---

## Goal

Exercise the per-step sentry field.

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
        brief, fm = self._parse(sentry_line="- **sentry:** read")
        self.assertEqual(brief.steps[0].sentry, "read")
        validate_or_reject(brief, fm)  # no raise

    def test_unknown_value_rejected(self) -> None:
        # 'write' is invalid for the read-only Sentry connector (rule 15).
        for bad in ("write", "edit", "delete"):
            with self.subTest(value=bad):
                brief, fm = self._parse(sentry_line=f"- **sentry:** {bad}")
                with self.assertRaises(BriefValidationError) as ctx:
                    validate_or_reject(brief, fm)
                self.assertIn(f"sentry '{bad}'", " ".join(ctx.exception.errors))

    def test_missing_is_none_and_validates(self) -> None:
        brief, fm = self._parse(sentry_line="")
        self.assertIsNone(brief.steps[0].sentry)
        validate_or_reject(brief, fm)  # no raise

    def test_blank_values_treated_as_absent(self) -> None:
        for line in ("- **sentry:** ", "- **sentry:** null", "- **sentry:** none"):
            with self.subTest(line=line):
                brief, fm = self._parse(sentry_line=line)
                self.assertIsNone(brief.steps[0].sentry)
                validate_or_reject(brief, fm)  # no raise

    # --- Q-B3 triple-coexistence: model: + issues: + sentry: ----------------
    def test_model_issues_sentry_coexist(self) -> None:
        brief, fm = self._parse(
            operations="write",
            model_line="- **model:** haiku",
            issues_line="- **issues:** write",
            sentry_line="- **sentry:** read",
        )
        step = brief.steps[0]
        self.assertEqual(step.model, "haiku")
        self.assertEqual(step.issues, "write")
        self.assertEqual(step.sentry, "read")
        validate_or_reject(brief, fm)  # all three validate independently


class TestBriefDeployTarget(unittest.TestCase):
    """v4 Phase 1c Step 1 (Q-C3): the per-brief `deploy_target:` frontmatter
    field — parse, validate (rule 16), defensive empty/null/none handling, the
    vps_deploy: coexistence warnings (vps-deploy-deprecated / deploy-target-
    mismatch, neither rejecting), and the four-axis coexistence (model: +
    issues: + sentry: + deploy_target:)."""

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-deploy-"))
        _git_init(self._repo)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-deploy-brief-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _parse(self, *, vps_deploy: str = "no", deploy_target_line: str = "",
               step_extra: str = ""):
        # Rule 4: vps_deploy: yes requires service_name + vps_target_path. Supply
        # them so a yes-fixture exercises the deploy_target logic, not rule 4.
        vps_extra = (
            "service_name: test-svc\nvps_target_path: /srv/test\n"
            if vps_deploy == "yes" else ""
        )
        body = f"""---
brief_version: 1
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {self._repo}
vps_deploy: {vps_deploy}
{vps_extra}{deploy_target_line}
---

## Goal

Exercise the per-brief deploy_target field.

## Steps

### Step 1 — A step

- **scope.files:**
- **scope.operations:** read
- **smoke:** `echo hi`
- **confirm:** explicit
{step_extra}
"""
        p = self._tmpdir / "brief.md"
        p.write_text(body, encoding="utf-8")
        return parse_brief_raw(p)

    def _kinds(self, brief):
        return [w.get("kind") for w in brief.parse_warnings]

    # --- parsing + validation -----------------------------------------------
    def test_valid_targets_parse_and_validate(self) -> None:
        # vps/vercel/netlify validate clean; "none" normalizes to None (still valid).
        for tgt in ("vps", "vercel", "netlify", "none"):
            with self.subTest(target=tgt):
                # vps_deploy chosen to avoid a mismatch warning muddying the case.
                vps = "yes" if tgt == "vps" else "no"
                brief, fm = self._parse(
                    vps_deploy=vps, deploy_target_line=f"deploy_target: {tgt}")
                validate_or_reject(brief, fm)  # no raise
                expected = None if tgt == "none" else tgt
                self.assertEqual(brief.deploy_target, expected)

    def test_unknown_target_rejected(self) -> None:
        for bad in ("aws", "cloudflare", "lambda"):
            with self.subTest(target=bad):
                brief, fm = self._parse(deploy_target_line=f"deploy_target: {bad}")
                with self.assertRaises(BriefValidationError) as ctx:
                    validate_or_reject(brief, fm)
                self.assertIn(f"deploy_target '{bad}'", " ".join(ctx.exception.errors))

    def test_absent_is_none(self) -> None:
        brief, fm = self._parse(deploy_target_line="")
        self.assertIsNone(brief.deploy_target)
        validate_or_reject(brief, fm)  # no raise

    def test_blank_values_treated_as_absent(self) -> None:
        for val in ("", "null", "none"):
            with self.subTest(value=val):
                brief, fm = self._parse(deploy_target_line=f"deploy_target: {val}")
                self.assertIsNone(brief.deploy_target)
                validate_or_reject(brief, fm)  # no raise

    # --- vps_deploy: coexistence warnings (Q-C3) ----------------------------
    def test_vps_deploy_yes_absent_target_warns_deprecated(self) -> None:
        brief, fm = self._parse(vps_deploy="yes", deploy_target_line="")
        validate_or_reject(brief, fm)  # warning, not error
        self.assertIn("vps-deploy-deprecated", self._kinds(brief))
        # The warning is the operational signal — the field stays None.
        self.assertIsNone(brief.deploy_target)

    def test_deploy_target_vps_with_vps_deploy_no_warns_mismatch(self) -> None:
        brief, fm = self._parse(
            vps_deploy="no", deploy_target_line="deploy_target: vps")
        validate_or_reject(brief, fm)  # warning, not error
        self.assertIn("deploy-target-mismatch", self._kinds(brief))
        self.assertEqual(brief.deploy_target, "vps")

    def test_consistent_pairs_emit_no_coexistence_warning(self) -> None:
        # vercel + vps_deploy:no is consistent (not deploying to vps); no warning.
        brief, _ = self._parse(
            vps_deploy="no", deploy_target_line="deploy_target: vercel")
        kinds = self._kinds(brief)
        self.assertNotIn("vps-deploy-deprecated", kinds)
        self.assertNotIn("deploy-target-mismatch", kinds)

    # --- four-axis coexistence ----------------------------------------------
    def test_model_issues_sentry_deploy_target_coexist(self) -> None:
        brief, fm = self._parse(
            vps_deploy="no",
            deploy_target_line="deploy_target: vercel",
            step_extra=(
                "- **model:** haiku\n"
                "- **issues:** write\n"
                "- **sentry:** read"
            ),
        )
        self.assertEqual(brief.deploy_target, "vercel")
        step = brief.steps[0]
        self.assertEqual(step.model, "haiku")
        self.assertEqual(step.issues, "write")
        self.assertEqual(step.sentry, "read")
        validate_or_reject(brief, fm)  # all four axes validate independently


class TestRule2Relaxation(unittest.TestCase):
    """v4 Phase 2b Step 1: rule 2 relaxed from {1} to {1, 2}. v1 and v2 both
    validate; v3+ rejects with the "1 or 2" message; absent rejects via rule 1.
    (Q-E3-F1: the version-rejection path moved here from the invalid-brief.md
    omnibus fixture, which now carries a valid brief_version: 2.)"""

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-rule2-"))
        _git_init(self._repo)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-rule2-brief-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _parse(self, *, version_line: str = "brief_version: 1"):
        body = f"""---
{version_line}
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {self._repo}
vps_deploy: no
---

## Goal

Exercise rule 2.

## Steps

### Step 1 — A step

- **scope.files:**
- **scope.operations:** read
- **smoke:** `echo hi`
- **confirm:** explicit
"""
        p = self._tmpdir / "brief.md"
        p.write_text(body, encoding="utf-8")
        return parse_brief_raw(p)

    def test_brief_version_1_valid(self) -> None:
        brief, fm = self._parse(version_line="brief_version: 1")
        self.assertEqual(brief.brief_version, 1)
        validate_or_reject(brief, fm)  # no raise

    def test_brief_version_2_valid_no_observe(self) -> None:
        brief, fm = self._parse(version_line="brief_version: 2")
        self.assertEqual(brief.brief_version, 2)
        validate_or_reject(brief, fm)  # no raise — v2 accepted, observe optional

    def test_brief_version_3_rejected(self) -> None:
        brief, fm = self._parse(version_line="brief_version: 3")
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief, fm)
        self.assertIn("brief_version must be 1 or 2 (got 3)",
                      " ".join(ctx.exception.errors))

    def test_brief_version_absent_rejected(self) -> None:
        # No brief_version key → rule 1 (presence) fires; rule 2 also fires on the
        # parser default 0. Either way the brief is rejected, version flagged.
        brief, fm = self._parse(version_line="project_placeholder: x")
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief, fm)
        self.assertIn("brief_version", " ".join(ctx.exception.errors))


class TestStepObserveField(unittest.TestCase):
    """v4 Phase 2b Step 1 (DC5/DC6, BAF-1/BAF-2): per-step observe: block —
    flat dot-notation parse (observe.target / observe.surfaces), rule 17
    well-formedness (target required when present; surfaces ⊆ OBSERVE_SURFACES;
    empty surfaces valid; duplicates allowed; ordering preserved), and the
    any-field-present detection (surfaces-without-target is a caught error, not
    a silent drop). All on brief_version: 2 (version-gate tested separately)."""

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-observe-"))
        _git_init(self._repo)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-observe-brief-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _parse(self, *, version: int = 2, observe_lines: str = ""):
        body = f"""---
brief_version: {version}
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {self._repo}
vps_deploy: no
---

## Goal

Exercise the per-step observe: block.

## Steps

### Step 1 — A step

- **scope.files:**
- **scope.operations:** read
- **smoke:** `echo hi`
- **confirm:** explicit
{observe_lines}
"""
        p = self._tmpdir / "brief.md"
        p.write_text(body, encoding="utf-8")
        return parse_brief_raw(p)

    def test_observe_target_only(self) -> None:
        brief, fm = self._parse(
            observe_lines="- **observe.target:** http://localhost:3000")
        self.assertEqual(brief.steps[0].observe,
                         {"target": "http://localhost:3000", "surfaces": []})
        validate_or_reject(brief, fm)  # no raise

    def test_observe_target_and_surfaces(self) -> None:
        brief, fm = self._parse(observe_lines=(
            "- **observe.target:** http://localhost:3000\n"
            "- **observe.surfaces:** dom, console"))
        self.assertEqual(
            brief.steps[0].observe,
            {"target": "http://localhost:3000", "surfaces": ["dom", "console"]})
        validate_or_reject(brief, fm)  # no raise

    def test_observe_surfaces_only_rule17_target_required(self) -> None:
        # BAF-2: surfaces present but target absent → observe assembled with
        # target=None → rule 17 fires "requires a target" (loud, not dropped).
        brief, fm = self._parse(observe_lines="- **observe.surfaces:** dom")
        self.assertEqual(brief.steps[0].observe,
                         {"target": None, "surfaces": ["dom"]})
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief, fm)
        self.assertIn("observe: requires a target", " ".join(ctx.exception.errors))

    def test_observe_absent_is_none(self) -> None:
        brief, fm = self._parse(observe_lines="")
        self.assertIsNone(brief.steps[0].observe)
        validate_or_reject(brief, fm)  # no raise

    def test_observe_target_defensive_normalization(self) -> None:
        # null/none/"" alone (no surfaces) → observe None (the model:/issues:/
        # sentry: none→absent precedent); validates clean (no observe block).
        for line in ("- **observe.target:** null",
                     "- **observe.target:** none",
                     "- **observe.target:** "):
            with self.subTest(line=line):
                brief, fm = self._parse(observe_lines=line)
                self.assertIsNone(brief.steps[0].observe)
                validate_or_reject(brief, fm)  # no raise

    def test_observe_surfaces_empty_valid(self) -> None:
        # target present, surfaces line empty → surfaces=[], rule 17 passes.
        brief, fm = self._parse(observe_lines=(
            "- **observe.target:** http://localhost\n"
            "- **observe.surfaces:** "))
        self.assertEqual(brief.steps[0].observe,
                         {"target": "http://localhost", "surfaces": []})
        validate_or_reject(brief, fm)  # no raise

    def test_observe_surfaces_duplicates_allowed(self) -> None:
        brief, fm = self._parse(observe_lines=(
            "- **observe.target:** x\n"
            "- **observe.surfaces:** dom, dom, console"))
        self.assertEqual(brief.steps[0].observe["surfaces"],
                         ["dom", "dom", "console"])
        validate_or_reject(brief, fm)  # no raise — duplicates harmless

    def test_observe_surfaces_invalid_value_rejected(self) -> None:
        for bad in ("screenshot", "performance", "video"):
            with self.subTest(value=bad):
                brief, fm = self._parse(observe_lines=(
                    f"- **observe.target:** x\n"
                    f"- **observe.surfaces:** dom, {bad}"))
                with self.assertRaises(BriefValidationError) as ctx:
                    validate_or_reject(brief, fm)
                joined = " ".join(ctx.exception.errors)
                self.assertIn("observe.surfaces contains invalid values", joined)
                self.assertIn(bad, joined)

    def test_observe_surfaces_ordering_preserved(self) -> None:
        brief, fm = self._parse(observe_lines=(
            "- **observe.target:** x\n"
            "- **observe.surfaces:** console, dom, network"))
        self.assertEqual(brief.steps[0].observe["surfaces"],
                         ["console", "dom", "network"])  # not sorted
        validate_or_reject(brief, fm)  # no raise


class TestRule17VersionGate(unittest.TestCase):
    """v4 Phase 2b Step 1 (DC6): observe: is version-gated to brief_version: 2.
    An observe: block on a v1 brief is a validation error (not-safely-ignorable);
    a v2 brief accepts a well-formed observe: block; a v2 brief with no observe:
    is valid (v2 does not require observe:, only observe: requires v2)."""

    def setUp(self) -> None:
        self._repo = Path(tempfile.mkdtemp(prefix="anvil-test-r17-"))
        _git_init(self._repo)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="anvil-test-r17-brief-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._repo, ignore_errors=True)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _parse(self, *, version: int, observe_lines: str = ""):
        body = f"""---
brief_version: {version}
project: anvil
build_name: test
target_repo: github.com/test/test
target_repo_path: {self._repo}
vps_deploy: no
---

## Goal

Exercise rule 17 version-gating.

## Steps

### Step 1 — A step

- **scope.files:**
- **scope.operations:** read
- **smoke:** `echo hi`
- **confirm:** explicit
{observe_lines}
"""
        p = self._tmpdir / "brief.md"
        p.write_text(body, encoding="utf-8")
        return parse_brief_raw(p)

    def test_observe_on_v1_brief_rejects(self) -> None:
        brief, fm = self._parse(version=1, observe_lines=(
            "- **observe.target:** http://localhost\n"
            "- **observe.surfaces:** dom"))
        # the block parses (it's well-formed), but rule 17 version-gates it
        self.assertIsNotNone(brief.steps[0].observe)
        with self.assertRaises(BriefValidationError) as ctx:
            validate_or_reject(brief, fm)
        self.assertIn("observe: requires brief_version: 2",
                      " ".join(ctx.exception.errors))

    def test_observe_on_v2_brief_accepts(self) -> None:
        brief, fm = self._parse(version=2, observe_lines=(
            "- **observe.target:** http://localhost\n"
            "- **observe.surfaces:** dom, console, network"))
        validate_or_reject(brief, fm)  # no raise

    def test_v2_brief_without_observe_valid(self) -> None:
        brief, fm = self._parse(version=2, observe_lines="")
        self.assertIsNone(brief.steps[0].observe)
        validate_or_reject(brief, fm)  # no raise — v2 does not require observe:
