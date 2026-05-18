"""Phase 2 Step 6 — brief parse-time path warning tests.

Six cases per the brief:
  (a) scope.files paths that exist → no warnings
  (b) scope.files paths that don't exist but are write targets → no warnings
  (c) scope.files paths that don't exist and aren't write targets → warning
       with closest match if one exists
  (d) brief with non-existent path and multiple close matches → warning
       with closest_match: None
  (e) warnings appear in brief.parse_warnings
  (f) warnings emit to stderr AND the logging system

Hermetic: each test builds a tmp git repo + a tmp brief markdown.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from anvil.brief import parse_brief


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True,
                   capture_output=True)
    (repo / ".keep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   capture_output=True)


def _write_brief(brief_path: Path, target_repo: Path,
                 step_files: list[str],
                 step_operations: list[str] = ("write", "commit")) -> None:
    """Write a one-step brief targeting target_repo. step_files / step_operations
    populate scope.files / scope.operations."""
    files_csv = ", ".join(step_files)
    ops_csv = ", ".join(step_operations)
    brief_path.write_text(textwrap.dedent(f"""\
        ---
        brief_version: 1
        project: test
        build_name: Test
        target_repo: t
        target_repo_path: {target_repo}
        vps_deploy: 'no'
        ---

        ## Goal

        Just for testing.

        ## Context

        ## Steps

        ### Step 1 — Example step
        - **scope.files:** {files_csv}
        - **scope.operations:** {ops_csv}
        - **smoke:** echo ok
        - **confirm:** explicit
        - **notes:** test
    """), encoding="utf-8")


class BriefParseWarningsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-d18-"))
        self.repo = self._tmp / "repo"
        self.repo.mkdir()
        _init_git(self.repo)
        self.brief_path = self._tmp / "brief.md"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # --- (a) all scope.files exist → no warnings ---
    def test_a_existing_paths_produce_no_warnings(self):
        (self.repo / "real.py").write_text("# real\n", encoding="utf-8")
        _write_brief(self.brief_path, self.repo, ["real.py"])
        # Capture stderr so the "no warnings" assertion has its own clean
        # baseline (and silences any unrelated noise from parse_brief).
        buf = io.StringIO()
        with redirect_stderr(buf):
            brief = parse_brief(self.brief_path)
        self.assertEqual(brief.parse_warnings, [])

    # --- (b) missing path that IS a write target → no warning ---
    def test_b_write_target_missing_is_not_warned(self):
        # The brief declares scope.operations=['write', 'commit'] for the
        # step, and the file doesn't exist on disk — exactly the "file the
        # build creates" case. No warning expected.
        _write_brief(self.brief_path, self.repo, ["new_file.py"])
        with redirect_stderr(io.StringIO()):
            brief = parse_brief(self.brief_path)
        self.assertEqual(brief.parse_warnings, [])

    # --- (c) missing path that is NOT a write target → warning with closest match ---
    def test_c_missing_read_only_path_warns_with_closest_match(self):
        # Step declares 'read' only (not 'write'); the path doesn't exist
        # but a similarly-named file lives elsewhere in the repo.
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "thing.py").write_text("# thing\n",
                                                     encoding="utf-8")
        _write_brief(
            self.brief_path, self.repo,
            ["other/thing.py"], step_operations=["read", "smoke-test"],
        )
        with redirect_stderr(io.StringIO()):
            brief = parse_brief(self.brief_path)
        self.assertEqual(len(brief.parse_warnings), 1)
        w = brief.parse_warnings[0]
        self.assertEqual(w["kind"], "path-not-found")
        self.assertEqual(w["step_number"], 1)
        self.assertEqual(w["path"], "other/thing.py")
        self.assertEqual(w["closest_match"], "sub/thing.py")

    # --- (d) missing path with multiple close matches → closest_match=None ---
    def test_d_multiple_matches_yield_closest_match_none(self):
        (self.repo / "a").mkdir()
        (self.repo / "b").mkdir()
        (self.repo / "a" / "ambiguous.py").write_text("# a\n",
                                                       encoding="utf-8")
        (self.repo / "b" / "ambiguous.py").write_text("# b\n",
                                                       encoding="utf-8")
        _write_brief(
            self.brief_path, self.repo,
            ["c/ambiguous.py"], step_operations=["read", "smoke-test"],
        )
        with redirect_stderr(io.StringIO()):
            brief = parse_brief(self.brief_path)
        self.assertEqual(len(brief.parse_warnings), 1)
        self.assertIsNone(brief.parse_warnings[0]["closest_match"])

    # --- (e) warnings appear in brief.parse_warnings ---
    def test_e_warnings_attached_to_brief_object(self):
        _write_brief(
            self.brief_path, self.repo,
            ["missing.py"], step_operations=["read", "smoke-test"],
        )
        with redirect_stderr(io.StringIO()):
            brief = parse_brief(self.brief_path)
        # The field exists, is the right type, and is populated.
        self.assertIsInstance(brief.parse_warnings, list)
        self.assertGreaterEqual(len(brief.parse_warnings), 1)

    # --- (f) warnings emit to BOTH stderr and the anvil logger ---
    def test_f_warnings_emit_to_stderr_and_logger(self):
        _write_brief(
            self.brief_path, self.repo,
            ["missing.py"], step_operations=["read", "smoke-test"],
        )
        buf = io.StringIO()
        with redirect_stderr(buf), self.assertLogs(
            "anvil.brief", level="WARNING"
        ) as captured:
            parse_brief(self.brief_path)
        # stderr
        err = buf.getvalue()
        self.assertIn("[brief-warning]", err)
        self.assertIn("missing.py", err)
        # logger
        joined = "\n".join(captured.output)
        self.assertIn("[brief-warning]", joined)
        self.assertIn("missing.py", joined)


if __name__ == "__main__":
    unittest.main()
