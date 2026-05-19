"""v2 Phase 1 Step 2 — Orchestrator event instrumentation.

Reuses the FakePlanner / FakeTelegram / FakeGit shapes from
`tests/test_orchestrator.py` against a hermetic /tmp brief. Asserts:

  - run.start → brief.parsed → brief.validated → step.start ...
    step.end → run.end sequence on a clean manual-mode pass.
  - State.run_id is set during handle_brief and persists in state.json.
  - Resume reuses the existing state.run_id (run.resume, not run.start).
  - Resume with state.run_id=None constructs a fresh id and logs.
  - Escalation flow emits escalation.raised + escalation.resolved with
    a populated latency_ms_user.
  - events.end_run() fires on success and on aborted-via-escalation
    paths (drops field reflects run-end accounting).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path

# anvil.checkpoint must be imported BEFORE anvil.orchestrator when this
# module is loaded in isolation; checkpoint.py at line 28 does
# `from anvil.orchestrator import _slug` and Python's circular-import
# handling needs the dependent module to be partway initialised. In the
# full unittest discover run other tests load checkpoint first; in
# isolated runs (single-test invocations) we need this hint.
import anvil.checkpoint  # noqa: F401

from anvil import events
from anvil.config import Config
from anvil.orchestrator import Orchestrator
from anvil.planner import Plan
from anvil.state import read_state, state_dir, write_state, init_state

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
        class _R: pass
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
        self.calls.append({"step_idx": step_idx})
        return f"deadbeef{step_idx:02d}"


class FakePlanner:
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


class _OrchEventsBase(unittest.TestCase):
    def setUp(self) -> None:
        # Reset events module state.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        self._prev_state = os.environ.get("ANVIL_STATE_DIR")
        self._prev_anvil_root = os.environ.get("ANVIL_ROOT")
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-orch-events-"))
        # Both state dir AND ANVIL_ROOT under tmp so events.jsonl lands hermetic.
        os.environ["ANVIL_STATE_DIR"] = str(self._tmp / "state")
        os.environ["ANVIL_ROOT"] = str(self._tmp)

        self.repo = self._tmp / "target-repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)

        inbox = self._tmp / "inbox"
        inbox.mkdir()
        text = TRIVIAL.read_text().replace(
            "target_repo_path: /tmp/anvil-test-repo",
            f"target_repo_path: {self.repo}",
        )
        self.brief_path = inbox / "trivial-test-brief.md"
        self.brief_path.write_text(text)

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
        # Restore env.
        for k, prev in (("ANVIL_STATE_DIR", self._prev_state),
                        ("ANVIL_ROOT", self._prev_anvil_root)):
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _orch(self, replies, planner=None):
        self.tg = FakeTelegram(replies)
        self.git = FakeGit()
        return Orchestrator(
            self.cfg,
            coder_mode="manual",
            planner=planner or FakePlanner(),
            telegram=self.tg,
            git=self.git,
            run_smoke=lambda cmd, cwd: (True, "pass"),
        )

    def _read_events(self, run_id: str) -> list[dict]:
        path = self._tmp / "state" / "runs" / run_id / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]


class TestOrchestratorHappyPath(_OrchEventsBase):

    def test_full_pass_emits_run_brief_step_sequence(self) -> None:
        # The trivial brief is 3 steps: explicit, auto, explicit.
        # Replies feed manual mode + explicit confirms.
        orch = self._orch(["done", "go", "done", "done", "go"])
        rc = orch.handle_brief(self.brief_path)
        self.assertEqual(rc, 0)

        st = read_state()
        self.assertIsNotNone(st.run_id)
        rows = self._read_events(st.run_id)
        kinds = [r["kind"] for r in rows]

        # Required ordering at the front:
        self.assertEqual(kinds[0], "run.start")
        self.assertIn("brief.parsed", kinds)
        self.assertIn("brief.validated", kinds)
        self.assertLess(kinds.index("brief.parsed"),
                        kinds.index("brief.validated"))
        # Three step.start, three step.end:
        self.assertEqual(kinds.count("step.start"), 3)
        self.assertEqual(kinds.count("step.end"), 3)
        # Terminal:
        self.assertEqual(kinds[-1], "run.end")
        # run.end carries drops counter.
        run_end = rows[-1]
        self.assertIn("drops", run_end["data"])

    def test_state_run_id_persists(self) -> None:
        orch = self._orch(["done", "go", "done", "done", "go"])
        orch.handle_brief(self.brief_path)
        st = read_state()
        self.assertIsNotNone(st.run_id)
        # Pattern: YYYY-MM-DD-HHMM-<slug>
        self.assertRegex(st.run_id, r"^\d{4}-\d{2}-\d{2}-\d{4}-")


class TestOrchestratorResume(_OrchEventsBase):

    def test_resume_reuses_run_id_emits_run_resume(self) -> None:
        # Pre-seed a paused state with a known run_id, persist, then
        # invoke handle_brief with resumed_state=<state>.
        from anvil.brief import parse_brief, validate_or_reject, resolve_context_paths
        brief = parse_brief(self.brief_path)
        validate_or_reject(brief)
        brief = resolve_context_paths(brief, self.cfg.vault_path)
        # Move brief to active/ so handle_brief's resume path finds it.
        active = self._tmp / "active"
        active.mkdir()
        new_brief_path = active / self.brief_path.name
        shutil.move(str(self.brief_path), str(new_brief_path))
        st = init_state(brief, "2026-05-20T00:00:00",
                        brief_path=str(new_brief_path))
        # Seed a run_log path so git_ops.commit_step's filename ref
        # doesn't fall on a None Path. A pre-existing handle_brief
        # resume branch requirement; not Step-2 specific.
        run_log = self._tmp / "state" / "runs" / "synthetic-resume.md"
        run_log.parent.mkdir(parents=True, exist_ok=True)
        run_log.write_text("# resume run log\n", encoding="utf-8")
        st.run_log = str(run_log)
        st.run_id = "2026-05-20-1500-resume-test"
        st.status = "paused-by-user"
        write_state(st)

        orch = self._orch(["done", "go", "done", "done", "go"])
        rc = orch.handle_brief(new_brief_path, resumed_state=st)
        self.assertEqual(rc, 0)

        rows = self._read_events("2026-05-20-1500-resume-test")
        kinds = [r["kind"] for r in rows]
        # On resume we expect run.start (from begin_run) AND run.resume.
        self.assertIn("run.resume", kinds)
        # The run.resume must reference the same run_id.
        resume_row = next(r for r in rows if r["kind"] == "run.resume")
        self.assertEqual(resume_row["data"]["run_id"],
                         "2026-05-20-1500-resume-test")

    def test_resume_with_null_run_id_constructs_fresh(self) -> None:
        # Legacy state: run_id=None. Resume should mint a fresh id and proceed.
        from anvil.brief import parse_brief, validate_or_reject, resolve_context_paths
        brief = parse_brief(self.brief_path)
        validate_or_reject(brief)
        brief = resolve_context_paths(brief, self.cfg.vault_path)
        active = self._tmp / "active"
        active.mkdir()
        new_brief_path = active / self.brief_path.name
        shutil.move(str(self.brief_path), str(new_brief_path))
        st = init_state(brief, "2026-05-20T00:00:00",
                        brief_path=str(new_brief_path))
        # Seed a run_log path so git_ops.commit_step's filename ref
        # doesn't fall on a None Path. A pre-existing handle_brief
        # resume branch requirement; not Step-2 specific.
        run_log = self._tmp / "state" / "runs" / "synthetic-resume.md"
        run_log.parent.mkdir(parents=True, exist_ok=True)
        run_log.write_text("# resume run log\n", encoding="utf-8")
        st.run_log = str(run_log)
        st.run_id = None
        st.status = "paused-by-user"
        write_state(st)

        orch = self._orch(["done", "go", "done", "done", "go"])
        rc = orch.handle_brief(new_brief_path, resumed_state=st)
        self.assertEqual(rc, 0)
        # After resume completes, state has a non-None run_id.
        st2 = read_state()
        self.assertIsNotNone(st2.run_id)
        self.assertTrue(st2.run_id.endswith("-resumed"))


class TestOrchestratorEscalationAndEndRun(_OrchEventsBase):

    def test_escalation_raised_and_resolved_emitted_with_latency(self) -> None:
        # Trigger smoke failure → escalation → reply "abort".
        # smoke runs at step 1, returns False, _escalate fires,
        # _await_user_decision sees "abort".
        self.tg = FakeTelegram(["done", "abort"])
        self.git = FakeGit()
        orch = Orchestrator(
            self.cfg, coder_mode="manual",
            planner=FakePlanner(), telegram=self.tg, git=self.git,
            run_smoke=lambda cmd, cwd: (False, "boom"),
        )
        rc = orch.handle_brief(self.brief_path)
        self.assertEqual(rc, 1)

        st = read_state()
        rows = self._read_events(st.run_id)
        kinds = [r["kind"] for r in rows]
        self.assertIn("escalation.raised", kinds)
        self.assertIn("escalation.resolved", kinds)
        raised_idx = kinds.index("escalation.raised")
        resolved_idx = kinds.index("escalation.resolved")
        self.assertLess(raised_idx, resolved_idx)
        resolved = rows[resolved_idx]
        self.assertEqual(resolved["data"]["reply"], "abort")
        # latency_ms_user populated and non-negative.
        self.assertIsNotNone(resolved["data"]["latency_ms_user"])
        self.assertGreaterEqual(resolved["data"]["latency_ms_user"], 0)

    def test_run_end_fires_on_abort_path(self) -> None:
        # Same as above; the finally clause must still emit run.end.
        orch = Orchestrator(
            self.cfg, coder_mode="manual",
            planner=FakePlanner(),
            telegram=FakeTelegram(["done", "abort"]),
            git=FakeGit(),
            run_smoke=lambda cmd, cwd: (False, "boom"),
        )
        rc = orch.handle_brief(self.brief_path)
        self.assertEqual(rc, 1)
        st = read_state()
        rows = self._read_events(st.run_id)
        self.assertEqual(rows[-1]["kind"], "run.end")


if __name__ == "__main__":
    unittest.main()
