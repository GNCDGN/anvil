#!/usr/bin/env python3
"""Phase 3 Step 1 patch — brief schema gains vps_target_path; validation rule 4 extends.

Idempotent: detects whether the patch has already been applied (by checking for
'vps_target_path' in brief.py) and exits cleanly if so. Leaves .bak files for
the two modified files.

Applies three edits to anvil/brief.py:
  1. Brief model gains `vps_target_path: str | None = None` (after service_name)
  2. parse_brief_raw populates vps_target_path from frontmatter
  3. validate_or_reject rule 4 extends to require vps_target_path when vps_deploy=yes

Appends one test class to tests/test_brief.py covering the five cases from the
Phase 3 brief Step 1 notes.

Run from ~/Downloads/anvil:
    .venv/bin/python apply_step1_patch.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
BRIEF_PY = REPO / "anvil" / "brief.py"
TEST_BRIEF_PY = REPO / "tests" / "test_brief.py"


def fail(msg: str) -> None:
    print(f"[step1-patch] FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[step1-patch] {msg}")


def _backup(p: Path) -> None:
    bak = p.with_suffix(p.suffix + ".pre-phase-3-step-1.bak")
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


def patch_brief_py() -> bool:
    """Apply the three edits to brief.py. Returns True if anything changed."""
    text = BRIEF_PY.read_text()
    if "vps_target_path" in text:
        info("brief.py already has vps_target_path — skipping")
        return False

    _backup(BRIEF_PY)

    # Edit 1: add vps_target_path field after service_name
    old_field = "    vps_deploy: Literal[\"yes\", \"no\"]\n    service_name: str | None = None\n    goal: str = \"\"\n"
    new_field = "    vps_deploy: Literal[\"yes\", \"no\"]\n    service_name: str | None = None\n    # Phase 3 Step 1: VPS-side path for deploy chain (cd here, then git pull --ff-only)\n    vps_target_path: str | None = None\n    goal: str = \"\"\n"
    text = _apply_unique(text, old_field, new_field, "edit 1: Brief.vps_target_path field")

    # Edit 2: parse_brief_raw populates from frontmatter
    old_parse = "        vps_deploy=_norm_yes_no(fm.get(\"vps_deploy\", \"no\")),\n        service_name=(\n            str(fm[\"service_name\"]) if fm.get(\"service_name\") else None\n        ),\n"
    new_parse = "        vps_deploy=_norm_yes_no(fm.get(\"vps_deploy\", \"no\")),\n        service_name=(\n            str(fm[\"service_name\"]) if fm.get(\"service_name\") else None\n        ),\n        vps_target_path=(\n            str(fm[\"vps_target_path\"]) if fm.get(\"vps_target_path\") else None\n        ),\n"
    text = _apply_unique(text, old_parse, new_parse, "edit 2: parse_brief_raw vps_target_path")

    # Edit 3: validate_or_reject rule 4 extends
    old_rule = "    # 4. vps_deploy: yes requires service_name\n    if brief.vps_deploy == \"yes\" and not brief.service_name:\n        e.append(\"vps_deploy is 'yes' but service_name is not set\")\n"
    new_rule = "    # 4. vps_deploy: yes requires service_name AND vps_target_path (Phase 3 Step 1)\n    if brief.vps_deploy == \"yes\" and not brief.service_name:\n        e.append(\"vps_deploy is 'yes' but service_name is not set\")\n    if brief.vps_deploy == \"yes\" and not brief.vps_target_path:\n        e.append(\"vps_deploy is 'yes' but vps_target_path is not set\")\n"
    text = _apply_unique(text, old_rule, new_rule, "edit 3: validate_or_reject rule 4")

    BRIEF_PY.write_text(text)
    info("patched brief.py")
    return True


def patch_test_brief_py() -> bool:
    """Append the Phase 3 Step 1 test class. Returns True if anything changed."""
    text = TEST_BRIEF_PY.read_text()
    if "TestPhase3VpsTargetPath" in text:
        info("test_brief.py already has TestPhase3VpsTargetPath — skipping")
        return False

    _backup(TEST_BRIEF_PY)

    addition = '''


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
'''

    TEST_BRIEF_PY.write_text(text + addition)
    info("patched test_brief.py (appended TestPhase3VpsTargetPath)")
    return True


def main() -> int:
    if not BRIEF_PY.exists():
        fail(f"brief.py not found at {BRIEF_PY}")
    if not TEST_BRIEF_PY.exists():
        fail(f"test_brief.py not found at {TEST_BRIEF_PY}")

    changed_brief = patch_brief_py()
    changed_tests = patch_test_brief_py()

    if not (changed_brief or changed_tests):
        info("nothing to do — patch already fully applied")
        return 0

    # Compile-check brief.py before declaring success
    import py_compile
    try:
        py_compile.compile(str(BRIEF_PY), doraise=True)
        py_compile.compile(str(TEST_BRIEF_PY), doraise=True)
        info("compile-check passed")
    except py_compile.PyCompileError as e:
        fail(f"compile-check failed: {e}")

    info("Step 1 patch applied. Next: run smoke")
    info("  .venv/bin/python -m unittest tests.test_brief -v")
    return 0


if __name__ == "__main__":
    sys.exit(main())
