"""Step 8 tests — Orchestrator manual-mode full pass over the trivial brief.

Hermetic: a /tmp git repo as target_repo_path, a /tmp inbox brief, and
ANVIL_STATE_DIR pointed at a /tmp dir (runs/ + any marker land there).
NEVER ~/Downloads/anvil. Telegram and git_ops are mocked; the Planner is
a local FakePlanner returning in-scope canned Plans per brief step (no
LLM / no network). It replaced the Phase 0 stub injection when Step 6
deleted the stub (decision #8). run_smoke is injected (manual mode makes
no real file changes, so the trivial brief's real smokes can't pass — we
inject pass).

Note-2 / decision-enforcing assertions: a clean run must create NO lock
file (~/.anvil-active) and NO state/*.marker (telegram-down marker is
failure-only).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from anvil.config import Config
from anvil.orchestrator import Orchestrator
from anvil.planner import Plan
from anvil.state import read_state, state_dir

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRIVIAL = FIXTURES / "trivial-test-brief.md"
ANVIL_REPO = Path(__file__).resolve().parent.parent


class FakeTelegram:
    def __init__(self, replies):
        self.sent: list[str] = []
        self._replies = deque(replies)
        self._mid = 0

    def send(self, text: str) -> int:
        self.sent.append(text)
        self._mid += 1
        return self._mid

    def wait_for_reply(self, timeout):
        if not self._replies:
            return None
        text = self._replies.popleft()
        if text == "<INT>":
            # Simulate what the Phase B hotfix's flag-check does inside the
            # real wait_for_reply: a deliberate KeyboardInterrupt out of the
            # wait, which must reach Orchestrator.run()'s handler.
            raise KeyboardInterrupt("simulated SIGINT during wait_for_reply")

        class _R:
            pass
        r = _R()
        r.text = text
        r.message_id = 999
        r.timestamp = 0
        return r


class FakeGit:
    def __init__(self):
        self.calls = []

    def commit_step(self, repo_path, plan, step_idx, *, brief_name=None,
                    commit_message_hint=None, run_log_filename=None) -> str:
        self.calls.append({
            "step_idx": step_idx,
            "brief_name": brief_name,
            "commit_message_hint": commit_message_hint,
            "run_log_filename": run_log_filename,
        })
        return f"deadbeef{step_idx:02d}"


class FakePlanner:
    """Returns an in-scope Plan built from the brief step (mirrors what
    the deleted Phase 0 stub did for the trivial brief). No LLM, no
    network. plan_step signature matches the real Planner."""

    def plan_step(self, brief, state, step_idx: int):
        s = brief.steps[step_idx]
        return Plan(
            step_number=s.number,
            step_name=s.name,
            files_to_touch=list(s.scope_files),
            operations=list(s.scope_operations),
            approach=f"(fake) execute {s.name}",
            smoke_test=s.smoke,
            expected_outcome="ok",
            commit_message=s.commit_message_hint or f"Step {s.number}",
            scope_boundaries={
                "in_scope": ", ".join(s.scope_files) or "(none)",
                "out_of_scope": "anything not declared",
            },
            confidence="high",
            escalation_triggers=[],
        )


class TestOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_state = os.environ.get("ANVIL_STATE_DIR")
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-orch-"))
        self.assertTrue(str(self._tmp).startswith(tempfile.gettempdir()))
        self.assertNotEqual(self._tmp.resolve(), ANVIL_REPO.resolve())

        os.environ["ANVIL_STATE_DIR"] = str(self._tmp / "state")

        # /tmp target repo (validate_or_reject rule 3 needs a real git repo)
        self.repo = self._tmp / "target-repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)

        # trivial brief copy with target_repo_path rewritten, placed in inbox/
        inbox = self._tmp / "inbox"
        inbox.mkdir()
        text = TRIVIAL.read_text().replace(
            "target_repo_path: /tmp/anvil-test-repo",
            f"target_repo_path: {self.repo}",
        )
        self.brief_path = inbox / "trivial-test-brief.md"
        self.brief_path.write_text(text)

        # vault_path with no _voice.md → exercises voice snapshot fallback
        self.cfg = Config(
            anthropic_api_key="x",
            telegram_bot_token="t",
            telegram_chat_id="123",
            vault_path=self._tmp / "no-vault",
            anvil_root=ANVIL_REPO,
            anvil_defer_window_seconds=300,
            planner_model="claude-opus-4-7",
            planner_timeout=120,
            coder_timeout=600,
        )

    def tearDown(self) -> None:
        if self._prev_state is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev_state
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _orch(self, replies):
        self.tg = FakeTelegram(replies)
        self.git = FakeGit()
        return Orchestrator(
            self.cfg,
            coder_mode="manual",
            planner=FakePlanner(),
            telegram=self.tg,
            git=self.git,
            run_smoke=lambda cmd, cwd: (True, "pass"),
        )

    def test_full_manual_pass_over_trivial_brief(self) -> None:
        # step1 manual 'done', step1 confirm 'go', step2 manual 'done'
        # (auto: no confirm), step3 manual 'done', step3 confirm 'go'
        orch = self._orch(["done", "go", "done", "done", "go"])
        rc = orch.handle_brief(self.brief_path)
        self.assertEqual(rc, 0)

        st = read_state()
        self.assertIsNotNone(st)
        self.assertEqual(st.status, "done")
        self.assertEqual([s.status for s in st.steps], ["done", "done", "done"])
        self.assertTrue(all(s.commit for s in st.steps))  # none skipped
        self.assertIsNotNone(st.run_log)

        # run log written, slugged from build_name "Phase 0 — trivial round-trip"
        runs = list((state_dir() / "runs").glob(
            "*-phase-0-trivial-round-trip.md"))
        self.assertEqual(len(runs), 1)
        log_txt = runs[0].read_text()
        self.assertIn("ANVIL run log", log_txt)
        self.assertIn("complete", log_txt)
        self.assertEqual(Path(st.run_log).name, runs[0].name)

        # git.commit_step wired: 3 commits, run_log_filename = the log file
        self.assertEqual(len(self.git.calls), 3)
        for c in self.git.calls:
            self.assertEqual(c["run_log_filename"], runs[0].name)
            self.assertEqual(c["brief_name"], "Phase 0 — trivial round-trip")
            self.assertIsNone(c["commit_message_hint"])  # trivial steps set none

        # step-completion format on the EXPLICIT steps (1 and 3)
        joined = "\n---\n".join(self.tg.sent)
        self.assertIn("[ANVIL] Step 1 complete — Create a file", joined)
        self.assertIn("- What:", joined)
        self.assertIn("- Files: test.txt", joined)
        self.assertIn("- Smoke: pass", joined)
        self.assertIn("Reply 'go' to continue", joined)
        self.assertIn("[ANVIL] Step 3 complete — Verify and finish", joined)
        # AUTO step 2: no "Step 2 complete" confirmation message at all
        self.assertNotIn("Step 2 complete", joined)
        # completion message
        self.assertIn(
            "[ANVIL] Build complete — Phase 0 — trivial round-trip", joined
        )

        # --- the decision-enforcing assertions ---
        self.assertFalse(
            Path("~/.anvil-active").expanduser().exists(),
            "orchestrator must NEVER create the dead lock file",
        )
        markers = list(state_dir().glob("*.marker"))
        self.assertEqual(
            markers, [], f"clean run must produce no marker; found {markers}",
        )

    def test_handle_brief_stashes_lint_result(self) -> None:
        # v3 Phase 1a Step 2: handle_brief runs lint_brief after
        # validate_or_reject (and resolve_context_paths) and persists the
        # result on state.lint_result before the step loop.
        orch = self._orch(["done", "go", "done", "done", "go"])
        rc = orch.handle_brief(self.brief_path)
        self.assertEqual(rc, 0)
        st = read_state()
        self.assertIsNotNone(st.lint_result)
        self.assertEqual(
            set(st.lint_result.structured_features),
            {"brief_token_estimate", "step_count", "total_scope_files",
             "has_vps_deploy", "has_end_to_end_test", "context_paths_count",
             "confidence_band"},
        )
        # Trivial brief is in-corpus (3 steps, no deploy, canonical ops).
        self.assertEqual(st.lint_result.structured_features["step_count"], 3)
        self.assertEqual(st.lint_result.confidence_band, "high")

    def test_build_routing_policy_shadow_when_calibration_db_set(self) -> None:
        # v3 Phase 1b Step 2: ANVIL_CALIBRATION_DB set → the Stage A shadow
        # policy is selected (from_db is never-raise, so the path need not be a
        # valid DB for the selection logic to fire — a degraded calibration
        # still yields a PHASE_1B_STAGE_A_SHADOW policy).
        from anvil.policy import PHASE_1B_STAGE_A_SHADOW
        orch = self._orch([])
        with mock.patch.dict(
            os.environ, {"ANVIL_CALIBRATION_DB": "/tmp/anvil-cal-nonexistent.duckdb"}
        ):
            policy = orch._build_routing_policy()
        self.assertIsNotNone(policy)
        self.assertEqual(policy.policy_version, PHASE_1B_STAGE_A_SHADOW)
        self.assertIsNotNone(policy.calibration)

    def test_build_routing_policy_none_when_calibration_db_unset(self) -> None:
        # Unset → None → Planner defaults to PHASE_1A_PLACEHOLDER (back-compat).
        orch = self._orch([])
        with mock.patch.dict(os.environ):
            os.environ.pop("ANVIL_CALIBRATION_DB", None)
            self.assertIsNone(orch._build_routing_policy())

    def test_build_routing_policy_canary_when_task_in_allowlist(self) -> None:
        # v3 Phase 1b Step 3: calibration set + the current task in the canary
        # allowlist → PHASE_1B_STAGE_A_CANARY (from_db is never-raise, so the
        # path need not be a valid DB for the selection to fire).
        from anvil.policy import PHASE_1B_STAGE_A_CANARY
        orch = self._orch([])
        with mock.patch.dict(os.environ, {
            "ANVIL_CALIBRATION_DB": "/tmp/anvil-cal-nonexistent.duckdb",
            "ANVIL_CANARY_TASKS": "T1-doc-edit",
            "ANVIL_CURRENT_TASK": "T1-doc-edit",
        }):
            policy = orch._build_routing_policy()
        self.assertEqual(policy.policy_version, PHASE_1B_STAGE_A_CANARY)

    def test_build_routing_policy_shadow_when_task_not_in_allowlist(self) -> None:
        # Calibration set, but the current task is NOT allowlisted → shadow
        # (the canary is narrow; non-canary tasks stay shadow-only).
        from anvil.policy import PHASE_1B_STAGE_A_SHADOW
        orch = self._orch([])
        with mock.patch.dict(os.environ, {
            "ANVIL_CALIBRATION_DB": "/tmp/anvil-cal-nonexistent.duckdb",
            "ANVIL_CANARY_TASKS": "T1-doc-edit",
            "ANVIL_CURRENT_TASK": "T2-two-step",
        }):
            policy = orch._build_routing_policy()
        self.assertEqual(policy.policy_version, PHASE_1B_STAGE_A_SHADOW)

    def test_move_brief_updates_state_brief_path_for_resume(self) -> None:
        """Step 10 hotfix: after inbox→active move, state.brief_path must
        point at the active/ file (persisted), so resume() re-parses a path
        that still exists rather than the vacated inbox path."""
        from anvil.brief import parse_brief
        from anvil.state import init_state, read_state, write_state

        orch = self._orch([])
        brief = parse_brief(self.brief_path)
        st = init_state(
            brief, "2026-05-18T00:00:00+01:00",
            brief_path=str(self.brief_path), coder_mode="manual",
        )
        orch._state = st
        orch._run_log = None
        write_state(st)  # persisted with the inbox path
        self.assertTrue(self.brief_path.exists())

        orch._move_brief(self.brief_path)

        active = self._tmp / "active" / self.brief_path.name
        self.assertTrue(active.exists(), "brief must move into active/")
        self.assertFalse(self.brief_path.exists(), "inbox copy must be gone")
        self.assertEqual(orch._state.brief_path, str(active))
        self.assertEqual(read_state().brief_path, str(active))
        # resume() does parse_brief(state.brief_path) — must now succeed
        reparsed = parse_brief(read_state().brief_path)
        self.assertEqual(reparsed.build_name, "Phase 0 — trivial round-trip")

    def test_run_interrupted_at_step1_persists_paused_mid_execution(
        self,
    ) -> None:
        """Phase B hotfix end-to-end: a KeyboardInterrupt out of Step 1's
        wait_for_reply must propagate through handle_brief (which catches
        only Exception/AnvilError/NotImplementedError) to run()'s
        except KeyboardInterrupt, persisting status=paused-mid-execution."""
        orch = self._orch(["<INT>"])  # SIGINT at the Step 1 manual prompt
        rc = orch.run(self.brief_path)
        self.assertEqual(rc, 2)
        st = read_state()
        self.assertEqual(st.status, "paused-mid-execution")
        self.assertEqual(st.current_step, 1)
        self.assertEqual(st.steps[0].status, "running")
        # hotfix interplay: brief_path still points at the active/ copy
        self.assertTrue(st.brief_path.endswith("/active/" + self.brief_path.name))
        self.assertFalse(Path("~/.anvil-active").expanduser().exists())

    def test_auto_coder_mode_constructs_without_raising(self) -> None:
        # Phase 2 Step 9 replaces Phase 0's NotImplementedError
        # assertion: auto-mode is now wired (decisions P2-8 + P2-9),
        # so constructing an Orchestrator with coder_mode='auto'
        # should succeed and populate self.coder. Full integration
        # coverage lives at
        # tests/test_orchestrator_coder_integration.py — this test
        # guards the construction step alone, matching the original
        # test's narrow scope (Phase 0 asserted Phase 0 behaviour;
        # Phase 2 asserts Phase 2 behaviour).
        from unittest import mock
        from anvil.coder import Coder
        from anvil.orchestrator import Orchestrator
        orch = Orchestrator(
            self.cfg,
            coder_mode="auto",
            planner=mock.Mock(),
            telegram=mock.Mock(),
            git=mock.Mock(),
            run_smoke=mock.Mock(),
        )
        self.assertIsInstance(orch.coder, Coder)
        self.assertEqual(orch.coder_mode, "auto")

    def test_manual_abort_returns_nonzero_and_state_aborted(self) -> None:
        orch = self._orch(["abort"])  # step 1 manual reply = abort
        rc = orch.handle_brief(self.brief_path)
        self.assertEqual(rc, 1)
        self.assertEqual(read_state().status, "aborted")
        self.assertFalse(Path("~/.anvil-active").expanduser().exists())


if __name__ == "__main__":
    unittest.main()
