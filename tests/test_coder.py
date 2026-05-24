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

import json
import os
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
    # v2 Phase 2 Step 4: operations changed from ["write"] to ["read"].
    # The write carve-out (V2P2-4) now treats an unresolved path as a
    # 'new-file' (not 'failed') whenever "write" is declared, so this
    # escalation path is only reachable for non-write operations. The
    # write-new behaviour is covered by ReconcileWriteNewTests below.
    def test_d2_zero_matches_escalation_block(self):
        _init_repo(self.repo, {".keep": ""})  # empty repo
        plan = _make_plan(files_to_touch=["totally_missing.py"],
                          operations=["read"])
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
    # v2 Phase 2 Step 4: operations changed from ["write"] to ["read"]
    # for the same reason as d-2 — with "write" declared, an ambiguous
    # (multiple-basename-match) path now falls through to 'new-file'
    # rather than escalating. Ambiguity still escalates for non-write.
    def test_d3_multiple_matches_escalation_block(self):
        _init_repo(self.repo, {
            "a/ambiguous.py": "# a\n",
            "b/ambiguous.py": "# b\n",
        })
        plan = _make_plan(files_to_touch=["c/ambiguous.py"],
                          operations=["read"])
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


# ---------------------------------------------------------------------------
# v2 Phase 2 Step 4 — _reconcile_paths write-new carve-out (V2P2-4)
# ---------------------------------------------------------------------------

class ReconcileWriteNewTests(unittest.TestCase):
    """The write-new fall-through: an unresolved path (no existing file,
    no single basename match) is recorded as 'new-file' (the Coder
    creates it) when the plan declares `write`, and as 'failed' (preflight
    escalation) when it does not. Tests _reconcile_paths at the function
    level — no execute_step, no subprocess."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-reconcile-"))
        self.repo = self._tmp / "repo"
        # No git needed — _reconcile_paths only stats files and walks
        # for basename matches. An empty dir is a strictly-new repo.
        self.repo.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # --- (7a) strictly-new path WITH write → status 'new-file' ---
    def test_new_path_with_write_is_new_file(self):
        resolved, recs = coder._reconcile_paths(
            ["anvil/utils/hello.py"], self.repo, ["write", "smoke-test"]
        )
        # Path returned unchanged — the Coder creates it.
        self.assertEqual(resolved, ["anvil/utils/hello.py"])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["status"], "new-file")
        self.assertEqual(recs[0]["original"], "anvil/utils/hello.py")
        self.assertIsNone(recs[0]["resolved"])
        self.assertIn("write operation declared", recs[0]["reason"])

    # --- (7b) strictly-new path WITHOUT write → status 'failed' ---
    def test_new_path_without_write_is_failed(self):
        resolved, recs = coder._reconcile_paths(
            ["anvil/utils/hello.py"], self.repo, ["read"]
        )
        # Existing behaviour: original is still returned, but the
        # reconciliation is a failure that the caller escalates on.
        self.assertEqual(resolved, ["anvil/utils/hello.py"])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["status"], "failed")
        self.assertIsNone(recs[0]["resolved"])
        self.assertIn("no single basename match", recs[0]["reason"])

    def test_empty_operations_treated_as_no_write(self):
        # Defensive: an empty/None operations list must not be read as
        # write-permitted. Unresolved path → 'failed'.
        resolved, recs = coder._reconcile_paths(
            ["anvil/utils/hello.py"], self.repo, []
        )
        self.assertEqual(recs[0]["status"], "failed")


# ---------------------------------------------------------------------------
# v2 Phase 2 Step 4 — T6 write-new calibration brief + mocked fixture
# ---------------------------------------------------------------------------

_T6_BRIEF = (
    Path.home()
    / "vaults" / "second-brain" / "01-Projects" / "code-workspace"
    / "anvil" / "builds" / "2026-05-20-anvil-v2-phase-1-calibration"
    / "T6-write-new" / "brief.md"
)
_T6_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "v2-phase-1" / "mocked-plans" / "T6-step0.json"
)


class T6CalibrationBriefTests(unittest.TestCase):
    """The T6 write-new calibration brief parses + validates, and its
    MockedPlanner step-0 fixture is a valid Plan. These guard the
    Step-5 sweep inputs: a malformed brief or fixture would only
    surface mid-sweep otherwise."""

    # --- (7c) T6 brief passes validate_or_reject ---
    def test_t6_brief_validates(self):
        from anvil.brief import parse_brief_raw, validate_or_reject
        from tools import calibration_runner
        self.assertTrue(_T6_BRIEF.is_file(), f"T6 brief missing: {_T6_BRIEF}")
        # v2 Phase 2 Step 4 follow-up: target_repo_path is the isolated
        # throwaway state/calibration/targets/T6 (renamed from
        # v2-phase-1/targets/ in v2 Phase 5 Step 1b), which only exists after
        # bootstrap (matches T1-T5 — validate_or_reject rule 3 requires
        # the target to exist + be a git repo). Bootstrap first, exactly
        # as test_calibration_runner.test_all_six_briefs_parse does via
        # parse_brief_only.
        calibration_runner.bootstrap_target_repo("T6")
        brief, raw = parse_brief_raw(_T6_BRIEF)
        # One write+smoke-test step touching the new utils file.
        self.assertEqual(len(brief.steps), 1)
        self.assertEqual(brief.steps[0].scope_files, ["anvil/utils/hello.py"])
        self.assertIn("write", brief.steps[0].scope_operations)
        # Should not raise.
        validate_or_reject(brief, raw_frontmatter=raw)

    # --- (7d) T6 MockedPlanner fixture is a valid Plan ---
    def test_t6_fixture_is_valid_plan(self):
        from anvil.planner import Plan
        self.assertTrue(_T6_FIXTURE.is_file(),
                        f"T6 fixture missing: {_T6_FIXTURE}")
        data = json.loads(_T6_FIXTURE.read_text(encoding="utf-8"))
        # Constructs without ValidationError → satisfies all eleven
        # _REQUIRED_PLAN_FIELDS and the six structural constraints
        # (confidence enum, scope_boundaries shape, etc.).
        plan = Plan(**data)
        self.assertEqual(plan.step_number, 1)
        self.assertEqual(plan.files_to_touch, ["anvil/utils/hello.py"])
        self.assertEqual(plan.operations, ["write", "smoke-test"])
        self.assertEqual(plan.confidence, "high")


class CoderInstrumentationTests(unittest.TestCase):
    """v2 Phase 5 Step 1a: --output-format json envelope parse + Coder cost
    instrumentation. The model text (`result`) is extracted back into the
    `stdout` the result dict exposes (downstream contract preserved); usage
    + total_cost_usd ride the coder.subprocess.end event. Non-JSON output
    (mock mode / errors) falls back to raw text with no cost."""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-coder-instr-"))
        self.repo = self._tmp / "repo"
        _init_repo(self.repo, {"a.py": "# original\n"})

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_with_stdout(self, subprocess_stdout: str):
        """Run execute_step with the claude subprocess returning the given
        stdout; capture the result dict + the coder.subprocess.end event
        payload (via a patched events.emit)."""
        emitted: list[tuple] = []

        def fake_run(cmd, **kw):
            if cmd[0:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            (self.repo / "a.py").write_text("# edited\n", encoding="utf-8")
            return _proc_result(0, subprocess_stdout, "")

        def fake_emit(kind, data, **kw):
            emitted.append((kind, data))

        plan = _make_plan(files_to_touch=["a.py"], operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(coder._events, "emit", side_effect=fake_emit):
            result = _make_coder().execute_step(plan, brief)
        end = next(d for k, d in emitted if k == "coder.subprocess.end")
        return result, end

    def test_coder_subprocess_json_envelope_parsed(self):
        envelope = json.dumps({
            "result": "Done.\n[anvil-coder] edited a.py",
            "usage": {"input_tokens": 2152, "output_tokens": 4,
                      "cache_creation_input_tokens": 5283,
                      "cache_read_input_tokens": 10315},
            "total_cost_usd": 0.04953225,
        })
        result, end = self._run_with_stdout(envelope)
        # `result` field extracted as stdout (downstream contract preserved).
        self.assertEqual(result["stdout"], "Done.\n[anvil-coder] edited a.py")
        # usage + reported cost captured on the event.
        self.assertEqual(end["total_cost_usd"], 0.04953225)
        self.assertEqual(end["input_tokens"], 2152)
        self.assertEqual(end["output_tokens"], 4)
        self.assertEqual(end["cache_read_input_tokens"], 10315)
        self.assertIsInstance(end["usage"], dict)

    def test_coder_subprocess_text_fallback(self):
        # Non-JSON stdout (mock mode / a binary without JSON support / an
        # error) → fall back to raw text; no cost recorded.
        result, end = self._run_with_stdout("Done. Plain text, not JSON.")
        self.assertEqual(result["stdout"], "Done. Plain text, not JSON.")
        self.assertIsNone(end["usage"])
        self.assertIsNone(end["total_cost_usd"])
        self.assertIsNone(end["input_tokens"])

    def test_coder_subprocess_end_emits_cost_fields(self):
        # The cost-field keys are always present on the event (None on
        # fallback), so the harness can rely on the shape.
        _, end = self._run_with_stdout("plain text")
        for field in ("usage", "total_cost_usd", "input_tokens",
                      "output_tokens", "cache_creation_input_tokens",
                      "cache_read_input_tokens"):
            self.assertIn(field, end)

    def test_mock_mode_text_stdout_records_no_cost(self):
        # MockedCoder returns plain text (no real API call); the fallback
        # path must leave Coder cost unrecorded so mock-mode sweeps show $0
        # Coder cost. (The full MockedCoder integration is covered by
        # test_mocked.py; this pins the cost-fallback contract directly.)
        result, end = self._run_with_stdout("MockedCoder effect applied.")
        self.assertEqual(result["stdout"], "MockedCoder effect applied.")
        self.assertIsNone(end["total_cost_usd"])


class TestDeriveCoderModel(unittest.TestCase):
    """v3 Phase 2b Step 1 (V3P0-1 fix, Q-B1): _derive_coder_model picks the
    max-costUSD key from the envelope's modelUsage (the model is the KEY, not a
    top-level field). Single-model envelopes have one key; the multi-key path
    is synthetic — no Phase 2a/2b corpus exercises a multi-model Coder session."""

    def test_single_key_envelope(self) -> None:
        env = {"modelUsage": {"claude-haiku-4-5-20251001": {"costUSD": 0.037}}}
        self.assertEqual(
            coder._derive_coder_model(env), "claude-haiku-4-5-20251001")

    def test_multi_key_returns_max_cost(self) -> None:
        env = {"modelUsage": {
            "claude-haiku-4-5-20251001": {"costUSD": 0.05},
            "claude-opus-4-7": {"costUSD": 0.10},
        }}
        self.assertEqual(coder._derive_coder_model(env), "claude-opus-4-7")

    def test_tied_cost_is_deterministic(self) -> None:
        # Tie → Python's max returns the FIRST max by insertion order
        # (stable-but-implementation-defined; documented in the helper). Assert
        # determinism + first-max, not a semantic guarantee on which model wins.
        env = {"modelUsage": {
            "model-a": {"costUSD": 0.05}, "model-b": {"costUSD": 0.05}}}
        r1 = coder._derive_coder_model(env)
        self.assertEqual(r1, coder._derive_coder_model(env))   # deterministic
        self.assertEqual(r1, "model-a")                        # first-max

    def test_empty_modelusage_returns_none(self) -> None:
        self.assertIsNone(coder._derive_coder_model({"modelUsage": {}}))

    def test_missing_modelusage_returns_none(self) -> None:
        self.assertIsNone(coder._derive_coder_model({}))

    def test_missing_costusd_defaults_zero(self) -> None:
        # A single entry without costUSD → 0.0 default; still returns its key.
        self.assertEqual(
            coder._derive_coder_model({"modelUsage": {"some-model": {}}}),
            "some-model")

    def test_missing_costusd_loses_to_priced_key(self) -> None:
        env = {"modelUsage": {"unpriced": {}, "priced": {"costUSD": 0.01}}}
        self.assertEqual(coder._derive_coder_model(env), "priced")

    def test_env_none_returns_none(self) -> None:
        # Mock path / dead subprocess: no JSON envelope → env is None → None
        # (the caller maps this to "no-envelope", distinct from "unknown").
        self.assertIsNone(coder._derive_coder_model(None))


class CoderModelOverrideTests(unittest.TestCase):
    """v3 Phase 2e Step 1 (Q-E4): the ANVIL_CODER_MODEL routing override.

    Unset → no `--model` flag (the pre-2e default — CLI runs Opus 4.7[1m]).
    Set to a valid token → `--model <value>` inserted into the claude argv.
    Malformed → fail fast at construction (== startup), not mid-sweep.
    Format-only validation (no model allowlist — V3P1C-4 stale-list hazard).
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-coder-model-"))
        self.repo = self._tmp / "repo"
        _init_repo(self.repo, {"a.py": "# original\n"})

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _capture_argv(self, coder_obj):
        """Run a clean step under a captured subprocess.run; return the
        claude argv the Coder assembled."""
        captured = {}

        def fake_run(cmd, **kw):
            if cmd[0:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            captured["cmd"] = list(cmd)
            (self.repo / "a.py").write_text("# edited\n", encoding="utf-8")
            return _proc_result(0, "Done.", "")

        plan = _make_plan(files_to_touch=["a.py"], operations=["write"])
        brief = _make_brief(self.repo)
        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            coder_obj.execute_step(plan, brief)
        return captured["cmd"]

    def test_unset_inserts_no_model_flag(self):
        # The production default: no override → no --model → CLI's Opus 4.7[1m].
        with mock.patch.dict(os.environ):
            os.environ.pop("ANVIL_CODER_MODEL", None)
            argv = self._capture_argv(_make_coder())
        self.assertNotIn("--model", argv)
        # The base flags are still present (no behaviour change).
        self.assertIn("--print", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")

    def test_empty_string_treated_as_unset(self):
        # Mirrors ANVIL_CANARY_TASKS="" → empty: a blank/whitespace value is
        # not an override and must NOT raise (it is the unset case).
        for blank in ("", "   "):
            with mock.patch.dict(os.environ, {"ANVIL_CODER_MODEL": blank}):
                argv = self._capture_argv(_make_coder())
            self.assertNotIn("--model", argv, f"blank {blank!r} should be unset")

    def test_set_full_name_inserts_model_flag(self):
        with mock.patch.dict(
            os.environ, {"ANVIL_CODER_MODEL": "claude-haiku-4-5-20251001"}
        ):
            argv = self._capture_argv(_make_coder())
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1],
                         "claude-haiku-4-5-20251001")

    def test_set_alias_inserts_model_flag(self):
        # Aliases (no "claude-" prefix, no dashes) are valid CLI input — the
        # validation must not be over-strict and reject them.
        with mock.patch.dict(os.environ, {"ANVIL_CODER_MODEL": "haiku"}):
            argv = self._capture_argv(_make_coder())
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")

    def test_set_value_is_stripped(self):
        # Surrounding whitespace is stripped (a valid token remains valid).
        with mock.patch.dict(
            os.environ, {"ANVIL_CODER_MODEL": "  claude-haiku-4-5  "}
        ):
            argv = self._capture_argv(_make_coder())
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-4-5")

    def test_leading_dash_fails_fast_at_construction(self):
        # A flag-like value the CLI would misparse → ValueError at construction
        # (startup), before any sweep spend. NOT mid-step.
        with mock.patch.dict(os.environ, {"ANVIL_CODER_MODEL": "--dangerous"}):
            with self.assertRaises(ValueError):
                _make_coder()

    def test_internal_whitespace_fails_fast_at_construction(self):
        # A multi-token garbage value → ValueError at construction.
        with mock.patch.dict(os.environ, {"ANVIL_CODER_MODEL": "claude haiku"}):
            with self.assertRaises(ValueError):
                _make_coder()


if __name__ == "__main__":
    unittest.main()
