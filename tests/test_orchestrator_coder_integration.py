"""Phase 2 Step 9 — orchestrator+Coder integration tests.

FakeCoder injection mirroring Phase 1's FakePlanner pattern. Covers:
  (a) auto-mode happy path — Coder returns clean dict, step proceeds
      through smoke + commit, state.coder_output is populated
  (b) auto-mode escalation block (path reconciliation failed) →
      _escalate fires; reply "go" skips the step
  (c) auto-mode out_of_scope → escalates "coder-out-of-scope"; "go"
      proceeds to smoke
  (d) auto-mode exit_code != 0 → escalates "coder-failed"
  (e) head_hash fallback: manual-mode commit_step returns "" (clean tree)
      → state.commit gets git_ops.head_hash result
  (f) manual-mode unchanged — auto-only code paths must not affect
      the Phase 0/1 flow
  (g) _build_coder smoke — Coder gets constructed with the right
      arguments when coder_mode='auto' and no Coder injected

Hermetic: ANVIL_STATE_DIR -> tmp dir; brief parsing stubbed; Telegram +
git + smoke runner injected.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anvil.brief import Brief, Step
from anvil.orchestrator import Orchestrator
from anvil.planner import Plan


_VALID_PLAN = {
    "step_number": 1,
    "step_name": "Step one",
    "files_to_touch": ["a.py"],
    "operations": ["write", "commit"],
    "approach": "do it",
    "smoke_test": "echo s1",
    "expected_outcome": "ok",
    "commit_message": "Step 1: one",
    "scope_boundaries": {"in_scope": "a.py", "out_of_scope": "rest"},
    "confidence": "high",
    "escalation_triggers": [],
}


def _one_step_brief() -> Brief:
    return Brief(
        brief_version=1, project="anvil", build_name="coder-integration",
        target_repo="x", target_repo_path=Path("/tmp"), vps_deploy="no",
        steps=[Step(
            number=1, name="Step one",
            scope_files=["a.py"], scope_operations=["write", "commit"],
            smoke="echo s1", confirm="auto",
        )],
    )


class _StubBriefParse:
    def __init__(self, brief: Brief):
        self.brief = brief
        self._patches = []

    def __enter__(self):
        for target, ret in [
            ("anvil.orchestrator.parse_brief", lambda _p: self.brief),
            ("anvil.orchestrator.validate_or_reject", lambda _b: None),
            ("anvil.orchestrator.resolve_context_paths",
             lambda b, _vp: b),
        ]:
            p = mock.patch(target, side_effect=ret)
            self._patches.append(p)
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class _ReplyQueue:
    def __init__(self, replies):
        self._replies = list(replies)
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def wait_for_reply(self, timeout=None):
        if not self._replies:
            return SimpleNamespace(text="abort")
        return SimpleNamespace(text=self._replies.pop(0))


def _config():
    return SimpleNamespace(
        anthropic_api_key="x", planner_model="x",
        planner_timeout=60, coder_timeout=600,
        claude_binary=None,
        vault_path=Path("/tmp/no-vault"),
    )


def _planner_returning(plan_dict):
    planner = mock.Mock()
    planner.plan_step.return_value = Plan(**plan_dict)
    return planner


def _git_mock(commit_returns="abc1234", head_returns="def5678"):
    g = mock.Mock()
    g.commit_step.return_value = commit_returns
    g.head_hash.return_value = head_returns
    return g


class OrchestratorCoderAutoModeTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ANVIL_STATE_DIR")
        self._dir = Path(tempfile.mkdtemp(prefix="anvil-test-step9-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._dir)
        self.brief = _one_step_brief()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev
        shutil.rmtree(self._dir, ignore_errors=True)

    def _build_orch(self, *, coder, planner=None, tg=None,
                    git=None, smoke=(True, "ok")):
        return Orchestrator(
            _config(),
            coder_mode="auto",
            planner=planner or _planner_returning(_VALID_PLAN),
            telegram=tg or _ReplyQueue([]),
            git=git or _git_mock(),
            run_smoke=mock.Mock(return_value=smoke),
            coder=coder,
        )

    # --- (a) happy path ---
    def test_a_auto_happy_path_proceeds_through_smoke_and_commit(self):
        coder = mock.Mock()
        coder.execute_step.return_value = {
            "stdout": "Done.", "stderr": "", "exit_code": 0,
            "files_touched": ["a.py"], "out_of_scope": [],
            "reconciliations": [], "duration_s": 4.2,
            "allowed_tools": ["Edit", "Write"],
            "disallowed_tools": ["Bash"],
        }
        # confirm="auto" so no Telegram round-trip at step-done
        tg = _ReplyQueue([])
        orch = self._build_orch(coder=coder, tg=tg)
        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(Path("/tmp/coder-integration.md"))
        self.assertEqual(rc, 0)
        coder.execute_step.assert_called_once()
        # Coder output persisted on state
        self.assertIsNotNone(orch._state.steps[0].coder_output)
        self.assertEqual(
            orch._state.steps[0].coder_output["exit_code"], 0,
        )
        # State.commit got the commit_step return value
        self.assertEqual(orch._state.steps[0].commit, "abc1234")
        self.assertEqual(orch._state.steps[0].status, "done")

    # --- (b) escalation block (path reconciliation failed) ---
    def test_b_auto_escalation_block_routes_to_escalate(self):
        coder = mock.Mock()
        coder.execute_step.return_value = {
            "escalate": True,
            "reason": "coder-path-reconciliation-failed",
            "detail": "could not find chat_handler.py",
            "step_number": 1,
            "reconciliations": [],
        }
        # Reply "go" → step marked done with commit=None, proceeds
        tg = _ReplyQueue(["go"])
        orch = self._build_orch(coder=coder, tg=tg)
        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(Path("/tmp/coder-integration.md"))
        self.assertEqual(rc, 0)
        # An escalation message was sent to Telegram
        self.assertTrue(any("coder-path-reconciliation-failed" in m
                            for m in tg.sent),
                        f"escalation reason not in sent: {tg.sent}")
        self.assertEqual(orch._state.steps[0].status, "done")
        self.assertIsNone(orch._state.steps[0].commit)

    # --- (c) out_of_scope ---
    def test_c_auto_out_of_scope_escalates_and_resumes(self):
        coder = mock.Mock()
        coder.execute_step.return_value = {
            "stdout": "", "stderr": "", "exit_code": 0,
            "files_touched": ["a.py", "b.py"],
            "out_of_scope": ["b.py"],
            "reconciliations": [], "duration_s": 1.0,
            "allowed_tools": ["Edit"], "disallowed_tools": ["Bash"],
        }
        # Reply "go" past the escalation, then auto-confirms the step
        tg = _ReplyQueue(["go"])
        orch = self._build_orch(coder=coder, tg=tg)
        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(Path("/tmp/coder-integration.md"))
        self.assertEqual(rc, 0)
        self.assertTrue(any("coder-out-of-scope" in m for m in tg.sent),
                        f"out_of_scope reason not in sent: {tg.sent}")
        # User said go; step proceeded through smoke + commit
        self.assertEqual(orch._state.steps[0].status, "done")
        self.assertEqual(orch._state.steps[0].commit, "abc1234")

    # --- (d) non-zero exit ---
    def test_d_auto_nonzero_exit_escalates(self):
        coder = mock.Mock()
        coder.execute_step.return_value = {
            "stdout": "", "stderr": "boom", "exit_code": 1,
            "files_touched": [], "out_of_scope": [],
            "reconciliations": [], "duration_s": 0.1,
            "allowed_tools": [], "disallowed_tools": ["Bash"],
        }
        tg = _ReplyQueue(["abort"])
        orch = self._build_orch(coder=coder, tg=tg)
        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(Path("/tmp/coder-integration.md"))
        self.assertEqual(rc, 1)
        self.assertTrue(any("coder-failed" in m for m in tg.sent))


class HeadHashFallbackTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ANVIL_STATE_DIR")
        self._dir = Path(tempfile.mkdtemp(prefix="anvil-test-step9-h-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._dir)
        self.brief = _one_step_brief()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev
        shutil.rmtree(self._dir, ignore_errors=True)

    # --- (e) head_hash fallback closes #14/17 ---
    def test_e_manual_mode_state_commit_falls_back_to_head_hash(self):
        # Manual mode flow: commit_step returns "" (Genco committed himself);
        # head_hash returns the real SHA from git rev-parse HEAD.
        git = _git_mock(commit_returns="", head_returns="genco-sha")
        # Reply queue: "done" for the manual coder step.
        tg = _ReplyQueue(["done"])
        orch = Orchestrator(
            _config(),
            coder_mode="manual",
            planner=_planner_returning(_VALID_PLAN),
            telegram=tg,
            git=git,
            run_smoke=mock.Mock(return_value=(True, "ok")),
        )
        with _StubBriefParse(self.brief):
            rc = orch.handle_brief(Path("/tmp/coder-integration.md"))
        self.assertEqual(rc, 0)
        # commit_step ran and returned ""
        git.commit_step.assert_called_once()
        # head_hash was called as fallback
        git.head_hash.assert_called()
        # state.commit gets the head_hash result
        self.assertEqual(orch._state.steps[0].commit, "genco-sha")


class BuildCoderTests(unittest.TestCase):
    # --- (g) _build_coder constructs the right thing ---
    def test_g_build_coder_uses_coder_system_md_and_config_timeout(self):
        # Patch shutil.which so the test doesn't depend on `claude` being
        # installed.
        cfg = _config()
        cfg.claude_binary = "/usr/local/bin/claude"
        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            orch = Orchestrator(
                cfg,
                coder_mode="auto",
                planner=_planner_returning(_VALID_PLAN),
                telegram=_ReplyQueue([]),
                git=_git_mock(),
                run_smoke=mock.Mock(return_value=(True, "ok")),
            )
        self.assertIsNotNone(orch.coder)
        self.assertEqual(orch.coder.timeout, 600)
        # System prompt was loaded and voice-substituted (no literal
        # {VOICE_SPEC} token remaining)
        self.assertNotIn("{VOICE_SPEC}", orch.coder.system_prompt)
        self.assertIn("scope-fidelity rule", orch.coder.system_prompt)

    def test_h_manual_mode_does_not_construct_coder(self):
        # If coder_mode is manual, self.coder is None and no construction
        # happens (so no Claude Code binary is required to be present).
        orch = Orchestrator(
            _config(),
            coder_mode="manual",
            planner=_planner_returning(_VALID_PLAN),
            telegram=_ReplyQueue([]),
            git=_git_mock(),
            run_smoke=mock.Mock(return_value=(True, "ok")),
        )
        self.assertIsNone(orch.coder)


if __name__ == "__main__":
    unittest.main()
