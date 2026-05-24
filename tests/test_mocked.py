"""v2 Phase 1 Step 5 — MockedPlanner / MockedCoder tests.

Hermetic: ANVIL_ROOT, ANVIL_STATE_DIR, ANVIL_MOCKED_FIXTURE_ROOT all
redirected to tmp_path; MOCKED_TASK_ID + MOCKED_PLANNER_JITTER_MS +
MOCKED_CODER_JITTER_MS set per test. The default fixture root under
`tests/fixtures/v2-phase-1/mocked-plans/` is used directly — Step 5
authored those fixtures and they're tracked in git.
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
from types import SimpleNamespace
from unittest import mock

# Circular import workaround (notes.md Step 2 outcome).
import anvil.checkpoint  # noqa: F401

from anvil import events
from anvil.brief import Brief, Step
from anvil.calibration import CHEAP_STAGE_A_MODEL, RoutingCalibration
from anvil.config import Config
from anvil.mocked import MockedCoder, MockedPlanner
from anvil.orchestrator import Orchestrator
from anvil.planner import Plan
from anvil.policy import (
    PHASE_1B_STAGE_A_CANARY,
    PHASE_1B_STAGE_A_SHADOW,
    RoutingPolicy,
)
from anvil.state import init_state, read_state

_FIX_ROOT = (
    Path(__file__).resolve().parent
    / "fixtures" / "v2-phase-1" / "mocked-plans"
)
ANVIL_REPO = Path(__file__).resolve().parent.parent


def _brief_and_state(task_id: str, scope_files=None, smoke="echo ok"):
    """Synthetic Brief shaped to let the fixtures pass _validate_plan_structure.

    Each calibration task's plan declares files in `scope_files`; the brief
    must declare the same so rule 4 (files_to_touch within declared scope)
    passes. Default ["README.md", "a.py", "b.py", "version.txt",
    "CHANGELOG.md", "retention.py"] — superset of every fixture's scope.
    """
    scope_files = scope_files or [
        "README.md", "a.py", "b.py", "version.txt",
        "CHANGELOG.md", "retention.py",
    ]
    brief = Brief(
        brief_version=1,
        project="anvil",
        build_name=f"calibration-{task_id}",
        target_repo="x",
        target_repo_path=Path("/tmp"),
        vps_deploy="no",
        steps=[
            Step(
                number=1,
                name="Step 1",
                scope_files=scope_files,
                scope_operations=["write", "smoke-test"],
                smoke=smoke,
                confirm="auto",
            ),
            Step(
                number=2,
                name="Step 2",
                scope_files=scope_files,
                scope_operations=["write", "smoke-test"],
                smoke=smoke,
                confirm="auto",
            ),
        ],
    )
    state = init_state(brief, "2026-05-20T00:00:00", brief_path="/nonexistent")
    return brief, state


class _MockedTestBase(unittest.TestCase):
    """Reset events state, redirect ANVIL_ROOT, set common env."""

    def setUp(self) -> None:
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(os.environ, {
            "ANVIL_ROOT": str(self.tmp_path),
            "MOCKED_PLANNER_JITTER_MS": "0",
            "MOCKED_CODER_JITTER_MS": "0",
            "MOCKED_TASK_ID": "",  # individual tests set
        })
        self._env_patch.start()
        events.begin_run("mocked-test")

    def tearDown(self) -> None:
        events.end_run()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _events(self, run_id: str = "mocked-test") -> list[dict]:
        path = self.tmp_path / "state" / "runs" / run_id / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]


class TestMockedPlannerCallAnthropic(_MockedTestBase):

    def test_stage_a_returns_fixture_and_emits_api_end(self) -> None:
        os.environ["MOCKED_TASK_ID"] = "T1"
        p = MockedPlanner(model="claude-opus-4-7")
        p._current_step_idx = 0
        text = p._call_anthropic(system="(sys)", user="(usr)", timeout=30,
                                 step=1, stage="A")
        self.assertIn("step_number", text)
        ends = [e for e in self._events()
                if e["kind"] == "planner.stage_a.api_end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["data"]["model"], "claude-opus-4-7")
        # Token counts from T1-step0.usage.json
        self.assertEqual(ends[0]["data"]["input_tokens"], 10500)
        self.assertEqual(ends[0]["data"]["output_tokens"], 115)
        self.assertTrue(ends[0]["data"]["ok"])

    def test_plan_step_records_selected_paths_and_raw_response(self) -> None:
        # v3 Phase 2a Step 2 (V3P2A-2 / Step2-2a-F1): MockedPlanner overrides
        # ONLY _call_anthropic and inherits plan_step, so the parsed emit
        # carries the three new fields from the fixture's parse — no mocked.py
        # change needed (the parallel-wire is the shared emit, not a mock copy).
        os.environ["MOCKED_TASK_ID"] = "T1"
        p = MockedPlanner(model="claude-opus-4-7")
        brief, state = _brief_and_state("T1")
        p.plan_step(brief, state, 0)
        parsed = next(e for e in self._events()
                      if e["kind"] == "planner.stage_a.parsed")["data"]
        self.assertIsInstance(parsed["selected_paths"], list)
        self.assertEqual(parsed["selected_paths"], [])   # empty-context fixture
        self.assertEqual(parsed["paths_returned"], len(parsed["selected_paths"]))
        self.assertIsInstance(parsed["raw_response_text"], str)
        self.assertGreater(len(parsed["raw_response_text"]), 0)  # the fixture text
        self.assertFalse(parsed["truncated"])

    def test_stage_b_emits_correct_kind(self) -> None:
        os.environ["MOCKED_TASK_ID"] = "T2"
        p = MockedPlanner(model="claude-opus-4-7")
        p._current_step_idx = 1
        p._call_anthropic(system="", user="", timeout=30, step=2, stage="B")
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("planner.stage_b.api_end", kinds)

    def test_synthesised_api_end_carries_routing_fields(self) -> None:
        # v3 Phase 0 Step 1 (V3P0-1): the MockedPlanner emit carries the
        # five routing fields, same shape as the production wrapper.
        os.environ["MOCKED_TASK_ID"] = "T1"
        p = MockedPlanner(model="claude-opus-4-7")
        p._current_step_idx = 0
        p._current_context_paths_count = 3
        p._call_anthropic(system="(sys)", user="(usr)", timeout=30,
                          step=1, stage="A")
        end = next(e for e in self._events()
                   if e["kind"] == "planner.stage_a.api_end")
        d = end["data"]
        self.assertEqual(d["route_actual"], "claude-opus-4-7")
        self.assertEqual(d["route_candidate"], "claude-opus-4-7")
        self.assertFalse(d["route_fallback_fired"])
        # v3 Phase 1a Step 3: policy_version flips to the Phase 1a stamp
        # (the mocked planner's model already == the placeholder, so
        # route_actual is unchanged at "claude-opus-4-7").
        self.assertEqual(d["policy_version"], "v3-phase-1a-placeholder")
        fs = d["features_seen"]
        self.assertEqual(fs["stage"], "A")
        self.assertEqual(fs["step_idx"], 0)
        self.assertEqual(fs["context_paths_count"], 3)
        # observed_prompt_token_count mirrors input_tokens (T1-step0.usage.json).
        self.assertEqual(fs["observed_prompt_token_count"], d["input_tokens"])

    def test_synthesised_emit_pairs_shadow_decision(self) -> None:
        # v3 Phase 0 Step 2 (V3P0-3): the mock path emits a paired
        # shadow.decision after its synthesised api_end (criterion 1).
        os.environ["MOCKED_TASK_ID"] = "T1"
        p = MockedPlanner(model="claude-opus-4-7")
        p._current_step_idx = 0
        p._current_context_paths_count = 0
        p._call_anthropic(system="(sys)", user="(usr)", timeout=30,
                          step=1, stage="A")
        evs = self._events()
        kinds = [e["kind"] for e in evs]
        self.assertIn("shadow.decision", kinds)
        # Immediately after the api_end.
        i_api = kinds.index("planner.stage_a.api_end")
        self.assertEqual(kinds[i_api + 1], "shadow.decision")
        sd = evs[i_api + 1]["data"]
        self.assertEqual(sd["stage"], "A")
        self.assertEqual(sd["shadow_route_candidate"], "claude-opus-4-7")
        self.assertEqual(sd["actual_route_taken"], "claude-opus-4-7")
        self.assertTrue(sd["agreement"])

    def test_missing_task_id_raises(self) -> None:
        os.environ["MOCKED_TASK_ID"] = ""
        p = MockedPlanner(model="claude-opus-4-7")
        with self.assertRaises(RuntimeError) as cm:
            p._call_anthropic(system="", user="", timeout=30, step=1, stage="A")
        self.assertIn("MOCKED_TASK_ID", str(cm.exception))

    def test_stage_c_missing_fixture_returns_empty(self) -> None:
        """Step 6 prep: tasks reaching orchestrator step 9 invoke
        draft_completion_artefacts → _call_anthropic(step=0, stage="C").
        No T1-stepC.json exists; MockedPlanner must return "" so the
        completion-artefacts-draft-failed escalation path fires (same
        code path real-mode hits on an API hiccup). The synthesised
        api_end still emits so the operations view records the call."""
        os.environ["MOCKED_TASK_ID"] = "T1"
        p = MockedPlanner(model="claude-opus-4-7")
        # Stage C is invoked from draft_completion_artefacts with step=0.
        text = p._call_anthropic(system="", user="", timeout=30,
                                 step=0, stage="C")
        self.assertEqual(text, "")
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("planner.stage_c.api_end", kinds)

    def test_escalation_fixture_routes_through_plan_step(self) -> None:
        """T4-step0 is an escalate:true JSON; plan_step should propagate."""
        os.environ["MOCKED_TASK_ID"] = "T4"
        brief, state = _brief_and_state("T4")
        p = MockedPlanner(model="claude-opus-4-7", vault_root=Path("/tmp"))
        result = p.plan_step(brief, state, 0)
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("escalate"))
        self.assertEqual(result.get("reason"), "judgment-call")


class TestMockedCoderRealRun(_MockedTestBase):

    def _init_repo(self, repo: Path) -> None:
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                       check=True, capture_output=True)
        (repo / ".keep").write_text("", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo,
                       check=True, capture_output=True)

    def test_real_run_creates_files_per_coder_effect(self) -> None:
        os.environ["MOCKED_TASK_ID"] = "T1"
        repo = self.tmp_path / "repo"
        self._init_repo(repo)
        c = MockedCoder(claude_binary=Path("/usr/bin/true"),
                        timeout=30, system_prompt="(sys)")
        c._current_step_idx = 0
        proc = c._real_run(["claude"], "(prompt)", str(repo))
        self.assertEqual(proc.returncode, 0)
        # T1-step0.coder-effect.yaml creates README.md.
        self.assertTrue((repo / "README.md").is_file())
        self.assertIn("T1 target", (repo / "README.md").read_text())

    def test_t3_step1_creates_both_a_and_b(self) -> None:
        """The out-of-scope trap fixture creates both a.py (in-scope) AND
        b.py (out-of-scope) — Layer 2 git-diff in Coder.execute_step
        catches b.py and routes the coder-out-of-scope escalation."""
        os.environ["MOCKED_TASK_ID"] = "T3"
        repo = self.tmp_path / "repo"
        self._init_repo(repo)
        c = MockedCoder(claude_binary=Path("/usr/bin/true"),
                        timeout=30, system_prompt="(sys)")
        c._current_step_idx = 1  # step 2 (the trap)
        c._real_run(["claude"], "(prompt)", str(repo))
        self.assertTrue((repo / "a.py").is_file())
        self.assertTrue((repo / "b.py").is_file())

    def test_missing_coder_effect_fixture_is_silent(self) -> None:
        """T4-step0 has no .coder-effect.yaml (Coder never runs for T4),
        but if MockedCoder._real_run is invoked anyway it must not raise."""
        os.environ["MOCKED_TASK_ID"] = "T4"
        repo = self.tmp_path / "repo"
        self._init_repo(repo)
        c = MockedCoder(claude_binary=Path("/usr/bin/true"),
                        timeout=30, system_prompt="(sys)")
        c._current_step_idx = 0
        proc = c._real_run(["claude"], "(prompt)", str(repo))
        self.assertEqual(proc.returncode, 0)
        # No files created — the repo still has only .keep.
        files = [p.name for p in repo.iterdir() if p.is_file()]
        self.assertEqual(files, [".keep"])


class TestConfigMockedFlags(unittest.TestCase):
    """Config.load picks up MOCKED_PLANNER and MOCKED_CODER env."""

    def test_default_off(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "y",
            "TELEGRAM_CHAT_ID": "1", "VAULT_PATH": "/tmp/v",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Config.load(env_path=Path("/nonexistent"))
        self.assertFalse(cfg.mocked_planner)
        self.assertFalse(cfg.mocked_coder)

    def test_env_flips_flags(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "y",
            "TELEGRAM_CHAT_ID": "1", "VAULT_PATH": "/tmp/v",
            "MOCKED_PLANNER": "1", "MOCKED_CODER": "1",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = Config.load(env_path=Path("/nonexistent"))
        self.assertTrue(cfg.mocked_planner)
        self.assertTrue(cfg.mocked_coder)


# --- Determinism end-to-end --------------------------------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRIVIAL = FIXTURES / "trivial-test-brief.md"


class FakeTelegram:
    def __init__(self, replies):
        self.sent: list[str] = []
        self._replies = deque(replies)

    def send(self, text):
        self.sent.append(text)
        return 1

    def wait_for_reply(self, timeout):
        if not self._replies:
            return None
        text = self._replies.popleft()
        r = SimpleNamespace()
        r.text = text
        r.message_id = 1
        r.timestamp = 0
        return r


class FakeGit:
    def commit_step(self, *a, **kw):
        return ""

    def head_hash(self, *a, **kw):
        return ""

    def push(self, *a, **kw):
        return (True, "")


class TestDeterminism(unittest.TestCase):
    """With jitters at 0, two consecutive runs of the same brief
    produce byte-identical events.jsonl modulo ts and elapsed_ms.

    This is the load-bearing assertion the brief calls out for the
    calibration framework: the framework-only profile must be stable
    across repeated runs.
    """

    def setUp(self) -> None:
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-det-"))
        self._prev_state = os.environ.get("ANVIL_STATE_DIR")
        self._prev_root = os.environ.get("ANVIL_ROOT")
        os.environ["ANVIL_STATE_DIR"] = str(self._tmp / "state")
        os.environ["ANVIL_ROOT"] = str(self._tmp)
        os.environ["MOCKED_PLANNER_JITTER_MS"] = "0"
        os.environ["MOCKED_CODER_JITTER_MS"] = "0"
        os.environ["MOCKED_TASK_ID"] = "T1"

        # Hermetic target repo + brief.
        self.repo = self._tmp / "target-repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        # Real fixture brief
        inbox = self._tmp / "inbox"
        inbox.mkdir()
        text = TRIVIAL.read_text().replace(
            "target_repo_path: /tmp/anvil-test-repo",
            f"target_repo_path: {self.repo}",
        )
        self.brief_path = inbox / "trivial.md"
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
            mocked_planner=True,
            mocked_coder=True,
        )

    def tearDown(self) -> None:
        for k, prev in (("ANVIL_STATE_DIR", self._prev_state),
                        ("ANVIL_ROOT", self._prev_root)):
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        for k in ("MOCKED_PLANNER_JITTER_MS", "MOCKED_CODER_JITTER_MS",
                  "MOCKED_TASK_ID", "ANVIL_RUN_ID_OVERRIDE"):
            os.environ.pop(k, None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_once(self, run_id: str) -> list[dict]:
        # Reset events state between runs.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()
        # Re-place the brief at inbox/ (orchestrator moves it to active/).
        # For run 2, place a fresh copy.
        target_brief = self._tmp / "inbox" / "trivial.md"
        if not target_brief.is_file():
            # Move it back from active/ for the second run.
            active = self._tmp / "active" / "trivial.md"
            if active.is_file():
                shutil.move(str(active), str(target_brief))

        os.environ["ANVIL_RUN_ID_OVERRIDE"] = run_id
        # Telegram replies for the trivial brief: step1 (auto), step1 confirm,
        # step2 (no confirm), step3 (auto), step3 confirm — but with mocked
        # Coder we're in auto mode, no manual replies. Just provide confirm
        # responses for "explicit" steps.
        tg = FakeTelegram(["go", "go"])
        orch = Orchestrator(
            self.cfg,
            coder_mode="auto",
            telegram=tg,
            git=FakeGit(),
            run_smoke=lambda cmd, cwd: (True, "ok"),
        )
        orch.handle_brief(target_brief)

        # Read the events back.
        events_path = self._tmp / "state" / "runs" / run_id / "events.jsonl"
        if not events_path.is_file():
            return []
        return [
            json.loads(ln)
            for ln in events_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def _strip_timing(self, ev: dict) -> dict:
        """Strip non-deterministic fields (ts, elapsed_ms) so two runs
        can be compared verbatim. Also strips data.duration_ms which
        comes from time.monotonic and varies, and the run_id field
        itself (different per run)."""
        out = {k: v for k, v in ev.items()
               if k not in ("ts", "elapsed_ms", "run_id")}
        if isinstance(out.get("data"), dict):
            d = {k: v for k, v in out["data"].items()
                 if k not in ("duration_ms",)}
            out["data"] = d
        return out

    def test_two_runs_byte_identical_modulo_timestamps(self) -> None:
        run1 = self._run_once("det-1")
        run2 = self._run_once("det-2")
        self.assertGreater(len(run1), 0, "first run produced no events")
        self.assertEqual(len(run1), len(run2),
                         f"event-count drift: {len(run1)} vs {len(run2)}")
        for i, (a, b) in enumerate(zip(run1, run2)):
            sa = self._strip_timing(a)
            sb = self._strip_timing(b)
            self.assertEqual(
                sa, sb,
                f"event {i} ({a.get('kind')}) differs between runs:\n  a={sa}\n  b={sb}",
            )


class TestMockedPerStageModel(_MockedTestBase):
    """v3 Phase 1a Step 1: MockedPlanner inherits the per-stage attribute
    shape (no __init__ override) and its overridden _call_anthropic reads
    the per-stage model — so a Stage-C override does not leak into Stage A
    or B across a full mock sweep (V3P0-3 parallel-wire)."""

    def test_mocked_inherits_per_stage_attrs_without_init_override(self) -> None:
        # MockedPlanner defines no __init__ of its own — it inherits
        # Planner's, so the per-stage attrs resolve identically (criterion 5).
        self.assertNotIn("__init__", MockedPlanner.__dict__)
        p = MockedPlanner(
            model="claude-opus-4-7", stage_c_model="claude-sonnet-4-6",
        )
        self.assertEqual(p.stage_a_model, "claude-opus-4-7")
        self.assertEqual(p.stage_b_model, "claude-opus-4-7")
        self.assertEqual(p.stage_c_model, "claude-sonnet-4-6")
        self.assertEqual(p._model_for_stage("C"), "claude-sonnet-4-6")

    def test_per_stage_no_leak_end_to_end_mock_sweep(self) -> None:
        os.environ["MOCKED_TASK_ID"] = "T1"
        brief, state = _brief_and_state("T1")
        p = MockedPlanner(
            model="claude-opus-4-7", stage_c_model="claude-sonnet-4-6",
        )
        # Stage A + B via the inherited plan_step (T1-step0 fixtures).
        p.plan_step(brief, state, 0)
        # Stage C via draft_completion_artefacts: with no T1-stepC fixture
        # the mock returns "" but STILL emits planner.stage_c.api_end
        # carrying route_actual — the same code path real-mode hits.
        p.draft_completion_artefacts(brief, state)

        evs = self._events()

        def ends(kind):
            return [e for e in evs if e["kind"] == kind]

        a = ends("planner.stage_a.api_end")
        b = ends("planner.stage_b.api_end")
        c = ends("planner.stage_c.api_end")
        self.assertTrue(a, "expected a Stage A api_end")
        self.assertTrue(b, "expected a Stage B api_end")
        self.assertTrue(c, "expected a Stage C api_end")
        # v3 Phase 1a Step 3 inversion: route_actual = policy placeholder
        # ("opus") on every stage; the `model` data field = what the API ran
        # (per-stage). A/B coincide (both opus); Stage C diverges (model=sonnet
        # from the per-stage override, route_actual=opus from the policy).
        for e in a + b:
            self.assertEqual(e["data"]["route_actual"], "claude-opus-4-7")
            self.assertEqual(e["data"]["model"], "claude-opus-4-7")
        for e in c:
            self.assertEqual(e["data"]["route_actual"], "claude-opus-4-7")   # policy
            self.assertEqual(e["data"]["model"], "claude-sonnet-4-6")        # per-stage ran

    def test_parallel_wire_policy_route_actual_and_policy_version(self) -> None:
        # v3 Phase 1a Step 3 (V3P0-3 parallel-wire): MockedPlanner's overridden
        # _call_anthropic routes through the inherited _policy_routing, so its
        # synthesised api_end + shadow.decision carry the policy's route_actual
        # and policy_version — identical to the production wrapper.
        os.environ["MOCKED_TASK_ID"] = "T1"
        p = MockedPlanner(model="claude-opus-4-7")
        p._current_step_idx = 0
        p._current_context_paths_count = 0
        p._call_anthropic(system="(sys)", user="(usr)", timeout=30,
                          step=1, stage="A")
        evs = self._events()
        end = next(e for e in evs
                   if e["kind"] == "planner.stage_a.api_end")["data"]
        self.assertEqual(end["route_actual"], "claude-opus-4-7")          # policy
        self.assertEqual(end["policy_version"], "v3-phase-1a-placeholder")
        self.assertFalse(end["route_fallback_fired"])
        sd = next(e for e in evs if e["kind"] == "shadow.decision")["data"]
        self.assertEqual(sd["policy_version"], "v3-phase-1a-placeholder")
        self.assertTrue(sd["agreement"])

    def test_canary_baseline_fires_on_mock(self) -> None:
        # v3 Phase 1b Step 3: MockedPlanner's _stage_a_canary_baseline override
        # reads the same fixture (model-agnostic) → silent_miss == 0
        # deterministically, exercising the canary wiring end-to-end on a free
        # mock run. The primary Stage A api_end shows the API "ran" Haiku.
        os.environ["MOCKED_TASK_ID"] = "T1"
        brief, state = _brief_and_state("T1")
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        p = MockedPlanner(
            model="claude-opus-4-7",
            policy=RoutingPolicy(PHASE_1B_STAGE_A_CANARY, calibration=cal),
        )
        p.plan_step(brief, state, 0)
        evs = self._events()
        baselines = [e for e in evs
                     if e["kind"] == "planner.stage_a.canary_baseline.api_end"]
        self.assertEqual(len(baselines), 1)
        end_a = next(e for e in evs
                     if e["kind"] == "planner.stage_a.api_end")["data"]
        self.assertEqual(end_a["route_actual"], CHEAP_STAGE_A_MODEL)
        self.assertEqual(end_a["model"], CHEAP_STAGE_A_MODEL)  # API "ran" Haiku
        ce = next(e for e in evs
                  if e["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(ce["silent_miss_count"], 0)

    def test_parallel_wire_shadow_divergence(self) -> None:
        # v3 Phase 1b Step 2 (V3P0-3 parallel-wire verification): MockedPlanner's
        # overridden _call_anthropic, via the inherited _policy_routing, emits the
        # SAME route_candidate divergence as the production wrapper when wired
        # with a PHASE_1B_STAGE_A_SHADOW calibrated policy — no mocked.py change.
        os.environ["MOCKED_TASK_ID"] = "T1"
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        p = MockedPlanner(
            model="claude-opus-4-7",
            policy=RoutingPolicy(PHASE_1B_STAGE_A_SHADOW, calibration=cal),
        )
        p._current_step_idx = 0
        p._current_context_paths_count = 0
        p._call_anthropic(system="(sys)", user="(usr)", timeout=30,
                          step=1, stage="A")
        evs = self._events()
        end = next(e for e in evs
                   if e["kind"] == "planner.stage_a.api_end")["data"]
        self.assertEqual(end["route_candidate"], CHEAP_STAGE_A_MODEL)  # diverges
        self.assertEqual(end["route_actual"], "claude-opus-4-7")       # per-stage
        self.assertEqual(end["model"], "claude-opus-4-7")
        self.assertEqual(end["policy_version"], "v3-phase-1b-stage-a-shadow")
        sd = next(e for e in evs if e["kind"] == "shadow.decision")["data"]
        self.assertEqual(sd["shadow_route_candidate"], CHEAP_STAGE_A_MODEL)
        self.assertEqual(sd["actual_route_taken"], "claude-opus-4-7")
        self.assertFalse(sd["agreement"])


if __name__ == "__main__":
    unittest.main()
