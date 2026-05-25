"""v2 Phase 1 Step 2 — Planner instrumentation tests.

Covers the event sequence emitted by `plan_step` (Stage A inline +
`_run_stage_b_with_retry`) and the `api_end` emit from inside
`_call_anthropic`. `_call_anthropic` is mocked at the method level so
no real Anthropic SDK call happens; the events emitted from inside it
(via `_events.emit`) are exercised by a small SDK shim that wires in
the usage dict the wrapper expects.

Hermetic write target: ANVIL_ROOT is redirected to a tmp dir; every
`events.jsonl` lands under it. Module-global event state is reset in
setUp so tests can't bleed.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anvil import events
from anvil import planner as planner_mod
from anvil.brief import Brief, Step
from anvil.calibration import CHEAP_STAGE_A_MODEL, RoutingCalibration
from anvil.lint import LintResult
from anvil.planner import Planner, DEFAULT_PLANNER_MODEL
from anvil.policy import (
    PHASE_1B_STAGE_A_CANARY,
    PHASE_1B_STAGE_A_SHADOW,
    RoutingPolicy,
)
from anvil.state import init_state


_FIX = Path(__file__).resolve().parent / "fixtures" / "planner"
_STAGE_A_VALID = (_FIX / "stage_a_valid.txt").read_text(encoding="utf-8")
_STAGE_B_VALID = (_FIX / "stage_b_valid_plan.txt").read_text(encoding="utf-8")
_STAGE_B_INVALID = (_FIX / "stage_b_invalid_then_valid_first.txt").read_text(
    encoding="utf-8"
)
_STAGE_B_VALID_SECOND = (
    _FIX / "stage_b_invalid_then_valid_second.txt"
).read_text(encoding="utf-8")
_STAGE_B_ESCALATION = (_FIX / "stage_b_escalation.txt").read_text(
    encoding="utf-8"
)


def _brief_and_state():
    brief = Brief(
        brief_version=1,
        project="anvil",
        build_name="events-test",
        target_repo="x",
        target_repo_path=Path("/tmp"),
        vps_deploy="no",
        steps=[
            Step(
                number=1,
                name="Example step",
                scope_files=["a.py", "b.py"],
                scope_operations=["write", "commit"],
                smoke="echo x",
                confirm="explicit",
            )
        ],
    )
    state = init_state(brief, "2026-05-20T00:00:00", brief_path="/nonexistent")
    return brief, state


class _PlannerEventsBase(unittest.TestCase):
    """Set up tmp ANVIL_ROOT and a clean events state per test."""

    def setUp(self) -> None:
        # Module-global state reset.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(
            os.environ, {"ANVIL_ROOT": str(self.tmp_path)}
        )
        self._env_patch.start()

        events.begin_run("planner-events-test")
        self.brief, self.state = _brief_and_state()
        self.planner = Planner(model="claude-opus-4-7-test")

    def tearDown(self) -> None:
        events.end_run()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _events_for(self, run_id: str = "planner-events-test") -> list[dict]:
        path = self.tmp_path / "state" / "runs" / run_id / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def _fake_call_with_usage(response_text: str, *, input_tokens: int = 1000,
                          output_tokens: int = 100):
    """Build a side_effect callable for `_call_anthropic` that emits a
    matching `planner.stage_<X>.api_end` event itself, mirroring the
    production wrapper's emit-from-inside contract."""
    def _side_effect(self, system, user, timeout, *, step, stage):
        # The production wrapper emits api_end with usage. Mirror that.
        events.emit(
            f"planner.stage_{stage.lower()}.api_end",
            {
                "step_idx": getattr(self, "_current_step_idx", None),
                "model": self._model_for_stage(stage),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "duration_ms": 250,
                "ok": True,
            },
            step_idx=getattr(self, "_current_step_idx", None),
        )
        return response_text
    return _side_effect


def _make_fake(responses_by_stage, *, input_tokens=1000, output_tokens=100):
    """Build an autospec-compatible _call_anthropic side_effect callable.

    Emits the stage_<X>.api_end event itself (mirroring the production
    wrapper's emit-from-inside contract) and returns the next-up
    fixture response for the matching stage letter.
    """
    ix = {"A": 0, "B": 0, "C": 0}

    def _side(self, system, user, timeout, *, step, stage):
        text = responses_by_stage[stage][ix[stage]]
        ix[stage] += 1
        events.emit(
            f"planner.stage_{stage.lower()}.api_end",
            {
                "step_idx": getattr(self, "_current_step_idx", None),
                "model": self._model_for_stage(stage),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "duration_ms": 100,
                "ok": True,
            },
            step_idx=getattr(self, "_current_step_idx", None),
        )
        return text
    return _side


class TestPlanStepHappyPath(_PlannerEventsBase):

    def test_full_sequence_emitted(self) -> None:
        side = _make_fake({"A": [_STAGE_A_VALID], "B": [_STAGE_B_VALID]})
        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=side,
        ):
            self.planner.plan_step(self.brief, self.state, 0)

        kinds = [e["kind"] for e in self._events_for()]
        planner_kinds = [k for k in kinds if k.startswith("planner.")]
        expected = [
            "planner.stage_a.start",
            "planner.stage_a.prompt_assembled",
            "planner.stage_a.api_start",
            "planner.stage_a.api_end",
            "planner.stage_a.parsed",
            "planner.stage_b.start",
            "planner.stage_b.files_loaded",
            "planner.stage_b.prompt_assembled",
            "planner.stage_b.api_start",
            "planner.stage_b.api_end",
            "planner.stage_b.parsed",
            "planner.validation.pass",
        ]
        self.assertEqual(planner_kinds, expected)

    def test_stage_a_api_end_carries_usage(self) -> None:
        side = _make_fake(
            {"A": [_STAGE_A_VALID], "B": [_STAGE_B_VALID]},
            input_tokens=42, output_tokens=7,
        )
        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=side,
        ):
            self.planner.plan_step(self.brief, self.state, 0)

        api_end = [
            e for e in self._events_for()
            if e["kind"] == "planner.stage_a.api_end"
        ][0]
        self.assertEqual(api_end["data"]["input_tokens"], 42)
        self.assertEqual(api_end["data"]["output_tokens"], 7)
        self.assertTrue(api_end["data"]["ok"])


class TestPlanStepRetryAndFailure(_PlannerEventsBase):

    def _patch_calls(self, responses_by_stage):
        """responses_by_stage: dict mapping stage letter to a list of
        responses to return in order. Each response text is paired with
        a synthesised api_end emit."""
        ix = {"A": 0, "B": 0, "C": 0}

        def _side(self, system, user, timeout, *, step, stage):
            text = responses_by_stage[stage][ix[stage]]
            ix[stage] += 1
            events.emit(
                f"planner.stage_{stage.lower()}.api_end",
                {
                    "step_idx": getattr(self, "_current_step_idx", None),
                    "model": self._model_for_stage(stage),
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "duration_ms": 100,
                    "ok": True,
                },
                step_idx=getattr(self, "_current_step_idx", None),
            )
            return text
        return mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=_side,
        )

    def test_validation_fail_then_retry_pass(self) -> None:
        with self._patch_calls({
            "A": [_STAGE_A_VALID],
            "B": [_STAGE_B_INVALID, _STAGE_B_VALID_SECOND],
        }):
            self.planner.plan_step(self.brief, self.state, 0)

        kinds = [e["kind"] for e in self._events_for()]
        # validation.fail must precede retry.start, retry.end follows,
        # second attempt yields validation.pass.
        idx_fail = kinds.index("planner.validation.fail")
        idx_retry_start = kinds.index("planner.retry.start")
        idx_retry_end = kinds.index("planner.retry.end")
        idx_pass = kinds.index("planner.validation.pass")
        self.assertLess(idx_fail, idx_retry_start)
        self.assertLess(idx_retry_start, idx_retry_end)
        self.assertLess(idx_retry_end, idx_pass)
        # Retry succeeded → second_error_or_none is None.
        retry_end = next(
            e for e in self._events_for()
            if e["kind"] == "planner.retry.end"
        )
        self.assertIsNone(retry_end["data"]["second_error_or_none"])

    def test_validation_fail_twice_emits_escalate(self) -> None:
        with self._patch_calls({
            "A": [_STAGE_A_VALID],
            # Both responses invalid (same fixture used twice).
            "B": [_STAGE_B_INVALID, _STAGE_B_INVALID],
        }):
            self.planner.plan_step(self.brief, self.state, 0)

        events_list = self._events_for()
        kinds = [e["kind"] for e in events_list]
        self.assertIn("planner.validation.fail", kinds)
        self.assertIn("planner.retry.end", kinds)
        self.assertIn("planner.escalate", kinds)
        retry_end = next(
            e for e in events_list if e["kind"] == "planner.retry.end"
        )
        self.assertIsNotNone(retry_end["data"]["second_error_or_none"])

    def test_stage_b_escalation_block_routes_to_planner_escalate(self) -> None:
        # Stage B model returns an escalation block (judgment-call shape).
        with self._patch_calls({
            "A": [_STAGE_A_VALID],
            "B": [_STAGE_B_ESCALATION],
        }):
            result = self.planner.plan_step(self.brief, self.state, 0)

        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("escalate"))
        escalates = [
            e for e in self._events_for()
            if e["kind"] == "planner.escalate"
        ]
        self.assertEqual(len(escalates), 1)
        self.assertIn("reason", escalates[0]["data"])

    def test_call_anthropic_api_failure_emits_api_end_ok_false(self) -> None:
        # Make the wrapper itself fall through to the generic-Exception
        # path: patch the underlying _attempt by patching anthropic.Anthropic
        # construction is overkill — easier to override _call_anthropic
        # with a side_effect that emits the api_end ok=False directly.
        def _side(self, system, user, timeout, *, step, stage):
            events.emit(
                f"planner.stage_{stage.lower()}.api_end",
                {
                    "step_idx": getattr(self, "_current_step_idx", None),
                    "model": self._model_for_stage(stage),
                    "ok": False,
                    "error": "simulated failure",
                },
                step_idx=getattr(self, "_current_step_idx", None),
            )
            return ""  # empty string = wrapper-level failure
        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=_side,
        ):
            self.planner.plan_step(self.brief, self.state, 0)

        api_ends = [
            e for e in self._events_for()
            if e["kind"].endswith(".api_end")
        ]
        self.assertTrue(api_ends)
        self.assertFalse(api_ends[0]["data"]["ok"])
        self.assertIn("error", api_ends[0]["data"])


class TestStageCArtefacts(_PlannerEventsBase):

    def test_draft_completion_artefacts_emits_stage_c_api_end(self) -> None:
        # Stage C is single-shot. The artefacts validator requires
        # checkpoint to start with a `#` markdown heading and setup_log
        # to start with `## ` — use a fixture that satisfies both so the
        # call doesn't retry and emit a second stage_c.api_end.
        artefacts_response = json.dumps({
            "setup_log_entry": "## 2026-05-20\n\nEntry.\n",
            "checkpoint": "# Checkpoint\n\nBody.\n",
        })

        def _side(self, system, user, timeout, *, step, stage):
            events.emit(
                f"planner.stage_{stage.lower()}.api_end",
                {
                    "step_idx": getattr(self, "_current_step_idx", None),
                    "model": self._model_for_stage(stage),
                    "input_tokens": 5000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "duration_ms": 1500,
                    "ok": True,
                },
                step_idx=getattr(self, "_current_step_idx", None),
            )
            return artefacts_response

        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=_side,
        ):
            self.planner.draft_completion_artefacts(self.brief, self.state)

        stage_c = [
            e for e in self._events_for()
            if e["kind"] == "planner.stage_c.api_end"
        ]
        self.assertEqual(len(stage_c), 1)
        self.assertEqual(stage_c[0]["data"]["input_tokens"], 5000)


def _make_fake_client(*, input_tokens, output_tokens, cache_creation,
                      cache_read, text="ok"):
    """v2 Phase 4 Step 1: a fake Anthropic client that exercises the REAL
    `_call_anthropic` (not mocked at the method level). Captures the
    kwargs passed to `messages.stream` (so the cache_control request shape
    can be asserted) and returns a streaming context manager whose
    `get_final_message()` carries a `usage` namespace with the cache
    columns. Returns (client, capture_dict)."""
    capture: dict = {}
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    final = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=usage,
    )
    stream_obj = mock.MagicMock()
    stream_obj.get_final_message.return_value = final
    cm = mock.MagicMock()
    cm.__enter__.return_value = stream_obj
    cm.__exit__.return_value = False

    def _stream(**kwargs):
        capture.update(kwargs)
        return cm

    client = mock.MagicMock()
    client.with_options.return_value = client  # .with_options(timeout=…) → self
    client.messages.stream.side_effect = _stream
    return client, capture


class TestCacheControl(_PlannerEventsBase):
    """v2 Phase 4 Step 1: cache_control on the shared system prompt +
    cache-column recording under the new (list-of-content-blocks) request
    shape. These exercise the real `_call_anthropic` against a fake
    streaming client."""

    def test_call_anthropic_passes_cache_control_on_system_prompt(self) -> None:
        client, capture = _make_fake_client(
            input_tokens=500, output_tokens=50,
            cache_creation=2603, cache_read=0,
        )
        self.planner._client = client
        out = self.planner._call_anthropic(
            system="SYSTEM-PROMPT-TEXT", user="u", timeout=30, step=1, stage="B",
        )
        self.assertEqual(out, "ok")
        # system is now a list of content blocks, with cache_control on the
        # (single, whole-file) system block.
        sysparam = capture["system"]
        self.assertIsInstance(sysparam, list)
        self.assertEqual(len(sysparam), 1)
        block = sysparam[0]
        self.assertEqual(block["type"], "text")
        self.assertEqual(block["text"], "SYSTEM-PROMPT-TEXT")
        self.assertEqual(block["cache_control"], {"type": "ephemeral"})
        # The user prompt is NOT cached (varies per call).
        self.assertEqual(capture["messages"], [{"role": "user", "content": "u"}])

    def test_call_anthropic_cache_creation_recorded(self) -> None:
        client, _ = _make_fake_client(
            input_tokens=500, output_tokens=50,
            cache_creation=2603, cache_read=0,
        )
        self.planner._client = client
        self.planner._call_anthropic(
            system="SYS", user="u", timeout=30, step=1, stage="B",
        )
        ends = [e for e in self._events_for()
                if e["kind"] == "planner.stage_b.api_end"]
        self.assertEqual(len(ends), 1)
        data = ends[0]["data"]
        self.assertEqual(data["cache_creation_input_tokens"], 2603)
        self.assertEqual(data["cache_read_input_tokens"], 0)
        self.assertEqual(data["input_tokens"], 500)

    def test_call_anthropic_cache_read_recorded(self) -> None:
        client, _ = _make_fake_client(
            input_tokens=500, output_tokens=50,
            cache_creation=0, cache_read=2603,
        )
        self.planner._client = client
        self.planner._call_anthropic(
            system="SYS", user="u", timeout=30, step=1, stage="B",
        )
        ends = [e for e in self._events_for()
                if e["kind"] == "planner.stage_b.api_end"]
        self.assertEqual(len(ends), 1)
        data = ends[0]["data"]
        self.assertEqual(data["cache_creation_input_tokens"], 0)
        self.assertEqual(data["cache_read_input_tokens"], 2603)


class TestRoutingObservability(_PlannerEventsBase):
    """v3 Phase 0 Step 1 (V3P0-1): the five routing fields fire from the
    real `_call_anthropic` emit site (success + error paths). Exercises
    the production wrapper against a fake streaming client."""

    def test_success_path_carries_five_routing_fields(self) -> None:
        client, _ = _make_fake_client(
            input_tokens=620, output_tokens=40, cache_creation=0, cache_read=2603,
        )
        self.planner._client = client
        # Mirror the stash plan_step / _run_stage_b_with_retry set.
        self.planner._current_step_idx = 1
        self.planner._current_context_paths_count = 5
        self.planner._call_anthropic(
            system="SYS", user="u", timeout=30, step=2, stage="B",
        )
        end = next(e for e in self._events_for()
                   if e["kind"] == "planner.stage_b.api_end")
        d = end["data"]
        # v3 Phase 1a Step 3: route_actual now sources from the policy
        # (placeholder → "claude-opus-4-7"), NOT the planner's per-stage model
        # "claude-opus-4-7-test"; policy_version flips to the Phase 1a stamp.
        # route_candidate still mirrors route_actual (placeholder shell).
        self.assertEqual(d["route_actual"], "claude-opus-4-7")
        self.assertEqual(d["route_candidate"], d["route_actual"])
        self.assertFalse(d["route_fallback_fired"])
        self.assertEqual(d["policy_version"], "v3-phase-1a-placeholder")
        fs = d["features_seen"]
        # observed_prompt_token_count is the API's input_tokens (620).
        self.assertEqual(fs["observed_prompt_token_count"], 620)
        self.assertEqual(fs["step_idx"], 1)
        self.assertEqual(fs["stage"], "B")
        self.assertEqual(fs["context_paths_count"], 5)

    def test_error_path_carries_fields_with_null_token_count(self) -> None:
        # A client whose stream() raises a non-retryable error → the
        # broad-Exception emit path (ok=False) still carries the fields.
        client = mock.MagicMock()
        client.with_options.return_value = client
        client.messages.stream.side_effect = RuntimeError("boom")
        self.planner._client = client
        self.planner._current_step_idx = 0
        self.planner._current_context_paths_count = 2
        out = self.planner._call_anthropic(
            system="SYS", user="u", timeout=30, step=1, stage="A",
        )
        self.assertEqual(out, "")  # wrapper-level failure returns empty
        end = next(e for e in self._events_for()
                   if e["kind"] == "planner.stage_a.api_end")
        d = end["data"]
        self.assertFalse(d["ok"])
        # v3 Phase 1a Step 3: route_actual + policy_version source from the
        # policy on the error path too (the wrapper still consults it).
        self.assertEqual(d["route_actual"], "claude-opus-4-7")
        self.assertEqual(d["policy_version"], "v3-phase-1a-placeholder")
        self.assertFalse(d["route_fallback_fired"])
        fs = d["features_seen"]
        # No usage on the error path → token count is None, keys still present.
        self.assertIsNone(fs["observed_prompt_token_count"])
        self.assertEqual(fs["stage"], "A")
        self.assertEqual(fs["context_paths_count"], 2)


class TestShadowDecisionPairing(_PlannerEventsBase):
    """v3 Phase 0 Step 2 (V3P0-3): every Planner stage call emits a
    shadow.decision immediately after its planner.stage_X.api_end, reusing
    that emit's features_seen + route_actual. Exercises the real
    _call_anthropic against a fake streaming client."""

    def test_shadow_decision_follows_api_end(self) -> None:
        client, _ = _make_fake_client(
            input_tokens=620, output_tokens=40, cache_creation=0, cache_read=2603,
        )
        self.planner._client = client
        self.planner._current_step_idx = 1
        self.planner._current_context_paths_count = 5
        self.planner._call_anthropic(
            system="SYS", user="u", timeout=30, step=2, stage="B",
        )
        evs = self._events_for()
        kinds = [e["kind"] for e in evs]
        # shadow.decision is emitted, immediately after the api_end.
        self.assertIn("shadow.decision", kinds)
        i_api = kinds.index("planner.stage_b.api_end")
        i_shadow = kinds.index("shadow.decision")
        self.assertEqual(i_shadow, i_api + 1)
        sd = evs[i_shadow]["data"]
        self.assertEqual(sd["stage"], "B")
        self.assertEqual(sd["shadow_route_candidate"], "claude-opus-4-7")
        # v3 Phase 1a Step 3: actual_route_taken now sources from the policy
        # decision (route_actual = placeholder opus), not the sentinel per-stage
        # model — so candidate == actual → agreement is True.
        self.assertEqual(sd["actual_route_taken"], "claude-opus-4-7")
        self.assertEqual(
            sd["agreement"],
            sd["shadow_route_candidate"] == sd["actual_route_taken"],
        )
        # v3 Phase 1b Step 3 (Step3B-F3): shadow_decision_basis is the PRE-call
        # merged features the policy decided on, so its observed_prompt_token_count
        # is None (the count isn't known until after the call); the api_end's
        # features_seen carries the real post-call count. They agree on every
        # other key.
        basis = sd["shadow_decision_basis"]
        fs = evs[i_api]["data"]["features_seen"]
        self.assertIsNone(basis["observed_prompt_token_count"])
        self.assertEqual(fs["observed_prompt_token_count"], 620)
        for k in ("stage", "step_idx", "context_paths_count"):
            self.assertEqual(basis[k], fs[k])

    def test_one_shadow_per_planner_stage_in_full_plan_step(self) -> None:
        # A full plan_step (Stage A + Stage B) → 2 api_end → 2 shadow rows.
        side = _make_fake({"A": [_STAGE_A_VALID], "B": [_STAGE_B_VALID]})

        def _side_with_shadow(self, system, user, timeout, *, step, stage):
            # The fake _call_anthropic must also emit the paired shadow,
            # mirroring the production wrapper (which the method-level mock
            # bypasses). Reuse the production helper for fidelity.
            text = side(self, system, user, timeout, step=step, stage=stage)
            events.emit_shadow_decision(
                stage=stage,
                step_idx=getattr(self, "_current_step_idx", None),
                features_seen=events._compute_features_seen(
                    stage, getattr(self, "_current_step_idx", None), 100,
                    getattr(self, "_current_context_paths_count", None),
                ),
                actual_route_taken=self._model_for_stage(stage),
            )
            return text

        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True,
            side_effect=_side_with_shadow,
        ):
            self.planner.plan_step(self.brief, self.state, 0)
        shadow = [e for e in self._events_for()
                  if e["kind"] == "shadow.decision"]
        self.assertEqual(len(shadow), 2)
        self.assertEqual({s["data"]["stage"] for s in shadow}, {"A", "B"})


class TestStageAComparatorInPlanStep(_PlannerEventsBase):
    """v3 Phase 0 Step 3 (V3P0-4): plan_step fires the comparator after
    _parse_stage_a_response — a shadow_compare.begin/end pair per Stage A
    call, in the same step (not literal adjacency to api_end)."""

    def test_comparator_pair_fires_per_stage_a_call(self) -> None:
        side = _make_fake({"A": [_STAGE_A_VALID], "B": [_STAGE_B_VALID]})
        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=side,
        ):
            self.planner.plan_step(self.brief, self.state, 0)
        kinds = [e["kind"] for e in self._events_for()]
        # Exactly one begin + one end for the single Stage A call.
        self.assertEqual(kinds.count("stage_a.shadow_compare.begin"), 1)
        self.assertEqual(kinds.count("stage_a.shadow_compare.end"), 1)
        # Same-step pairing: shadow_compare follows stage_a.parsed (not
        # immediately after api_end — parser/parsed sit between).
        i_parsed = kinds.index("planner.stage_a.parsed")
        i_begin = kinds.index("stage_a.shadow_compare.begin")
        i_end = kinds.index("stage_a.shadow_compare.end")
        self.assertLess(i_parsed, i_begin)
        self.assertLess(i_begin, i_end)

    def test_comparator_identity_silent_miss_zero(self) -> None:
        # routed == baseline (Phase 0) → silent_miss 0, jaccard 1.0, and
        # silent_miss.detected never fires.
        side = _make_fake({"A": [_STAGE_A_VALID], "B": [_STAGE_B_VALID]})
        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=side,
        ):
            self.planner.plan_step(self.brief, self.state, 0)
        evs = self._events_for()
        end = next(e for e in evs
                   if e["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(end["silent_miss_count"], 0)
        self.assertEqual(end["hallucination_count"], 0)
        self.assertEqual(end["jaccard_similarity"], 1.0)
        self.assertNotIn("stage_a.silent_miss.detected",
                         [e["kind"] for e in evs])


class TestCacheFamilyDiagnostics(_PlannerEventsBase):
    """v3 Phase 0 Step 4 (V3P0-6): vault_index_hit memoisation, null on
    Stage B, candidate block sizes, and the TTL field logic."""

    def test_vault_index_hit_false_first_call_true_second(self) -> None:
        # Two plan_step calls on the SAME Planner + run_id: the first
        # builds the index (hit=false), the second reuses it (hit=true).
        # Drive the real _call_anthropic via a fake streaming client so
        # the wrapper's cache_diag emit runs. Stage B fails to parse
        # text="ok" and escalates — irrelevant; the Stage A api_end fires.
        client, _ = _make_fake_client(
            input_tokens=620, output_tokens=40, cache_creation=2603, cache_read=0,
        )
        self.planner._client = client
        self.planner.plan_step(self.brief, self.state, 0)
        self.planner.plan_step(self.brief, self.state, 0)
        stage_a = [e for e in self._events_for()
                   if e["kind"] == "planner.stage_a.api_end"]
        self.assertEqual(len(stage_a), 2)
        self.assertIs(stage_a[0]["data"]["vault_index_hit"], False)
        self.assertIs(stage_a[1]["data"]["vault_index_hit"], True)
        # Block sizes populated and sum positive on both.
        for e in stage_a:
            bs = e["data"]["candidate_user_block_sizes"]
            self.assertEqual(set(bs),
                             {"brief", "state", "vault_files", "prior_step"})
            self.assertGreater(sum(bs.values()), 0)

    def test_stage_b_vault_index_hit_is_null(self) -> None:
        client, _ = _make_fake_client(
            input_tokens=620, output_tokens=40, cache_creation=0, cache_read=2603,
        )
        self.planner._client = client
        self.planner.plan_step(self.brief, self.state, 0)
        stage_b = [e for e in self._events_for()
                   if e["kind"] == "planner.stage_b.api_end"]
        self.assertTrue(stage_b)
        for e in stage_b:
            # Q(c): null on Stage B — the question doesn't apply.
            self.assertIsNone(e["data"]["vault_index_hit"])
            # The other two fields still populate.
            self.assertIn("candidate_user_block_sizes", e["data"])
            self.assertIn("seconds_since_cache_creation", e["data"])

    def test_ttl_null_on_creation_positive_on_read(self) -> None:
        # _cache_diag_fields TTL logic: a cache_creation call → null +
        # records the timestamp; a subsequent read call → positive delta.
        p = Planner(model="claude-opus-4-7")
        first = p._cache_diag_fields("A", cache_creation_tokens=2603)
        self.assertIsNone(first["seconds_since_cache_creation"])
        second = p._cache_diag_fields("B", cache_creation_tokens=0)
        self.assertIsNotNone(second["seconds_since_cache_creation"])
        self.assertGreaterEqual(second["seconds_since_cache_creation"], 0.0)
        self.assertLess(second["seconds_since_cache_creation"], 300)

    def test_ttl_null_before_any_creation(self) -> None:
        # A read call before any creation has been seen → null.
        p = Planner(model="claude-opus-4-7")
        d = p._cache_diag_fields("B", cache_creation_tokens=0)
        self.assertIsNone(d["seconds_since_cache_creation"])

    def test_cache_diag_fields_vault_hit_null_on_b_and_c(self) -> None:
        p = Planner(model="claude-opus-4-7")
        p._current_vault_index_hit = True  # would apply only to Stage A
        self.assertIs(p._cache_diag_fields("A", 0)["vault_index_hit"], True)
        self.assertIsNone(p._cache_diag_fields("B", 0)["vault_index_hit"])
        self.assertIsNone(p._cache_diag_fields("C", 0)["vault_index_hit"])


class TestPerStageModelPlumbing(_PlannerEventsBase):
    """v3 Phase 1a Step 1: per-stage model plumbing — constructor
    resolution, the _model_for_stage dispatch helper, and the Stage-C-
    routed-differently no-leak guarantee on BOTH the emitted route_actual
    AND the model handed to client.messages.stream."""

    def test_single_model_sets_all_three_stages(self) -> None:
        p = Planner(model="claude-opus-4-7")
        self.assertEqual(p.stage_a_model, "claude-opus-4-7")
        self.assertEqual(p.stage_b_model, "claude-opus-4-7")
        self.assertEqual(p.stage_c_model, "claude-opus-4-7")

    def test_per_stage_kwargs_override_each_stage(self) -> None:
        p = Planner(
            model="base-model",
            stage_a_model="model-a",
            stage_b_model="model-b",
            stage_c_model="model-c",
        )
        self.assertEqual(p.stage_a_model, "model-a")
        self.assertEqual(p.stage_b_model, "model-b")
        self.assertEqual(p.stage_c_model, "model-c")

    def test_partial_per_stage_defaults_unset_stages(self) -> None:
        # No model= given; only Stage C overridden → A/B fall back to
        # DEFAULT_PLANNER_MODEL, C takes the override (the rehearsal shape).
        p = Planner(stage_c_model="claude-sonnet-4-6")
        self.assertEqual(p.stage_a_model, DEFAULT_PLANNER_MODEL)
        self.assertEqual(p.stage_b_model, DEFAULT_PLANNER_MODEL)
        self.assertEqual(p.stage_c_model, "claude-sonnet-4-6")

    def test_all_stages_default_to_default_planner_model(self) -> None:
        p = Planner()
        self.assertEqual(p.stage_a_model, DEFAULT_PLANNER_MODEL)
        self.assertEqual(p.stage_b_model, DEFAULT_PLANNER_MODEL)
        self.assertEqual(p.stage_c_model, DEFAULT_PLANNER_MODEL)
        self.assertEqual(DEFAULT_PLANNER_MODEL, "claude-opus-4-7")

    def test_model_for_stage_dispatches_per_stage(self) -> None:
        p = Planner(
            model="base-model",
            stage_a_model="model-a",
            stage_b_model="model-b",
            stage_c_model="model-c",
        )
        self.assertEqual(p._model_for_stage("A"), "model-a")
        self.assertEqual(p._model_for_stage("B"), "model-b")
        self.assertEqual(p._model_for_stage("C"), "model-c")

    def test_model_for_stage_bad_stage_raises_keyerror(self) -> None:
        p = Planner(model="claude-opus-4-7")
        with self.assertRaises(KeyError):
            p._model_for_stage("Z")

    def test_stage_c_routes_without_leaking_into_a_or_b(self) -> None:
        # Step 1's no-leak proof, updated for Step 3's inversion. The API-call
        # no-leak axis (the mock-client kwarg) is unchanged and remains
        # load-bearing: Stage C's per-stage override reaches the API and does
        # not leak into A/B. The route_actual axis now proves POLICY-sourcing:
        # route_actual = the placeholder ("opus") on every stage, while the
        # `model` data field = what the API ran (per-stage). They DIVERGE for
        # Stage C here (model=sonnet, route_actual=opus) — that divergence IS
        # the Step3-F1 inversion ("model" = ran, "route_actual" = decided).
        client, capture = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0, cache_read=2603,
        )
        p = Planner(model="claude-opus-4-7", stage_c_model="claude-sonnet-4-6")
        p._client = client
        p._current_step_idx = None
        p._current_context_paths_count = 0

        # Stage C: API call + model field = sonnet (per-stage, what ran);
        # route_actual = opus (policy placeholder, what the router decided).
        p._call_anthropic(system="SYS", user="u", timeout=120, step=0, stage="C")
        self.assertEqual(capture["model"], "claude-sonnet-4-6")          # API ran
        end_c = next(e for e in self._events_for()
                     if e["kind"] == "planner.stage_c.api_end")
        self.assertEqual(end_c["data"]["model"], "claude-sonnet-4-6")    # ran
        self.assertEqual(end_c["data"]["route_actual"], "claude-opus-4-7")  # decided

        # Stage A → opus, no leak from the Stage C override.
        p._current_step_idx = 0
        p._call_anthropic(system="SYS", user="u", timeout=30, step=1, stage="A")
        self.assertEqual(capture["model"], "claude-opus-4-7")
        end_a = next(e for e in self._events_for()
                     if e["kind"] == "planner.stage_a.api_end")
        self.assertEqual(end_a["data"]["route_actual"], "claude-opus-4-7")
        self.assertEqual(end_a["data"]["model"], "claude-opus-4-7")

        # Stage B → opus, no leak.
        p._call_anthropic(system="SYS", user="u", timeout=30, step=1, stage="B")
        self.assertEqual(capture["model"], "claude-opus-4-7")
        end_b = next(e for e in self._events_for()
                     if e["kind"] == "planner.stage_b.api_end")
        self.assertEqual(end_b["data"]["route_actual"], "claude-opus-4-7")
        self.assertEqual(end_b["data"]["model"], "claude-opus-4-7")


class TestPolicyEngineWiring(_PlannerEventsBase):
    """v3 Phase 1a Step 3: the RoutingPolicy is wired into _call_anthropic.
    route_actual sources from the policy decision (placeholder → Opus); the
    API call + model data field stay per-stage; policy_version stamps the
    routing event and the shadow.decision; decision_basis is the lint+features
    merge (lint wins)."""

    def _drive_success(self, *, stage="B", step=2, input_tokens=620):
        client, capture = _make_fake_client(
            input_tokens=input_tokens, output_tokens=40,
            cache_creation=0, cache_read=2603,
        )
        self.planner._client = client
        self.planner._current_step_idx = step - 1
        self.planner._current_context_paths_count = 5
        self.planner._call_anthropic(
            system="SYS", user="u", timeout=30, step=step, stage=stage,
        )
        return capture

    def test_route_actual_sources_from_policy_not_per_stage(self) -> None:
        # The planner's model is the sentinel "claude-opus-4-7-test", but
        # route_actual = the policy placeholder "claude-opus-4-7". The `model`
        # data field = the sentinel (what the API ran). The inversion.
        self._drive_success(stage="B")
        d = next(e for e in self._events_for()
                 if e["kind"] == "planner.stage_b.api_end")["data"]
        self.assertEqual(d["route_actual"], "claude-opus-4-7")          # policy
        self.assertEqual(d["model"], "claude-opus-4-7-test")            # ran

    def test_policy_route_actual_independent_of_per_stage_model(self) -> None:
        # route_actual = the placeholder regardless of the per-stage model;
        # the API call kwarg + model field track the per-stage model.
        client, capture = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0, cache_read=2603,
        )
        p = Planner(model="model-x", stage_c_model="model-y")
        p._client = client
        p._current_step_idx = None
        p._current_context_paths_count = 0
        p._call_anthropic(system="SYS", user="u", timeout=120, step=0, stage="C")
        end_c = next(e for e in self._events_for()
                     if e["kind"] == "planner.stage_c.api_end")["data"]
        self.assertEqual(capture["model"], "model-y")          # API ran per-stage
        self.assertEqual(end_c["model"], "model-y")            # data field = ran
        self.assertEqual(end_c["route_actual"], "claude-opus-4-7")    # policy
        self.assertEqual(end_c["route_candidate"], "claude-opus-4-7")

    def test_policy_version_stamped_on_api_end(self) -> None:
        self._drive_success(stage="A", step=1)
        d = next(e for e in self._events_for()
                 if e["kind"] == "planner.stage_a.api_end")["data"]
        self.assertEqual(d["policy_version"], "v3-phase-1a-placeholder")
        self.assertFalse(d["route_fallback_fired"])

    def test_policy_version_stamped_on_shadow_event(self) -> None:
        self._drive_success(stage="A", step=1)
        sd = next(e for e in self._events_for()
                  if e["kind"] == "shadow.decision")["data"]
        self.assertEqual(sd["policy_version"], "v3-phase-1a-placeholder")
        # Placeholder shell: candidate == actual → agreement True.
        self.assertEqual(sd["shadow_route_candidate"], sd["actual_route_taken"])
        self.assertTrue(sd["agreement"])

    def test_decision_basis_merges_lint_features_lint_wins(self) -> None:
        # Stash a lint result with a colliding key (context_paths_count) and a
        # lint-only key (brief_token_estimate). The merge into decision_basis
        # must let lint WIN on the collision and carry both feature sets.
        self.planner._current_lint_result = LintResult(
            structured_features={
                "context_paths_count": 999, "brief_token_estimate": 50,
            },
        )
        self._drive_success(stage="B", step=2)  # sets _current_context_paths_count=5
        sd = next(e for e in self._events_for()
                  if e["kind"] == "shadow.decision")["data"]
        basis = sd["shadow_decision_basis"]
        self.assertEqual(basis["context_paths_count"], 999)   # lint wins collision
        self.assertEqual(basis["brief_token_estimate"], 50)   # lint-only key
        self.assertEqual(basis["stage"], "B")                 # features_seen key
        # v3 Phase 1b Step 3 (Step3B-F3): the decision is made PRE-call, so the
        # basis's observed_prompt_token_count is None (the api_end's features_seen
        # carries the real post-call count).
        self.assertIsNone(basis["observed_prompt_token_count"])

    def test_decision_basis_without_lint_is_features_seen_only(self) -> None:
        # No _current_lint_result stashed → only Phase 0 features_seen in basis;
        # context_paths_count keeps its features_seen value (not overridden).
        self.assertIsNone(getattr(self.planner, "_current_lint_result", None))
        self._drive_success(stage="B", step=2)
        sd = next(e for e in self._events_for()
                  if e["kind"] == "shadow.decision")["data"]
        basis = sd["shadow_decision_basis"]
        self.assertEqual(basis["context_paths_count"], 5)     # features_seen value
        self.assertNotIn("brief_token_estimate", basis)       # no lint keys
        self.assertEqual(
            set(basis),
            {"observed_prompt_token_count", "step_idx", "stage",
             "context_paths_count"},
        )


class TestPhase1bStageAShadowWiring(_PlannerEventsBase):
    """v3 Phase 1b Step 2: the Stage A shadow rule diverges route_candidate to
    Haiku through the REAL _call_anthropic, while route_actual + the `model` data
    field + the API-call kwarg stay Opus (the first route_candidate ≠ route_actual
    in v3 history; Step3-F1 inversion preserved)."""

    def _shadow_planner(self):
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        return Planner(
            model="claude-opus-4-7",
            policy=RoutingPolicy(PHASE_1B_STAGE_A_SHADOW, calibration=cal),
        )

    def test_stage_a_shadow_diverges_candidate_through_call_anthropic(self) -> None:
        client, capture = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0, cache_read=2603)
        p = self._shadow_planner()
        p._client = client
        p._current_step_idx = 0
        p._current_context_paths_count = 0  # empty context → cheap-route recommended
        p._call_anthropic(system="SYS", user="u", timeout=30, step=1, stage="A")
        end = next(e for e in self._events_for()
                   if e["kind"] == "planner.stage_a.api_end")["data"]
        # route_candidate diverges to Haiku; route_actual + model + API stay Opus.
        self.assertEqual(end["route_candidate"], CHEAP_STAGE_A_MODEL)
        self.assertEqual(end["route_actual"], "claude-opus-4-7")
        self.assertEqual(end["model"], "claude-opus-4-7")
        self.assertEqual(capture["model"], "claude-opus-4-7")  # API ran Opus
        self.assertEqual(end["policy_version"], "v3-phase-1b-stage-a-shadow")
        # The paired shadow.decision records the divergence → agreement False.
        sd = next(e for e in self._events_for()
                  if e["kind"] == "shadow.decision")["data"]
        self.assertEqual(sd["shadow_route_candidate"], CHEAP_STAGE_A_MODEL)
        self.assertEqual(sd["actual_route_taken"], "claude-opus-4-7")
        self.assertFalse(sd["agreement"])

    def test_stage_b_no_divergence_under_shadow(self) -> None:
        client, _ = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0, cache_read=2603)
        p = self._shadow_planner()
        p._client = client
        p._current_step_idx = 0
        p._current_context_paths_count = 0
        p._call_anthropic(system="SYS", user="u", timeout=30, step=1, stage="B")
        end = next(e for e in self._events_for()
                   if e["kind"] == "planner.stage_b.api_end")["data"]
        self.assertEqual(end["route_candidate"], "claude-opus-4-7")
        self.assertEqual(end["route_actual"], "claude-opus-4-7")


class TestPhase1bStageACanaryWiring(_PlannerEventsBase):
    """v3 Phase 1b Step 3: the canary makes the API ACTUALLY run Haiku — the
    pre-call decision restructure (api_model = decision.route_actual under the
    canary) and the parallel-Opus baseline in plan_step."""

    def _canary_planner(self):
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        return Planner(
            model="claude-opus-4-7",
            policy=RoutingPolicy(PHASE_1B_STAGE_A_CANARY, calibration=cal),
        )

    def test_canary_api_call_runs_haiku(self) -> None:
        client, capture = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0, cache_read=2603)
        p = self._canary_planner()
        p._client = client
        p._current_step_idx = 0
        p._current_context_paths_count = 0
        p._call_anthropic(system="SYS", user="u", timeout=30, step=1, stage="A")
        # The API ACTUALLY ran Haiku — the Step3-F1 swap, now production.
        self.assertEqual(capture["model"], CHEAP_STAGE_A_MODEL)
        end = next(e for e in self._events_for()
                   if e["kind"] == "planner.stage_a.api_end")["data"]
        self.assertEqual(end["model"], CHEAP_STAGE_A_MODEL)        # model = ran
        self.assertEqual(end["route_actual"], CHEAP_STAGE_A_MODEL)
        self.assertEqual(end["route_candidate"], CHEAP_STAGE_A_MODEL)
        self.assertEqual(end["policy_version"], "v3-phase-1b-stage-a-canary")

    def test_canary_stage_b_still_opus_no_leak(self) -> None:
        client, capture = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0, cache_read=2603)
        p = self._canary_planner()
        p._client = client
        p._current_step_idx = 0
        p._current_context_paths_count = 0
        p._call_anthropic(system="SYS", user="u", timeout=30, step=1, stage="B")
        self.assertEqual(capture["model"], "claude-opus-4-7")  # Stage B unchanged
        end = next(e for e in self._events_for()
                   if e["kind"] == "planner.stage_b.api_end")["data"]
        self.assertEqual(end["model"], "claude-opus-4-7")
        self.assertEqual(end["route_actual"], "claude-opus-4-7")

    def test_plan_step_canary_baseline_fires_silent_miss_zero(self) -> None:
        # A full plan_step on a canary planner, empty context: the primary
        # Stage A runs Haiku; the parallel-Opus baseline fires; both select
        # nothing (text="" → empty selection) → silent_miss == 0, and one
        # canary_baseline.api_end is emitted (the comparator's ground truth).
        client, _ = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0,
            cache_read=2603, text="")
        p = self._canary_planner()
        p._client = client
        p.plan_step(self.brief, self.state, 0)
        evs = self._events_for()
        baselines = [e for e in evs
                     if e["kind"] == "planner.stage_a.canary_baseline.api_end"]
        self.assertEqual(len(baselines), 1)  # the parallel Opus baseline fired
        ce = next(e for e in evs
                  if e["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(ce["silent_miss_count"], 0)  # Haiku == Opus (both empty)


class _FakeBaseline:
    """Test double for HistoricalBaselineProvider: returns a fixed lookup
    result (a list = hit, None = miss) regardless of (task_id, step_idx)."""

    def __init__(self, result, corpus=frozenset()):
        self._result = result
        self._corpus = corpus

    def lookup(self, task_id, step_idx):
        return self._result

    def corpus_distinct_paths(self):
        # v3 Phase 3 3b (β-i): the planner calls this on every plan_step. Default
        # empty → corpus_baselines=None → the single-row K-check (today's
        # behaviour), so these empty-context canary tests are unperturbed.
        return self._corpus


class TestPhase1cHistoricalBaseline(_PlannerEventsBase):
    """v3 Phase 1c Step 3 (V3P1C-3): comparator option-(b). The canary's
    silent-miss baseline comes from a historical DB lookup (reconstruct-∅ from
    paths_returned=0, Step3C-F1) instead of a live parallel-Opus call; the
    parallel call is the fallback only on a lookup miss (Step3C-F1 / Q3a)."""

    def _make_baseline_db(self, rows):
        # rows: list of (run_id, step_idx, paths_returned)
        import duckdb
        path = Path(tempfile.mkdtemp()) / "baseline.duckdb"
        con = duckdb.connect(str(path))
        con.execute(
            "CREATE TABLE events (run_id VARCHAR, mode VARCHAR, "
            "step_idx BIGINT, kind VARCHAR, data JSON)")
        for (run_id, step_idx, pr) in rows:
            con.execute(
                "INSERT INTO events VALUES (?, 'real', ?, "
                "'planner.stage_a.parsed', ?)",
                [run_id, step_idx,
                 json.dumps({"step_idx": step_idx, "paths_returned": pr})])
        con.close()
        return path

    def _canary_planner(self, historical=None):
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        return Planner(
            model="claude-opus-4-7",
            policy=RoutingPolicy(PHASE_1B_STAGE_A_CANARY, calibration=cal),
            historical_baseline=historical,
        )

    # --- provider lookup (Step3C-F1 reconstruction) ---

    def test_lookup_reconstructs_empty_from_zero(self) -> None:
        # Q9.1: paths_returned=0 → [] (∅, exact for empty-context).
        db = self._make_baseline_db([("T1-doc-edit-real", 0, 0)])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.lookup("T1-doc-edit", 0), [])

    def test_lookup_missing_row_returns_none(self) -> None:
        # Q9.2: no matching (run_id, step_idx) → None (miss → fallback).
        db = self._make_baseline_db([("T1-doc-edit-real", 0, 0)])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertIsNone(prov.lookup("T9-absent", 0))
        self.assertIsNone(prov.lookup("T1-doc-edit", 5))

    def test_lookup_nonzero_returns_none(self) -> None:
        # Q9.3: paths_returned>0 → None (the path list isn't recorded; can't
        # reconstruct the set — Step3C-F3 Phase 2 prerequisite).
        db = self._make_baseline_db([("T2-two-step-real", 0, 3)])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertIsNone(prov.lookup("T2-two-step", 0))

    def test_provider_never_raises_on_bad_db(self) -> None:
        # Q9.9: missing/None DB path → lookup returns None, never raises.
        self.assertIsNone(
            planner_mod.HistoricalBaselineProvider("/no/such/file.duckdb").lookup("X", 0))
        self.assertIsNone(planner_mod.HistoricalBaselineProvider(None).lookup("X", 0))

    # --- plan_step integration (baseline_source + parallel fallback) ---

    def test_canary_historical_hit_no_parallel(self) -> None:
        # Q9.5: a historical hit (∅) → no parallel call, no canary_baseline
        # emit, baseline_source="historical", silent_miss=0.
        client, _ = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0,
            cache_read=2603, text="")
        p = self._canary_planner(historical=_FakeBaseline([]))
        p._client = client
        p.plan_step(self.brief, self.state, 0)
        evs = self._events_for()
        baselines = [e for e in evs
                     if e["kind"] == "planner.stage_a.canary_baseline.api_end"]
        self.assertEqual(len(baselines), 0)  # historical hit → no parallel Opus
        ce = next(e for e in evs
                  if e["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(ce["baseline_source"], "historical")
        self.assertEqual(ce["silent_miss_count"], 0)

    def test_canary_historical_miss_falls_back_to_parallel(self) -> None:
        # Q9.4: a miss (None) → the parallel-Opus baseline fires (option-a),
        # baseline_source="parallel", one canary_baseline.api_end emitted.
        client, _ = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0,
            cache_read=2603, text="")
        p = self._canary_planner(historical=_FakeBaseline(None))
        p._client = client
        p.plan_step(self.brief, self.state, 0)
        evs = self._events_for()
        baselines = [e for e in evs
                     if e["kind"] == "planner.stage_a.canary_baseline.api_end"]
        self.assertEqual(len(baselines), 1)  # miss → parallel fallback fired
        ce = next(e for e in evs
                  if e["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(ce["baseline_source"], "parallel")
        self.assertEqual(ce["silent_miss_count"], 0)

    def test_baseline_source_identity_on_non_canary(self) -> None:
        # Q9.6: a non-canary (shadow) plan_step → baseline == routed →
        # baseline_source="identity".
        client, _ = _make_fake_client(
            input_tokens=600, output_tokens=40, cache_creation=0,
            cache_read=2603, text="")
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        p = Planner(
            model="claude-opus-4-7",
            policy=RoutingPolicy(PHASE_1B_STAGE_A_SHADOW, calibration=cal))
        p._client = client
        p.plan_step(self.brief, self.state, 0)
        ce = next(e for e in self._events_for()
                  if e["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(ce["baseline_source"], "identity")


class TestPhase2cProviderSelectedPaths(unittest.TestCase):
    """v3 Phase 2c Step 2 (V3P2C-2, Q-A4 discharge): HistoricalBaselineProvider
    reads `selected_paths` directly (Phase 2a+ baseline DBs, rich-context-ready),
    falling back to `paths_returned`-reconstruction for legacy DBs that predate
    Phase 2a recording. Empty-context behaviour identical on both paths."""

    def _make_db(self, rows):
        # rows: list of (run_id, step_idx, data_dict) — the test controls the
        # exact `data` shape (with/without selected_paths) to exercise both
        # the Phase 2a+ direct-read path and the Phase 1a/1b legacy fallback.
        import duckdb
        path = Path(tempfile.mkdtemp()) / "baseline.duckdb"
        con = duckdb.connect(str(path))
        con.execute(
            "CREATE TABLE events (run_id VARCHAR, mode VARCHAR, "
            "step_idx BIGINT, kind VARCHAR, data JSON)")
        for (run_id, step_idx, data) in rows:
            con.execute(
                "INSERT INTO events VALUES (?, 'real', ?, "
                "'planner.stage_a.parsed', ?)",
                [run_id, step_idx, json.dumps(data)])
        con.close()
        return path

    def test_empty_context_phase2a_path_returns_empty(self) -> None:
        # Phase 2a+ baseline: selected_paths=[] recorded → returns [] directly.
        db = self._make_db([("T1-doc-edit-real", 0,
                             {"step_idx": 0, "paths_returned": 0,
                              "selected_paths": []})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.lookup("T1-doc-edit", 0), [])

    def test_empty_context_legacy_path_returns_empty(self) -> None:
        # Legacy DB (Phase 1a/1b): no selected_paths key, paths_returned=0 →
        # fallback reconstruction → [] (behaviour identical to pre-switch).
        db = self._make_db([("T1-doc-edit-real", 0,
                             {"step_idx": 0, "paths_returned": 0})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.lookup("T1-doc-edit", 0), [])

    def test_rich_context_phase2a_path_returns_list_order_preserved(self) -> None:
        # The forward-readiness proof: a non-empty selected_paths reconstructs
        # EXACTLY (content + order), which the legacy count path cannot do.
        sel = ["docs/c.md", "docs/a.md", "docs/b.md"]  # deliberately unsorted
        db = self._make_db([("T2-two-step-real", 0,
                             {"step_idx": 0, "paths_returned": 3,
                              "selected_paths": sel})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.lookup("T2-two-step", 0), sel)  # content + order

    def test_rich_context_legacy_path_returns_none(self) -> None:
        # Legacy DB, paths_returned>0, no selected_paths → None (can't
        # reconstruct a list from a count — the failure mode the switch fixes;
        # caller falls back to the parallel-Opus baseline).
        db = self._make_db([("T2-two-step-real", 0,
                             {"step_idx": 0, "paths_returned": 3})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertIsNone(prov.lookup("T2-two-step", 0))

    def test_selected_paths_json_null_falls_back_empty(self) -> None:
        # Defensive (partial-migration): selected_paths recorded as JSON null →
        # falls back to paths_returned reconstruction (0 → []).
        db = self._make_db([("T1-doc-edit-real", 0,
                             {"step_idx": 0, "paths_returned": 0,
                              "selected_paths": None})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.lookup("T1-doc-edit", 0), [])

    def test_selected_paths_json_null_nonzero_falls_back_none(self) -> None:
        # Defensive: selected_paths JSON null + paths_returned>0 → fall back →
        # None (can't reconstruct from count).
        db = self._make_db([("T2-two-step-real", 0,
                             {"step_idx": 0, "paths_returned": 3,
                              "selected_paths": None})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertIsNone(prov.lookup("T2-two-step", 0))

    def test_phase2a_path_preferred_over_count_mismatch(self) -> None:
        # selected_paths is authoritative when present: a (hypothetical) row
        # where the count disagrees still returns the recorded list, not a
        # count-based reconstruction.
        db = self._make_db([("T3-out-of-scope-real", 1,
                             {"step_idx": 1, "paths_returned": 0,
                              "selected_paths": ["x/y.md"]})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.lookup("T3-out-of-scope", 1), ["x/y.md"])

    def test_missing_row_and_bad_db_still_none(self) -> None:
        # Regression: the never-raise contract holds through the switch.
        db = self._make_db([("T1-doc-edit-real", 0,
                             {"step_idx": 0, "paths_returned": 0,
                              "selected_paths": []})])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertIsNone(prov.lookup("T9-absent", 0))      # missing row
        self.assertIsNone(prov.lookup("T1-doc-edit", 5))    # missing step
        self.assertIsNone(
            planner_mod.HistoricalBaselineProvider("/no/such.duckdb").lookup("X", 0))
        self.assertIsNone(planner_mod.HistoricalBaselineProvider(None).lookup("X", 0))


class TestCorpusDistinctPaths(unittest.TestCase):
    """v3 Phase 3 3b (β-i, Rev B §B.2): HistoricalBaselineProvider.
    corpus_distinct_paths() returns the cross-row baseline vocabulary — the
    union of distinct real-mode selected_paths across the whole corpus — for the
    comparator's K=2 distinctness check. Never raises; caches; empty on a
    corpus-less DB."""

    def _make_selections_db(self, rows):
        # rows: list of (mode, selected_paths_list). Builds a stage_a_selections
        # base table (the method queries FROM stage_a_selections — a base table
        # serves identically to the harness view).
        import duckdb
        path = Path(tempfile.mkdtemp()) / "corpus.duckdb"
        con = duckdb.connect(str(path))
        con.execute(
            "CREATE TABLE stage_a_selections (mode VARCHAR, selected_paths JSON)")
        for (mode, sel) in rows:
            con.execute("INSERT INTO stage_a_selections VALUES (?, ?)",
                        [mode, json.dumps(sel)])
        con.close()
        return path

    def test_returns_union_of_distinct_real_mode_paths(self) -> None:
        # Single-path + multi-path real rows union into the vocabulary; a
        # mock-mode row is excluded (mode='real' filter).
        db = self._make_selections_db([
            ("real", ["a.md"]),
            ("real", ["b.md"]),
            ("real", ["c.md", "d.md"]),  # multi-path row
            ("real", ["a.md"]),           # duplicate path collapses
            ("mock", ["z.md"]),           # excluded — mock mode
        ])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.corpus_distinct_paths(),
                         frozenset({"a.md", "b.md", "c.md", "d.md"}))

    def test_result_is_cached(self) -> None:
        db = self._make_selections_db([("real", ["a.md"]), ("real", ["b.md"])])
        prov = planner_mod.HistoricalBaselineProvider(db)
        first = prov.corpus_distinct_paths()
        self.assertEqual(first, frozenset({"a.md", "b.md"}))
        self.assertIs(prov.corpus_distinct_paths(), first)  # same object → cached

    def test_empty_db_returns_empty_frozenset(self) -> None:
        # Table present, zero rows → empty frozenset (not None).
        db = self._make_selections_db([])
        prov = planner_mod.HistoricalBaselineProvider(db)
        self.assertEqual(prov.corpus_distinct_paths(), frozenset())

    def test_corpusless_and_bad_db_never_raise(self) -> None:
        # No stage_a_selections table (e.g. a Phase 1b exit-sweep / events-only
        # DB), a missing file, and a None path all degrade to empty frozenset.
        import duckdb
        events_only = Path(tempfile.mkdtemp()) / "events-only.duckdb"
        con = duckdb.connect(str(events_only))
        con.execute("CREATE TABLE events (run_id VARCHAR)")
        con.close()
        self.assertEqual(
            planner_mod.HistoricalBaselineProvider(events_only).corpus_distinct_paths(),
            frozenset())
        self.assertEqual(
            planner_mod.HistoricalBaselineProvider("/no/such.duckdb").corpus_distinct_paths(),
            frozenset())
        self.assertEqual(
            planner_mod.HistoricalBaselineProvider(None).corpus_distinct_paths(),
            frozenset())


class TestShadowExecuteHaiku(_PlannerEventsBase):
    """v3 Phase 3 3b (β-ii): the confidence-independent shadow-execute-Haiku
    branch. Runs Haiku Stage A in parallel during a real-mode rich-context
    SHADOW step (opt-in), grades its selection against Opus with the INVERTED
    orientation (routed=Haiku, baseline=Opus), and leaves route_actual=Opus."""

    def _shadow_planner(self):
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        return Planner(
            model="claude-opus-4-7",
            policy=RoutingPolicy(PHASE_1B_STAGE_A_SHADOW, calibration=cal),
        )

    def _opt_in(self):
        return mock.patch.dict(
            os.environ, {"ANVIL_SHADOW_EXECUTE_HAIKU": "1"})

    # --- trigger gating (§3.1) ---

    def test_trigger_fires_real_shadow_richctx_optin(self) -> None:
        events.begin_run("T11-rich-dependency-real")
        p = self._shadow_planner()
        p._current_context_paths_count = 4
        with self._opt_in():
            self.assertTrue(p._should_shadow_execute_haiku())

    def test_trigger_off_when_optout(self) -> None:
        events.begin_run("T11-rich-dependency-real")
        p = self._shadow_planner()
        p._current_context_paths_count = 4
        # ANVIL_SHADOW_EXECUTE_HAIKU unset → dormant.
        self.assertFalse(p._should_shadow_execute_haiku())

    def test_trigger_off_on_empty_context(self) -> None:
        events.begin_run("T1-doc-edit-real")
        p = self._shadow_planner()
        p._current_context_paths_count = 0  # empty-context = canary's scope
        with self._opt_in():
            self.assertFalse(p._should_shadow_execute_haiku())

    def test_trigger_off_on_mock_mode(self) -> None:
        events.begin_run("T11-rich-dependency-mock")  # not -real
        p = self._shadow_planner()
        p._current_context_paths_count = 4
        with self._opt_in():
            self.assertFalse(p._should_shadow_execute_haiku())

    def test_trigger_off_under_canary_policy(self) -> None:
        # The canary path runs its own baseline; shadow-execute is shadow-only.
        events.begin_run("T11-rich-dependency-real")
        cal = RoutingCalibration(
            [{"context_paths_count": 0, "paths_returned": 0}]).policy
        p = Planner(model="claude-opus-4-7",
                    policy=RoutingPolicy(PHASE_1B_STAGE_A_CANARY, calibration=cal))
        p._current_context_paths_count = 4
        with self._opt_in():
            self.assertFalse(p._should_shadow_execute_haiku())

    # --- the helper (§3.2) ---

    def test_helper_emits_canary_baseline_kind_with_haiku_model(self) -> None:
        # On success: targets Haiku, emits CANARY_BASELINE_KIND (model=Haiku),
        # returns the parsed selection.
        path = "/v/note.md"
        client, capture = _make_fake_client(
            input_tokens=300, output_tokens=20, cache_creation=0,
            cache_read=0, text=path)
        p = self._shadow_planner()
        p._client = client
        sel = p._stage_a_shadow_execute_haiku("PROMPT", 0, {path: {}})
        self.assertEqual(sel, [path])                      # parsed selection
        self.assertEqual(capture["model"], CHEAP_STAGE_A_MODEL)  # API ran Haiku
        ev = next(e for e in self._events_for()
                  if e["kind"] == events.CANARY_BASELINE_KIND)["data"]
        self.assertEqual(ev["model"], CHEAP_STAGE_A_MODEL)
        self.assertTrue(ev["ok"])

    def test_helper_never_raises_on_api_failure(self) -> None:
        client = mock.MagicMock()
        client.with_options.return_value = client
        client.messages.stream.side_effect = RuntimeError("haiku boom")
        p = self._shadow_planner()
        p._client = client
        sel = p._stage_a_shadow_execute_haiku("PROMPT", 0, {"/v/n.md": {}})
        self.assertIsNone(sel)                              # → None, no raise
        ev = next(e for e in self._events_for()
                  if e["kind"] == events.CANARY_BASELINE_KIND)["data"]
        self.assertFalse(ev["ok"])                          # ok=False recorded
        self.assertEqual(ev["model"], CHEAP_STAGE_A_MODEL)

    # --- plan_step integration (orientation + route_actual) ---

    def _rich_brief_and_state(self):
        # A rich-context brief: one absolute context_path file so plan_step
        # computes context_paths_count=1 and _build_vault_index indexes it.
        ctx_file = self.tmp_path / "ctx-note.md"
        ctx_file.write_text("---\ntitle: ctx\n---\nbody\n", encoding="utf-8")
        brief = Brief(
            brief_version=1, project="anvil", build_name="rich-test",
            target_repo="x", target_repo_path=Path("/tmp"), vps_deploy="no",
            context_paths=[ctx_file],
            steps=[Step(number=1, name="s", scope_files=["a.py"],
                        scope_operations=["write"], smoke="echo x",
                        confirm="explicit")],
        )
        state = init_state(brief, "2026-05-20T00:00:00", brief_path="/nonexistent")
        return brief, state, str(ctx_file)

    def test_plan_step_inverts_orientation_and_keeps_route_actual_opus(self) -> None:
        events.begin_run("T11-rich-dependency-real")
        brief, state, ctx_path = self._rich_brief_and_state()
        # Opus (the primary Stage A via the fake client) selects the ctx file.
        client, _ = _make_fake_client(
            input_tokens=400, output_tokens=30, cache_creation=0,
            cache_read=0, text=ctx_path)
        p = self._shadow_planner()
        p._client = client
        # Stub the Haiku helper: record the call, return a DROP ([]), so the
        # comparator sees routed=[] (Haiku) vs baseline=[ctx_path] (Opus).
        calls = []
        def _stub(prompt, step_idx, vault_index):
            calls.append((prompt, step_idx))
            return []
        p._stage_a_shadow_execute_haiku = _stub
        with self._opt_in():
            p.plan_step(brief, state, 0)
        self.assertEqual(len(calls), 1)  # helper was invoked
        evs = self._events_for("T11-rich-dependency-real")
        begin = next(e for e in evs
                     if e["kind"] == "stage_a.shadow_compare.begin")["data"]
        # Inverted orientation: routed = Haiku ([]), baseline = Opus ([ctx]).
        self.assertEqual(begin["routed_paths"], [])
        self.assertEqual(begin["baseline_paths"], [ctx_path])
        self.assertEqual(begin["baseline_source"], "shadow-execute")
        # route_actual stays Opus on the primary Stage A call.
        a_end = next(e for e in evs
                     if e["kind"] == "planner.stage_a.api_end")["data"]
        self.assertEqual(a_end["route_actual"], "claude-opus-4-7")

    def test_plan_step_haiku_none_falls_back_to_identity(self) -> None:
        events.begin_run("T11-rich-dependency-real")
        brief, state, ctx_path = self._rich_brief_and_state()
        client, _ = _make_fake_client(
            input_tokens=400, output_tokens=30, cache_creation=0,
            cache_read=0, text=ctx_path)
        p = self._shadow_planner()
        p._client = client
        p._stage_a_shadow_execute_haiku = lambda *a, **k: None  # Haiku miss
        with self._opt_in():
            p.plan_step(brief, state, 0)
        begin = next(e for e in self._events_for("T11-rich-dependency-real")
                     if e["kind"] == "stage_a.shadow_compare.begin")["data"]
        # Identity comparison preserved: routed == baseline == Opus's selection.
        self.assertEqual(begin["routed_paths"], [ctx_path])
        self.assertEqual(begin["baseline_paths"], [ctx_path])
        self.assertEqual(begin["baseline_source"], "identity")

    def test_valid_kinds_unchanged(self) -> None:
        from anvil.events import VALID_KINDS
        self.assertEqual(len(VALID_KINDS), 51)  # Rev B §B.3: no new kind


class TestSelectedPathsAndRawResponseRecording(_PlannerEventsBase):
    """v3 Phase 2a Step 2 (V3P2A-2): planner.stage_a.parsed records the parsed
    selection LIST (selected_paths — comparator-ready, not just the count), the
    model's pre-parser response (raw_response_text, truncated to
    RAW_RESPONSE_MAX_CHARS), and a truncated flag. Additive on the existing kind
    (VALID_KINDS stays 51). paths_returned is retained and equals
    len(selected_paths)."""

    def _parsed_after_stage_a(self, stage_a_text, *, vault_index=None):
        # Inject a synthetic vault_index (keyed by path; the parser only checks
        # membership) so a selection can be non-empty without files on disk —
        # _load_files is never-raise on missing files, and the parsed emit
        # fires before Stage B anyway.
        if vault_index is not None:
            self.planner._vault_index_cache["planner-events-test"] = vault_index
        side = _make_fake({"A": [stage_a_text], "B": [_STAGE_B_VALID]})
        with mock.patch.object(
            Planner, "_call_anthropic", autospec=True, side_effect=side,
        ):
            self.planner.plan_step(self.brief, self.state, 0)
        return next(e for e in self._events_for()
                    if e["kind"] == "planner.stage_a.parsed")["data"]

    def test_selected_paths_records_parser_postfilter_content(self) -> None:
        # 3 in-index + 1 out-of-index + 1 duplicate → selected_paths is the 3
        # in-index entries, deduplicated, order preserved (parser contract).
        vidx = {"docs/a.md": {}, "docs/b.md": {}, "docs/c.md": {}}
        text = "docs/c.md\ndocs/a.md\ndocs/b.md\nX/not-in-index.md\ndocs/c.md\n"
        d = self._parsed_after_stage_a(text, vault_index=vidx)
        self.assertEqual(d["selected_paths"], ["docs/c.md", "docs/a.md", "docs/b.md"])
        self.assertEqual(d["paths_returned"], 3)
        self.assertEqual(d["paths_returned"], len(d["selected_paths"]))  # invariant
        self.assertEqual(d["raw_response_text"], text)   # original, untruncated
        self.assertFalse(d["truncated"])

    def test_selected_paths_equals_parser_output(self) -> None:
        # selected_paths matches _parse_stage_a_response exactly (the recorded
        # list IS the parser's post-filter output, criterion 1).
        vidx = {"docs/a.md": {}, "docs/b.md": {}}
        text = "docs/b.md\ndocs/a.md\nZ/drop.md\n"
        expected = planner_mod._parse_stage_a_response(text, vidx)
        d = self._parsed_after_stage_a(text, vault_index=vidx)
        self.assertEqual(d["selected_paths"], expected)

    def test_empty_selection_records_empty_list_not_null(self) -> None:
        # Out-of-index paths → selected_paths == [] (a list, never null/omitted).
        vidx = {"docs/a.md": {}}
        text = "X/nope-1.md\nY/nope-2.md\n"
        d = self._parsed_after_stage_a(text, vault_index=vidx)
        self.assertEqual(d["selected_paths"], [])
        self.assertIsNotNone(d["selected_paths"])
        self.assertEqual(d["paths_returned"], 0)
        self.assertFalse(d["truncated"])

    def test_empty_raw_response_invariants_hold(self) -> None:
        # The model returned nothing: raw_response_text == "", selected == [],
        # paths_returned == 0, truncated False — every invariant holds.
        d = self._parsed_after_stage_a("")
        self.assertEqual(d["selected_paths"], [])
        self.assertEqual(d["paths_returned"], 0)
        self.assertEqual(d["raw_response_text"], "")
        self.assertFalse(d["truncated"])

    def test_raw_response_truncated_over_limit(self) -> None:
        text = "z" * (events.RAW_RESPONSE_MAX_CHARS + 5000)
        d = self._parsed_after_stage_a(text)
        self.assertTrue(d["truncated"])
        self.assertEqual(len(d["raw_response_text"]), events.RAW_RESPONSE_MAX_CHARS)
        self.assertEqual(d["raw_response_text"], text[:events.RAW_RESPONSE_MAX_CHARS])
        self.assertEqual(d["selected_paths"], [])   # selection still recorded
        self.assertEqual(d["paths_returned"], 0)


class TestRawResponseTruncationHelper(unittest.TestCase):
    """v3 Phase 2a Step 2 (V3P2A-2): the _truncate_raw_response contract +
    RAW_RESPONSE_MAX_CHARS. Truncation slices the DECODED Python string, so it
    cuts on a character boundary — never a split UTF-8 codepoint (Q-A2)."""

    def test_limit_constant(self) -> None:
        self.assertEqual(events.RAW_RESPONSE_MAX_CHARS, 16384)

    def test_under_limit_unchanged(self) -> None:
        text = "a/b.md\nc/d.md"
        self.assertEqual(events._truncate_raw_response(text), (text, False))

    def test_exactly_at_limit_not_truncated(self) -> None:
        text = "z" * events.RAW_RESPONSE_MAX_CHARS
        out, trunc = events._truncate_raw_response(text)
        self.assertEqual(out, text)
        self.assertFalse(trunc)

    def test_one_over_limit_truncated(self) -> None:
        text = "z" * (events.RAW_RESPONSE_MAX_CHARS + 1)
        out, trunc = events._truncate_raw_response(text)
        self.assertEqual(len(out), events.RAW_RESPONSE_MAX_CHARS)
        self.assertTrue(trunc)
        self.assertEqual(out, text[:events.RAW_RESPONSE_MAX_CHARS])

    def test_empty_and_none(self) -> None:
        self.assertEqual(events._truncate_raw_response(""), ("", False))
        self.assertEqual(events._truncate_raw_response(None), ("", False))

    def test_multibyte_char_boundary_safe(self) -> None:
        # Multibyte codepoints past the limit: slicing the decoded string keeps
        # exactly N codepoints, each intact (re-encodes to valid UTF-8) — never
        # a split 2-byte 'é'.
        text = "é" * (events.RAW_RESPONSE_MAX_CHARS + 100)
        out, trunc = events._truncate_raw_response(text)
        self.assertTrue(trunc)
        self.assertEqual(len(out), events.RAW_RESPONSE_MAX_CHARS)
        self.assertEqual(out, "é" * events.RAW_RESPONSE_MAX_CHARS)
        out.encode("utf-8")  # must not raise — every codepoint intact


# v3 Phase 1c Step 1 (criterion-3 sum-check reframe). The affine relation is
# validated against the ACTUAL Phase 1c Step 0 real-mode Opus Stage A/B
# events, captured inline because state/ is gitignored so the transient
# Step 0 DB can't be a CI fixture (Step1C-F4). Each tuple:
# (stage, model, block_sum, input_tokens, cache_read, cache_creation).
_STEP0_REAL_STAGE_EVENTS = [
    ("A", "haiku", 488, 3550, 0, 0),     # T1 canary — Haiku, NO cache (excluded)
    ("A", "opus", 702, 1527, 3479, 0),
    ("A", "opus", 723, 1637, 3479, 0),
    ("A", "opus", 738, 1511, 3479, 0),
    ("A", "opus", 818, 1717, 3479, 0),
    ("A", "opus", 891, 1857, 3479, 0),
    ("A", "opus", 1163, 2221, 3479, 0),
    ("A", "opus", 1271, 2413, 3479, 0),
    ("A", "opus", 1839, 3282, 3479, 0),
    ("B", "opus", 495, 1277, 3479, 0),
    ("B", "opus", 709, 1634, 3479, 0),
    ("B", "opus", 730, 1744, 3479, 0),
    ("B", "opus", 745, 1618, 3479, 0),
    ("B", "opus", 825, 1824, 3479, 0),
    ("B", "opus", 898, 1964, 3479, 0),
    ("B", "opus", 1546, 2968, 3479, 0),
    ("B", "opus", 1652, 3169, 3479, 0),
    ("B", "opus", 2336, 4214, 3479, 0),
]


_MODEL_ID = {  # fixture short label → full model id the accessors expect
    "opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5-20251001",
}

# v3 Phase 2a Step 1 (Q-A5): the 9 Haiku Stage A rows from the Phase 1c Step 4
# EXIT sweep (all-6 broadened canary → all Stage A ran Haiku; cr=cc=0 — Haiku
# doesn't cache, Step3.5C-F3). The Opus slope (1.64) gives 0/9 here (overshoot
# 21–33%); the Haiku-native slope (1.18) + shared intercept (407) + Haiku
# system constant (2590) gives 9/9 (R²=0.994). Step 4 re-grades a fresh N=9.
# (stage, model, block_sum, input_tokens, cache_read, cache_creation)
_PHASE1C_EXIT_HAIKU_STAGE_A = [
    ("A", "haiku", 488, 3550, 0, 0),    # T1
    ("A", "haiku", 702, 3818, 0, 0),    # T2 s0
    ("A", "haiku", 1159, 4365, 0, 0),   # T2 s1
    ("A", "haiku", 891, 4099, 0, 0),    # T3
    ("A", "haiku", 738, 3810, 0, 0),    # T4
    ("A", "haiku", 818, 3962, 0, 0),    # T5 s0
    ("A", "haiku", 1253, 4473, 0, 0),   # T5 s1
    ("A", "haiku", 1778, 5087, 0, 0),   # T5 s2
    ("A", "haiku", 723, 3910, 0, 0),    # T6
]


def _uncached_user_prompt_equiv(model, input_tokens, cache_read, cache_creation):
    """Cache-invariant user-prompt token total (Step1C-F1): the per-model
    system prompt moves between input_tokens and cache_read/creation, so adding
    them back and subtracting it isolates the user prompt. v3 Phase 2a Step 1
    (Q-A5): the system constant is per-model."""
    return (input_tokens + cache_read + cache_creation
            - events.planner_system_prompt_tokens(_MODEL_ID[model]))


def _affine_predict(model, block_sum):
    """Affine prediction at the per-model SLOPE + shared intercept (Q-A5)."""
    return (events.PLANNER_USER_TEMPLATE_TOKENS
            + block_sum * events.block_token_inflation_factor(_MODEL_ID[model]))


def _abs_pct_err(stage, model, bsum, inp, cr, cc):
    equiv = _uncached_user_prompt_equiv(model, inp, cr, cc)
    return abs(_affine_predict(model, bsum) - equiv) / equiv * 100.0


class TestCriterion3SumCheckReframe(unittest.TestCase):
    """v3 Phase 1c Step 1 — the criterion-3 sum-check reframed to the honest
    AFFINE, cache-invariant relation (Step1C-F1):

        uncached_user_prompt_equiv ≈ PLANNER_USER_TEMPLATE_TOKENS
            + block_sum × block_token_inflation_factor(model)

    v3 Phase 2a Step 1 (V3P2A-1, Q-A5): graded PER-MODEL and the Opus-only
    filter (Step1C-F2) is RETIRED. The system constant AND the slope are
    per-model (Opus 3479/1.64, Haiku 2590/1.18); the intercept (407) is shared.
    Opus rows hold at slope 1.64 (≥15/17, R²≥0.99); the 9 Haiku exit-sweep rows
    hold at slope 1.18 (9/9) but 0/9 at the Opus slope — the slope is genuinely
    per-model, not just the system constant."""

    OPUS = [r for r in _STEP0_REAL_STAGE_EVENTS if r[1] == "opus"]

    def test_per_model_constants_and_accessors(self) -> None:
        # Per-model mappings + accessors return the documented values.
        self.assertEqual(
            events.PLANNER_SYSTEM_PROMPT_TOKENS_BY_MODEL["claude-opus-4-7"], 3479)
        self.assertEqual(
            events.PLANNER_SYSTEM_PROMPT_TOKENS_BY_MODEL[
                "claude-haiku-4-5-20251001"], 2590)
        self.assertEqual(
            events.BLOCK_TOKEN_INFLATION_FACTOR_BY_MODEL["claude-opus-4-7"], 1.64)
        self.assertEqual(
            events.BLOCK_TOKEN_INFLATION_FACTOR_BY_MODEL[
                "claude-haiku-4-5-20251001"], 1.18)
        self.assertEqual(events.PLANNER_USER_TEMPLATE_TOKENS, 407)
        self.assertEqual(
            events.planner_system_prompt_tokens("claude-opus-4-7"), 3479)
        self.assertEqual(
            events.planner_system_prompt_tokens("claude-haiku-4-5-20251001"), 2590)
        self.assertEqual(
            events.block_token_inflation_factor("claude-opus-4-7"), 1.64)
        self.assertEqual(
            events.block_token_inflation_factor("claude-haiku-4-5-20251001"), 1.18)

    def test_unknown_model_falls_back_to_opus_and_registers(self) -> None:
        # Unknown model → Opus values (conservative) + registered for surfacing
        # (mirrors unknown_cost_models, V3P1C-4). Known models do NOT register.
        unknown = "claude-future-99-sumcheck-probe"
        self.assertEqual(events.planner_system_prompt_tokens(unknown), 3479)
        self.assertEqual(events.block_token_inflation_factor(unknown), 1.64)
        self.assertIn(unknown, events.unknown_token_models())
        self.assertNotIn("claude-opus-4-7", events.unknown_token_models())
        self.assertNotIn(
            "claude-haiku-4-5-20251001", events.unknown_token_models())

    def test_affine_relation_stage_a_opus(self) -> None:
        # Opus Stage A rows hold the affine relation at the Opus slope (1.64).
        errs = [_abs_pct_err(*r) for r in self.OPUS if r[0] == "A"]
        self.assertLessEqual(max(errs), 10.0)               # no row off by >10%
        self.assertLessEqual(sum(errs) / len(errs), 5.0)     # mean within ±5%
        self.assertGreaterEqual(sum(1 for e in errs if e <= 5.0), 7)  # ≥7/8

    def test_affine_relation_stage_b_opus_and_overall(self) -> None:
        # Opus Stage B + overall R²/±5% at the Opus slope.
        errs = [_abs_pct_err(*r) for r in self.OPUS if r[0] == "B"]
        self.assertLessEqual(max(errs), 10.0)
        self.assertLessEqual(sum(errs) / len(errs), 5.0)
        self.assertGreaterEqual(sum(1 for e in errs if e <= 5.0), 8)  # ≥8/9
        # Binding grade: ≥15/17 within ±5% AND R² ≥ 0.99 over all Opus rows.
        all_errs = [_abs_pct_err(*r) for r in self.OPUS]
        self.assertGreaterEqual(sum(1 for e in all_errs if e <= 5.0), 15)
        ys = [_uncached_user_prompt_equiv(m, i, cr, cc)
              for (_, m, b, i, cr, cc) in self.OPUS]
        ybar = sum(ys) / len(ys)
        ss_tot = sum((y - ybar) ** 2 for y in ys)
        ss_res = sum((_uncached_user_prompt_equiv(m, i, cr, cc)
                      - _affine_predict(m, b)) ** 2
                     for (_, m, b, i, cr, cc) in self.OPUS)
        self.assertGreaterEqual(1 - ss_res / ss_tot, 0.99)

    def test_affine_relation_haiku_at_native_slope(self) -> None:
        # v3 Phase 2a Step 1 (Q-A5): the 9 Haiku exit-sweep rows hold the affine
        # relation at the Haiku-native slope (1.18) + shared intercept — 9/9
        # within ±5%, R² ≥ 0.99 (the Haiku-native fit).
        errs = [_abs_pct_err(*r) for r in _PHASE1C_EXIT_HAIKU_STAGE_A]
        self.assertEqual(sum(1 for e in errs if e <= 5.0), 9)  # 9/9
        self.assertLessEqual(max(errs), 5.0)
        ys = [_uncached_user_prompt_equiv(m, i, cr, cc)
              for (_, m, b, i, cr, cc) in _PHASE1C_EXIT_HAIKU_STAGE_A]
        ybar = sum(ys) / len(ys)
        ss_tot = sum((y - ybar) ** 2 for y in ys)
        ss_res = sum((_uncached_user_prompt_equiv(m, i, cr, cc)
                      - _affine_predict(m, b)) ** 2
                     for (_, m, b, i, cr, cc) in _PHASE1C_EXIT_HAIKU_STAGE_A)
        self.assertGreaterEqual(1 - ss_res / ss_tot, 0.99)

    def test_haiku_rows_need_native_slope_not_opus_slope(self) -> None:
        # v3 Phase 2a Step 1 (Q-A5 — load-bearing): the slope is genuinely
        # per-model, not just the system constant. Applying the OPUS slope
        # (1.64) to the Haiku rows (with the correct Haiku system constant)
        # gives 0/9 — all overshoot >15%. This is why BLOCK_TOKEN_INFLATION_
        # FACTOR is a per-model mapping, not a single shared 1.64.
        opus_factor = events.block_token_inflation_factor("claude-opus-4-7")
        haiku_sys = events.planner_system_prompt_tokens(
            "claude-haiku-4-5-20251001")

        def opus_slope_err(bsum, inp):
            equiv = inp - haiku_sys
            pred = events.PLANNER_USER_TEMPLATE_TOKENS + bsum * opus_factor
            return abs(pred - equiv) / equiv * 100.0

        errs = [opus_slope_err(r[2], r[3]) for r in _PHASE1C_EXIT_HAIKU_STAGE_A]
        self.assertEqual(sum(1 for e in errs if e <= 5.0), 0)  # 0/9 at Opus slope
        self.assertGreater(min(errs), 15.0)

    def test_cache_invariant_across_creation_and_read(self) -> None:
        # A creation-call, a read-call, and an uncached call with the SAME user
        # prompt all yield the same equiv (per-model accessor).
        user = 900
        sys = events.planner_system_prompt_tokens("claude-opus-4-7")
        creation = _uncached_user_prompt_equiv("opus", user, 0, sys)     # first
        read = _uncached_user_prompt_equiv("opus", user, sys, 0)         # later
        uncached = _uncached_user_prompt_equiv("opus", user + sys, 0, 0)  # none
        self.assertEqual(creation, user)
        self.assertEqual(read, user)
        self.assertEqual(uncached, user)

    def test_haiku_row_fits_at_per_model_params_retiring_opus_filter(self) -> None:
        # v3 Phase 2a Step 1 (Q-A5): Phase 1c FILTERED the Haiku row out of the
        # sum-check (Step1C-F2) because the Opus 3479 constant made its equiv
        # nonsense (≈71 → ~1600% error). With the per-model system constant
        # (2590) AND per-model slope (1.18) the SAME row now fits within ±5% —
        # the Opus-only filter is discharged.
        haiku = next(r for r in _STEP0_REAL_STAGE_EVENTS if r[1] == "haiku")
        bad_equiv = haiku[3] + haiku[4] + haiku[5] - 3479  # old Opus-constant
        self.assertLess(bad_equiv, 200)                    # the footgun
        self.assertLessEqual(_abs_pct_err(*haiku), 5.0)    # fits per-model now


# v3 Phase 1c Step 2 — narrowed planner-side caching of the brief block.
_BRIEF_USER_PROMPT = (
    "## Build brief\n\n<brief>\nDo the thing carefully.\n</brief>\n\n"
    "## Current state\n\n<state>\n{}\n</state>\n\n"
    "## Step being planned\nStep 0: do-thing\n"
)


class TestBriefBlockCaching(_PlannerEventsBase):
    """v3 Phase 1c Step 2 (V3P1C-2): cache_control on the brief block (stable,
    prefix-positioned). The split is real-path-only — MockedPlanner overrides
    _call_anthropic and makes no API call (Step2C-F2)."""

    def test_split_helper_two_blocks_with_cache_control(self) -> None:
        # Q10.1: split helper → 2 blocks, cache_control on the brief-prefix.
        blocks = planner_mod._split_user_for_brief_cache(_BRIEF_USER_PROMPT)
        self.assertIsInstance(blocks, list)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertTrue(blocks[0]["text"].endswith("</brief>"))
        self.assertNotIn("cache_control", blocks[1])
        # byte-identical reconstruction (criterion 5).
        self.assertEqual(blocks[0]["text"] + blocks[1]["text"], _BRIEF_USER_PROMPT)

    def test_split_helper_noops_without_brief_marker(self) -> None:
        # Q10.2: no </brief> → return the string unchanged (Stage C / empty).
        self.assertEqual(
            planner_mod._split_user_for_brief_cache("no marker here"), "no marker here")
        self.assertEqual(planner_mod._split_user_for_brief_cache(""), "")
        # prompt ending exactly at </brief> (empty remainder) → no 2-block split.
        self.assertEqual(
            planner_mod._split_user_for_brief_cache("<brief>x</brief>"), "<brief>x</brief>")

    def test_call_anthropic_stage_a_caches_brief(self) -> None:
        # Q10.3: Stage A user content is the 2-block shape, byte-identical.
        client, capture = _make_fake_client(
            input_tokens=500, output_tokens=50, cache_creation=4180, cache_read=0)
        self.planner._client = client
        self.planner._call_anthropic(
            system="SYS", user=_BRIEF_USER_PROMPT, timeout=30, step=0, stage="A")
        content = capture["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(content[0]["text"] + content[1]["text"], _BRIEF_USER_PROMPT)

    def test_call_anthropic_stage_b_caches_brief(self) -> None:
        # Q10.4: same 2-block shape on Stage B via the shared wrapper.
        client, capture = _make_fake_client(
            input_tokens=500, output_tokens=50, cache_creation=4180, cache_read=0)
        self.planner._client = client
        self.planner._call_anthropic(
            system="SYS", user=_BRIEF_USER_PROMPT, timeout=30, step=0, stage="B")
        content = capture["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})

    def test_brief_cache_creation_recorded_first_call(self) -> None:
        # Q10.5: cache_creation recorded, cache_read=0 on the first call.
        client, _ = _make_fake_client(
            input_tokens=500, output_tokens=50, cache_creation=4180, cache_read=0)
        self.planner._client = client
        self.planner._call_anthropic(
            system="SYS", user=_BRIEF_USER_PROMPT, timeout=30, step=0, stage="A")
        data = [e for e in self._events_for()
                if e["kind"] == "planner.stage_a.api_end"][-1]["data"]
        self.assertEqual(data["cache_creation_input_tokens"], 4180)
        self.assertEqual(data["cache_read_input_tokens"], 0)

    def test_brief_cache_read_recorded_second_call(self) -> None:
        # Q10.6: cache_read > 0 recorded on the subsequent (hit) call.
        c1, _ = _make_fake_client(
            input_tokens=500, output_tokens=50, cache_creation=4180, cache_read=0)
        self.planner._client = c1
        self.planner._call_anthropic(
            system="SYS", user=_BRIEF_USER_PROMPT, timeout=30, step=0, stage="A")
        c2, _ = _make_fake_client(
            input_tokens=21, output_tokens=50, cache_creation=0, cache_read=4180)
        self.planner._client = c2
        self.planner._call_anthropic(
            system="SYS", user=_BRIEF_USER_PROMPT, timeout=30, step=1, stage="A")
        data = [e for e in self._events_for()
                if e["kind"] == "planner.stage_a.api_end"][-1]["data"]
        self.assertEqual(data["cache_read_input_tokens"], 4180)
        self.assertEqual(data["input_tokens"], 21)

    def test_affine_invariant_holds_pre_caching(self) -> None:
        # Q10.7: regression guard — Step 1 affine relation on pre-caching rows.
        opus = [r for r in _STEP0_REAL_STAGE_EVENTS if r[1] == "opus"]
        errs = [_abs_pct_err(*r) for r in opus]
        self.assertGreaterEqual(sum(1 for e in errs if e <= 5.0), 15)

    def test_affine_invariant_survives_brief_caching(self) -> None:
        # Q10.8 (Q5): caching moves ~brief tokens from input_tokens to
        # cache_read; the cache-invariant equiv (and the affine relation) is
        # unchanged with the same per-model constants (Opus 1.64/407/3479 here).
        opus = [r for r in _STEP0_REAL_STAGE_EVENTS if r[1] == "opus"]
        moved = 700  # ~brief tokens that move input_tokens → cache_read post-caching
        for (stage, model, bsum, inp, cr, cc) in opus:
            pre = _uncached_user_prompt_equiv(model, inp, cr, cc)
            post = _uncached_user_prompt_equiv(model, inp - moved, cr + moved, cc)
            self.assertEqual(pre, post)  # equiv invariant to the token movement
            self.assertEqual(
                _abs_pct_err(stage, model, bsum, inp, cr, cc),
                _abs_pct_err(stage, model, bsum, inp - moved, cr + moved, cc))

    def test_canary_baseline_caches_brief(self) -> None:
        # Q10.10: the canary's parallel Opus baseline also emits the 2-block
        # shape — so it cache-CREATES the Opus brief prefix that T1's Stage B
        # Opus call reads (forgetting this silently breaks T1 caching).
        client, capture = _make_fake_client(
            input_tokens=500, output_tokens=50, cache_creation=4180, cache_read=0)
        self.planner._client = client
        self.planner._stage_a_canary_baseline(_BRIEF_USER_PROMPT, 0)
        content = capture["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(content[0]["text"] + content[1]["text"], _BRIEF_USER_PROMPT)


if __name__ == "__main__":
    unittest.main()
