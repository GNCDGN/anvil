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
from anvil.planner import Planner
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
                "model": self.model,
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
                "model": self.model,
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
                    "model": self.model,
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
                    "model": self.model,
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
                    "model": self.model,
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
        # route_actual is self.model (the test planner's model string).
        self.assertEqual(d["route_actual"], "claude-opus-4-7-test")
        self.assertEqual(d["route_candidate"], d["route_actual"])
        self.assertFalse(d["route_fallback_fired"])
        self.assertEqual(d["policy_version"], "v3-phase-0-passive")
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
        self.assertEqual(d["route_actual"], "claude-opus-4-7-test")
        self.assertEqual(d["policy_version"], "v3-phase-0-passive")
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
        self.assertEqual(sd["actual_route_taken"], "claude-opus-4-7-test")
        # agreement: candidate (opus-4-7) vs actual (the test model string)
        # — they differ here only because the test planner uses a sentinel
        # model; the logic is exercised either way.
        self.assertEqual(
            sd["agreement"],
            sd["shadow_route_candidate"] == sd["actual_route_taken"],
        )
        # basis is the same features_seen dict the api_end carried.
        self.assertEqual(sd["shadow_decision_basis"],
                         evs[i_api]["data"]["features_seen"])

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
                actual_route_taken=self.model,
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


if __name__ == "__main__":
    unittest.main()
