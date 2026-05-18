"""Phase 2 Step 8 — Coder subprocess wrapper tests.

Mocked `subprocess.run` at the call-site level (anvil.coder.subprocess.run)
so no real `claude --print` invocation happens. Hermetic /tmp git repos
back the Layer 2 git-diff verification. Six functional groupings per
the brief:

  (a) clean run — exit 0, files_touched matches plan.files_to_touch,
      no out_of_scope, no reconciliations → clean dict returned
  (b) out-of-scope edit — files_touched includes a file not in plan →
      out_of_scope populated
  (c) timeout — subprocess.TimeoutExpired raised → exit_code: -1 and
      timeout stderr captured
  (d) path reconciliation — one match resolves; zero matches escalate;
      multiple matches escalate
  (e) operation mapping — write produces Edit/Write/MultiEdit tools;
      smoke-test does NOT appear in allow-list; commit does not appear;
      shell produces Bash
  (f) duration_s is captured around the subprocess call

A seventh covers parse_anvil_coder_blocks (the [anvil-coder] block
extractor) because the helper is the orchestrator's parsing contract.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anvil import coder
from anvil.coder import Coder, parse_anvil_coder_blocks

# Bind the real subprocess.run BEFORE any mock.patch fires. mock.patch.object
# on `coder.subprocess.run` actually patches `subprocess.run` globally (the
# module reference is the same object), so `fake_run` cannot delegate via
# `subprocess.run(...)` without recursing into itself. _real_run is captured
# at import time when no patches are active; it remains a direct reference
# to the original callable for the lifetime of this test module.
_real_run = subprocess.run


def _init_repo(repo: Path, files: dict[str, str] | None = None) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   check=True, capture_output=True)
    for name, content in (files or {".keep": ""}).items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   capture_output=True)


def _make_plan(**over):
    base = dict(
        step_number=1,
        step_name="Test step",
        files_to_touch=["a.py"],
        operations=["write"],
        approach="do it",
        expected_outcome="ok",
        escalation_triggers=[],
    )
    base.update(over)
    return SimpleNamespace(**base, model_dump=lambda: dict(base))


def _make_brief(target_repo_path: Path):
    return SimpleNamespace(target_repo_path=target_repo_path)


def _make_coder(claude_binary: Path = Path("/usr/bin/claude"),
                timeout: int = 600):
    return Coder(
        claude_binary=claude_binary,
        timeout=timeout,
        system_prompt="You are the coder.",
    )


def _proc_result(returncode=0, stdout="ok\n", stderr=""):
    """Fake subprocess.CompletedProcess for mocking subprocess.run."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class CoderCleanRunTests(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-coder-"))
        self.repo = self._tmp / "repo"
        _init_repo(self.repo, {"a.py": "# original\n"})

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # --- (a) clean run ---
    def test_a_clean_run_returns_clean_dict(self):
        # Simulate the model editing a.py: modify it on disk before the
        # mocked subprocess "returns", so git diff sees the change.
        # The mock side-effect performs the edit to keep the test honest
        # to the Layer 2 contract.
        def fake_run(cmd, **kw):
            if cmd[0:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            # The claude --print invocation
            (self.repo / "a.py").write_text("# edited\n", encoding="utf-8")
            return _proc_result(0, "Done.", "")

        plan = _make_plan(files_to_touch=["a.py"], operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run",
                                side_effect=fake_run):
            result = _make_coder().execute_step(plan, brief)

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["files_touched"], ["a.py"])
        self.assertEqual(result["out_of_scope"], [])
        self.assertEqual(result["reconciliations"], [])
        self.assertIn("Edit", result["allowed_tools"])
        self.assertIn("Write", result["allowed_tools"])

    # --- (b) out-of-scope edit ---
    def test_b_out_of_scope_edit_populates_out_of_scope(self):
        def fake_run(cmd, **kw):
            if cmd[0:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            # The model edits a.py (in scope) AND b.py (not in scope).
            (self.repo / "a.py").write_text("# edited\n", encoding="utf-8")
            (self.repo / "b.py").write_text("# new\n", encoding="utf-8")
            return _proc_result(0, "Done.", "")

        plan = _make_plan(files_to_touch=["a.py"], operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run",
                                side_effect=fake_run):
            result = _make_coder().execute_step(plan, brief)

        self.assertEqual(result["exit_code"], 0)
        self.assertIn("b.py", result["files_touched"])
        self.assertEqual(result["out_of_scope"], ["b.py"])

    # --- (c) timeout ---
    def test_c_timeout_returns_exit_minus_one_with_timeout_stderr(self):
        def fake_run(cmd, **kw):
            if cmd[0:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=600)

        plan = _make_plan(files_to_touch=["a.py"], operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run",
                                side_effect=fake_run):
            result = _make_coder().execute_step(plan, brief)

        self.assertEqual(result["exit_code"], -1)
        self.assertIn("coder-timeout", result["stderr"])
        # Duration is still captured even on timeout
        self.assertGreaterEqual(result["duration_s"], 0)

    # --- (f) duration_s captured ---
    def test_f_duration_s_captured_around_subprocess(self):
        def fake_run(cmd, **kw):
            if cmd[0:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            time.sleep(0.05)
            (self.repo / "a.py").write_text("# edited\n", encoding="utf-8")
            return _proc_result(0, "Done.", "")

        plan = _make_plan(files_to_touch=["a.py"], operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run",
                                side_effect=fake_run):
            result = _make_coder().execute_step(plan, brief)

        self.assertGreaterEqual(result["duration_s"], 0.05)


class CoderPathReconciliationTests(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-coder-rec-"))
        self.repo = self._tmp / "repo"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # --- (d-1) one match → reconciles ---
    def test_d1_single_basename_match_resolves(self):
        # Plan says reporter/chat_handler.py; on disk it's chat_handler.py
        _init_repo(self.repo, {"chat_handler.py": "# real\n"})

        def fake_run(cmd, **kw):
            if cmd[0:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            (self.repo / "chat_handler.py").write_text("# edited\n",
                                                       encoding="utf-8")
            return _proc_result(0, "Done.", "")

        plan = _make_plan(files_to_touch=["reporter/chat_handler.py"],
                          operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run",
                                side_effect=fake_run):
            result = _make_coder().execute_step(plan, brief)

        # Reconciliation should have fired, single match resolved
        self.assertEqual(len(result["reconciliations"]), 1)
        self.assertEqual(result["reconciliations"][0]["status"], "resolved")
        self.assertEqual(
            result["reconciliations"][0]["resolved"], "chat_handler.py",
        )
        # The Coder's edit to chat_handler.py is in scope because
        # reconciliation made it so
        self.assertEqual(result["out_of_scope"], [])

    # --- (d-2) zero matches → escalation block ---
    def test_d2_zero_matches_escalation_block(self):
        _init_repo(self.repo, {".keep": ""})  # empty repo
        plan = _make_plan(files_to_touch=["totally_missing.py"],
                          operations=["write"])
        brief = _make_brief(self.repo)
        # subprocess.run is not invoked because reconciliation pre-fails
        with mock.patch.object(coder.subprocess, "run") as run_mock:
            result = _make_coder().execute_step(plan, brief)
        # No claude --print call should have been made; only git
        # subprocess calls would happen, and pre-flight fails before
        # those for path reconciliation
        self.assertTrue(result.get("escalate"))
        self.assertEqual(result["reason"], "coder-path-reconciliation-failed")
        # The mock should not have been called with a claude binary
        for call in run_mock.call_args_list:
            cmd = call.args[0] if call.args else call.kwargs.get("args", [])
            self.assertNotIn("claude", " ".join(cmd))

    # --- (d-3) multiple matches → escalation block ---
    def test_d3_multiple_matches_escalation_block(self):
        _init_repo(self.repo, {
            "a/ambiguous.py": "# a\n",
            "b/ambiguous.py": "# b\n",
        })
        plan = _make_plan(files_to_touch=["c/ambiguous.py"],
                          operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run"):
            result = _make_coder().execute_step(plan, brief)
        self.assertTrue(result.get("escalate"))
        self.assertEqual(result["reason"], "coder-path-reconciliation-failed")


class CoderOperationMappingTests(unittest.TestCase):
    """Test (e): operation-to-tool mapping."""

    def test_write_produces_edit_write_tools(self):
        allow, deny = coder._operations_to_denylist(["write"])
        self.assertIn("Edit", allow)
        self.assertIn("Write", allow)
        # Bash is not in allow for a write-only plan
        self.assertNotIn("Bash", allow)
        # Bash is therefore in the deny-list
        self.assertIn("Bash", deny)

    def test_smoke_test_does_not_appear_in_allow(self):
        # smoke-test is silently dropped — orchestrator owns smokes
        allow, deny = coder._operations_to_denylist(["smoke-test"])
        self.assertEqual(allow, [])
        # Bash + Edit + Write are all denied
        self.assertIn("Bash", deny)
        self.assertIn("Edit", deny)
        self.assertIn("Write", deny)

    def test_commit_does_not_appear_in_allow(self):
        # commit is silently dropped — orchestrator owns commits
        allow, deny = coder._operations_to_denylist(["commit"])
        self.assertEqual(allow, [])

    def test_shell_produces_bash(self):
        allow, deny = coder._operations_to_denylist(["shell"])
        self.assertIn("Bash", allow)
        self.assertNotIn("Bash", deny)

    def test_read_only_allows_read_tools(self):
        allow, deny = coder._operations_to_denylist(["read"])
        self.assertIn("Read", allow)
        self.assertIn("Glob", allow)
        self.assertIn("Grep", allow)
        # No mutating tools — they're denied
        self.assertIn("Edit", deny)
        self.assertIn("Write", deny)


class CoderAnvilBlockParserTests(unittest.TestCase):
    """parse_anvil_coder_blocks contract."""

    def test_empty_input_returns_empty(self):
        self.assertEqual(parse_anvil_coder_blocks(""), [])

    def test_no_blocks_returns_empty(self):
        self.assertEqual(
            parse_anvil_coder_blocks("Done.\nNothing notable here.\n"), [],
        )

    def test_single_block(self):
        text = (
            "Done editing.\n\n"
            "[anvil-coder] reconciled reporter/x.py -> x.py at execute time.\n"
        )
        blocks = parse_anvil_coder_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn("reconciled", blocks[0])

    def test_multi_line_block_ends_at_blank_line(self):
        text = (
            "[anvil-coder] partial completion:\n"
            "- edited a.py cleanly\n"
            "- could not edit b.py (function signature differs)\n"
            "\n"
            "Done.\n"
        )
        blocks = parse_anvil_coder_blocks(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn("could not edit b.py", blocks[0])
        self.assertNotIn("Done.", blocks[0])

    def test_two_blocks_separated_by_blank_line(self):
        text = (
            "[anvil-coder] first observation\n"
            "\n"
            "[anvil-coder] second observation\n"
        )
        blocks = parse_anvil_coder_blocks(text)
        self.assertEqual(len(blocks), 2)


if __name__ == "__main__":
    unittest.main()
