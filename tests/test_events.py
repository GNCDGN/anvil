"""Tests for anvil.events (v2 Phase 1 Step 1).

Covers: schema validation, emit happy/failure paths, run lifecycle,
sentinel run_id, idempotency, ANVIL_ROOT redirect, drop_count semantics,
multi-emit JSONL integrity, unknown-kind handling.

Each test clears the module-global state in setUp so tests do not bleed
into each other. ANVIL_ROOT is redirected to a tmp_path for every test
so events.jsonl writes never escape the test scope.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from anvil import events


class _EventsTestBase(unittest.TestCase):
    """Shared setUp/tearDown for module-global state and ANVIL_ROOT redirect."""

    def setUp(self) -> None:
        # Reset module state so a prior test's begin_run/emit can't leak.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        # Redirect every write under a per-test temp dir.
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(
            os.environ, {"ANVIL_ROOT": str(self.tmp_path)}
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()
        # Belt-and-braces: clear state again so a test that fails mid-flight
        # cannot break the next test in sequence.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

    def _read_events(self, run_id: str) -> list[dict]:
        """Return the events.jsonl rows as parsed dicts, in order."""
        path = self.tmp_path / "state" / "runs" / run_id / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class TestEventSchema(_EventsTestBase):
    """Pydantic schema rules — kind catalogue, required fields, types."""

    def test_happy_path_constructs(self) -> None:
        e = events.Event(
            ts="2026-05-20T10:15:42.000+00:00",
            run_id="r1",
            kind="run.start",
            data={},
        )
        self.assertEqual(e.kind, "run.start")
        self.assertEqual(e.step_idx, None)
        self.assertEqual(e.elapsed_ms, 0)

    def test_missing_required_field_raises(self) -> None:
        # `ts` is required and has no default.
        with self.assertRaises(ValidationError):
            events.Event(run_id="r1", kind="run.start")  # type: ignore[call-arg]

    def test_invalid_kind_raises_at_construction(self) -> None:
        with self.assertRaises(ValidationError):
            events.Event(
                ts="2026-05-20T10:15:42.000+00:00",
                run_id="r1",
                kind="not.a.real.kind",
            )

    def test_valid_kinds_catalogue_size(self) -> None:
        # Pinned by the module's assert; this test surfaces drift at the
        # test-suite level too. (45 → 46: v3 Phase 0 Step 2 added
        # "shadow.decision". 46 → 50: v3 Phase 0 Step 3 added the four
        # silent-miss / parser-drop kinds. 50 → 51: v3 Phase 1b Step 3 added
        # "planner.stage_a.canary_baseline.api_end" — the first v3 bump.
        # 51 → 52: v4 Phase 2c Step 2 added "observe.captured" — the first v4
        # bump, the observe-loop's capture event.)
        self.assertEqual(len(events.VALID_KINDS), 52)
        # A few canonical kinds present:
        for k in ("run.start", "planner.stage_b.api_end",
                  "ssh.stage.end", "telegram.poll.reply", "shadow.decision",
                  "stage_a.shadow_compare.end", "stage_a.parser_drop"):
            self.assertIn(k, events.VALID_KINDS)


class TestEmitHappyPath(_EventsTestBase):
    """Emit writes a parseable JSONL row; fields populated correctly."""

    def test_emit_lands_in_jsonl(self) -> None:
        events.begin_run("r1")
        ok = events.emit("step.start", {"step_number": 1}, step_idx=0)
        events.end_run()

        self.assertTrue(ok)
        rows = self._read_events("r1")
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds, ["run.start", "step.start", "run.end"])
        step = rows[1]
        self.assertEqual(step["run_id"], "r1")
        self.assertEqual(step["step_idx"], 0)
        self.assertEqual(step["data"], {"step_number": 1})
        self.assertTrue(step["ts"].endswith("+00:00"))

    def test_emit_with_step_idx_populated(self) -> None:
        events.begin_run("r1")
        events.emit("coder.subprocess.start", {}, step_idx=2)
        events.end_run()
        rows = self._read_events("r1")
        coder = [r for r in rows if r["kind"] == "coder.subprocess.start"][0]
        self.assertEqual(coder["step_idx"], 2)

    def test_emit_with_sentinel_run_id(self) -> None:
        # No begin_run — emit lands under "unknown-run".
        ok = events.emit("step.start", {"step_number": 1})
        self.assertTrue(ok)
        rows = self._read_events("unknown-run")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "unknown-run")
        self.assertEqual(events.current_run_id(), "unknown-run")


class TestEmitFailurePaths(_EventsTestBase):
    """Unknown kinds, write errors, drop_count, never-raise contract."""

    def test_unknown_kind_drops_returns_false(self) -> None:
        events.begin_run("r1")
        start_drops = events.drop_count()
        ok = events.emit("bogus.kind", {})
        self.assertFalse(ok)
        self.assertEqual(events.drop_count(), start_drops + 1)
        # File contains run.start but NOT the bogus emit.
        rows = self._read_events("r1")
        self.assertNotIn("bogus.kind", [r["kind"] for r in rows])

    def test_unknown_kind_logged_once_only(self) -> None:
        events.begin_run("r1")
        with self.assertLogs("anvil.events", level="WARNING") as captured:
            events.emit("bogus.kind", {})
            events.emit("bogus.kind", {})
            events.emit("bogus.kind", {})
        # Three drops but only one log line per unique unknown kind.
        bogus_logs = [m for m in captured.output if "bogus.kind" in m]
        self.assertEqual(len(bogus_logs), 1)
        self.assertEqual(events.drop_count(), 3)

    def test_write_failure_increments_drop_count_no_raise(self) -> None:
        events.begin_run("r1")
        start_drops = events.drop_count()
        # Patch _real_append (the hot path) to raise OSError;
        # emit must catch and drop.
        with mock.patch.object(
            events, "_real_append",
            side_effect=OSError("disk full (simulated)"),
        ):
            ok = events.emit("step.start", {"step_number": 1})
        self.assertFalse(ok)
        self.assertEqual(events.drop_count(), start_drops + 1)

    def test_emit_never_raises_on_unexpected(self) -> None:
        events.begin_run("r1")
        with mock.patch.object(
            events, "_real_append",
            side_effect=RuntimeError("totally unexpected"),
        ):
            ok = events.emit("step.start", {})
        # Did not raise, returned False, drop_count incremented.
        self.assertFalse(ok)
        self.assertGreater(events.drop_count(), 0)


class TestRunLifecycle(_EventsTestBase):
    """begin_run/end_run, anchor semantics, idempotency, no-op end_run."""

    def test_begin_run_sets_state_emits_run_start(self) -> None:
        self.assertIsNone(events._run_id)
        events.begin_run("r1")
        self.assertEqual(events._run_id, "r1")
        self.assertIsNotNone(events._anchor_monotonic)
        rows = self._read_events("r1")
        self.assertEqual(rows[0]["kind"], "run.start")
        self.assertEqual(rows[0]["elapsed_ms"], 0)

    def test_run_start_elapsed_ms_is_zero_subsequent_grows(self) -> None:
        events.begin_run("r1")
        # Let the monotonic clock advance.
        time.sleep(0.02)
        events.emit("step.start", {})
        events.end_run()
        rows = self._read_events("r1")
        run_start = next(r for r in rows if r["kind"] == "run.start")
        step_start = next(r for r in rows if r["kind"] == "step.start")
        self.assertEqual(run_start["elapsed_ms"], 0)
        self.assertGreaterEqual(step_start["elapsed_ms"], 15)

    def test_end_run_emits_run_end_with_drops_and_resets(self) -> None:
        events.begin_run("r1")
        events.emit("bogus.kind", {})  # one drop
        events.end_run()
        rows = self._read_events("r1")
        run_end = next(r for r in rows if r["kind"] == "run.end")
        self.assertEqual(run_end["data"], {"drops": 1})
        # State reset.
        self.assertIsNone(events._run_id)
        self.assertIsNone(events._anchor_monotonic)
        self.assertEqual(events.drop_count(), 0)

    def test_begin_run_idempotent_resets_anchor(self) -> None:
        events.begin_run("r1")
        first_anchor = events._anchor_monotonic
        time.sleep(0.02)
        events.begin_run("r2")
        second_anchor = events._anchor_monotonic
        self.assertNotEqual(first_anchor, second_anchor)
        self.assertEqual(events._run_id, "r2")
        # Both runs produced their own run.start events under distinct
        # run_id dirs.
        self.assertTrue(self._read_events("r1"))
        self.assertEqual(self._read_events("r2")[0]["kind"], "run.start")

    def test_end_run_without_begin_run_is_noop(self) -> None:
        # No state to flush — must not crash, must not emit anything.
        events.end_run()  # no exception
        # No events.jsonl written anywhere under tmp.
        runs_dir = self.tmp_path / "state" / "runs"
        if runs_dir.exists():
            self.assertEqual(list(runs_dir.iterdir()), [])


class TestMultiEmitAndRedirect(_EventsTestBase):
    """JSONL integrity under multiple emits + ANVIL_ROOT redirect honoured."""

    def test_ten_emits_produce_valid_jsonl(self) -> None:
        events.begin_run("r1")
        for i in range(10):
            events.emit("step.start", {"i": i}, step_idx=i)
        events.end_run()
        rows = self._read_events("r1")
        # 1 (run.start) + 10 (step.start) + 1 (run.end) = 12
        self.assertEqual(len(rows), 12)
        step_rows = [r for r in rows if r["kind"] == "step.start"]
        self.assertEqual([r["data"]["i"] for r in step_rows], list(range(10)))

    def test_anvil_root_env_redirect_honoured(self) -> None:
        # tmp_path was already injected via setUp; verify the file lands
        # under that root, not under the live repo's state/runs/.
        events.begin_run("r1")
        events.end_run()
        expected = self.tmp_path / "state" / "runs" / "r1" / "events.jsonl"
        self.assertTrue(expected.is_file(),
                        f"events.jsonl should land at {expected}")


class TestAppendModeHotPath(_EventsTestBase):
    """Step 2 prep: _real_append is the O(1) hot path, replacing the
    Step 1 read-modify-write block. Verify the new write semantics."""

    def test_real_append_used_for_each_emit(self) -> None:
        """Patching _real_append (not _real_write) catches every emit."""
        captured: list[tuple[Path, str]] = []

        def _spy(path: Path, text: str) -> None:
            captured.append((path, text))
            # Still write so the file exists for subsequent assertions.
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)

        events.begin_run("r1")
        with mock.patch.object(events, "_real_append", side_effect=_spy):
            events.emit("step.start", {"i": 0})
            events.emit("step.start", {"i": 1})
            events.emit("step.start", {"i": 2})
        events.end_run()

        # begin_run.run_start + 3 emits + end_run.run_end = 5 captured
        # writes via _real_append (the run.start/end go through emit too).
        # The patched scope above only covers the three middle emits;
        # the run.start fires before mock.patch.object entered, and the
        # run.end fires after it exited. So we count exactly 3.
        self.assertEqual(len(captured), 3)

    def test_append_path_produces_valid_jsonl_for_long_sequence(self) -> None:
        """Appendmode integrity: 30 emits in tight succession still produce
        a JSONL where every line parses independently."""
        events.begin_run("r1")
        for i in range(30):
            events.emit("step.start", {"i": i}, step_idx=i)
        events.end_run()
        rows = self._read_events("r1")
        # 1 run.start + 30 step.start + 1 run.end = 32
        self.assertEqual(len(rows), 32)
        # Every step row carries its i intact, in order.
        step_rows = [r for r in rows if r["kind"] == "step.start"]
        self.assertEqual([r["data"]["i"] for r in step_rows], list(range(30)))


class TestRoutingObservability(_EventsTestBase):
    """v3 Phase 0 Step 1 (V3P0-1): the routing_observability helper shape
    and its round-trip through the events.jsonl emit path."""

    _FEATURE_KEYS = {
        "observed_prompt_token_count", "step_idx", "stage",
        "context_paths_count",
    }

    def test_helper_returns_five_passive_fields(self) -> None:
        fields = events.routing_observability(
            stage="B", step_idx=2, observed_prompt_token_count=500,
            context_paths_count=4, route_actual="claude-opus-4-7",
        )
        self.assertEqual(set(fields), {
            "route_candidate", "route_actual", "route_fallback_fired",
            "policy_version", "features_seen",
        })
        # Passive: candidate mirrors actual, no fallback, literal stamp.
        self.assertEqual(fields["route_candidate"], "claude-opus-4-7")
        self.assertEqual(fields["route_actual"], "claude-opus-4-7")
        self.assertFalse(fields["route_fallback_fired"])
        self.assertEqual(fields["policy_version"], "v3-phase-0-passive")
        self.assertEqual(fields["policy_version"], events.POLICY_VERSION_PHASE_0)

    def test_features_seen_always_carries_four_keys(self) -> None:
        # All four keys present even when values are None (error-path shape).
        fields = events.routing_observability(
            stage="A", step_idx=None, observed_prompt_token_count=None,
            context_paths_count=None, route_actual="claude-opus-4-7",
        )
        fs = fields["features_seen"]
        self.assertEqual(set(fs), self._FEATURE_KEYS)
        self.assertEqual(fs["stage"], "A")
        self.assertIsNone(fs["observed_prompt_token_count"])
        self.assertIsNone(fs["step_idx"])
        self.assertIsNone(fs["context_paths_count"])

    def test_fields_round_trip_through_jsonl(self) -> None:
        events.begin_run("r1")
        events.emit(
            "planner.stage_b.api_end",
            {
                "model": "claude-opus-4-7", "ok": True,
                **events.routing_observability(
                    stage="B", step_idx=0, observed_prompt_token_count=777,
                    context_paths_count=3, route_actual="claude-opus-4-7",
                ),
            },
            step_idx=0,
        )
        events.end_run()
        rows = self._read_events("r1")
        api_end = [r for r in rows if r["kind"] == "planner.stage_b.api_end"]
        self.assertEqual(len(api_end), 1)
        data = api_end[0]["data"]
        self.assertEqual(data["route_candidate"], "claude-opus-4-7")
        self.assertEqual(data["route_actual"], "claude-opus-4-7")
        self.assertFalse(data["route_fallback_fired"])
        self.assertEqual(data["policy_version"], "v3-phase-0-passive")
        # features_seen survives as a nested object, all four keys intact.
        self.assertEqual(set(data["features_seen"]), self._FEATURE_KEYS)
        self.assertEqual(data["features_seen"]["observed_prompt_token_count"], 777)
        self.assertEqual(data["features_seen"]["stage"], "B")
        self.assertEqual(data["features_seen"]["context_paths_count"], 3)


class TestShadowDecision(_EventsTestBase):
    """v3 Phase 0 Step 2 (V3P0-3): the shadow.decision kind validates and
    emit_shadow_decision produces the right shape + agreement logic."""

    def test_shadow_decision_kind_validates(self) -> None:
        self.assertIn("shadow.decision", events.VALID_KINDS)
        e = events.Event(
            ts="2026-05-20T10:15:42.000+00:00",
            run_id="r1",
            kind="shadow.decision",
            data={},
        )
        self.assertEqual(e.kind, "shadow.decision")

    def test_compute_shadow_decision_is_opus_phase_0(self) -> None:
        # Phase 0 placeholder: unconditionally Opus, ignoring features.
        self.assertEqual(
            events._compute_shadow_decision({"stage": "A"}),
            "claude-opus-4-7",
        )
        self.assertEqual(events.SHADOW_ROUTE_PHASE_0, "claude-opus-4-7")

    def test_emit_shadow_decision_agrees_when_actual_is_opus(self) -> None:
        events.begin_run("r1")
        basis = {"observed_prompt_token_count": 500, "step_idx": 0,
                 "stage": "B", "context_paths_count": 4}
        ok = events.emit_shadow_decision(
            stage="B", step_idx=0, features_seen=basis,
            actual_route_taken="claude-opus-4-7",
        )
        self.assertTrue(ok)
        events.end_run()
        rows = [r for r in self._read_events("r1")
                if r["kind"] == "shadow.decision"]
        self.assertEqual(len(rows), 1)
        d = rows[0]["data"]
        self.assertEqual(d["stage"], "B")
        self.assertEqual(d["shadow_route_candidate"], "claude-opus-4-7")
        self.assertEqual(d["actual_route_taken"], "claude-opus-4-7")
        self.assertTrue(d["agreement"])
        # The basis is the features_seen dict, preserved verbatim.
        self.assertEqual(d["shadow_decision_basis"], basis)
        self.assertEqual(rows[0]["step_idx"], 0)

    def test_emit_shadow_decision_disagrees_when_actual_differs(self) -> None:
        # Agreement is computed, not hardcoded: a non-Opus actual route
        # (cannot happen in Phase 0, but the logic must be correct for
        # Phase 1) yields agreement=False.
        events.begin_run("r1")
        events.emit_shadow_decision(
            stage="A", step_idx=2, features_seen={"stage": "A"},
            actual_route_taken="claude-haiku-4-5",
        )
        events.end_run()
        row = next(r for r in self._read_events("r1")
                   if r["kind"] == "shadow.decision")
        self.assertEqual(row["data"]["shadow_route_candidate"], "claude-opus-4-7")
        self.assertEqual(row["data"]["actual_route_taken"], "claude-haiku-4-5")
        self.assertFalse(row["data"]["agreement"])


class TestStageAComparator(_EventsTestBase):
    """v3 Phase 0 Step 3 (V3P0-4): the comparator helper + the
    shadow_compare emit pair, plus the four new kind validations."""

    def test_new_kinds_validate(self) -> None:
        for k in ("stage_a.shadow_compare.begin", "stage_a.shadow_compare.end",
                  "stage_a.silent_miss.detected", "stage_a.parser_drop"):
            self.assertIn(k, events.VALID_KINDS)
            e = events.Event(ts="2026-05-20T10:15:42.000+00:00",
                             run_id="r1", kind=k, data={})
            self.assertEqual(e.kind, k)

    def test_compare_identity_inputs(self) -> None:
        # Phase 0 shape: routed == baseline → no miss, no hallucination,
        # perfect similarity.
        r = events.compare_stage_a_selections(["a", "b"], ["a", "b"])
        self.assertEqual(r["silent_miss_count"], 0)
        self.assertEqual(r["hallucination_count"], 0)
        self.assertEqual(r["jaccard_similarity"], 1.0)
        self.assertEqual(r["baseline_only_paths"], [])
        self.assertEqual(r["routed_only_paths"], [])

    def test_compare_empty_both_is_jaccard_one(self) -> None:
        # Two empty selections are identical → jaccard 1.0 (not a div-by-0).
        r = events.compare_stage_a_selections([], [])
        self.assertEqual(r["silent_miss_count"], 0)
        self.assertEqual(r["jaccard_similarity"], 1.0)

    def test_compare_silent_miss_and_hallucination(self) -> None:
        # baseline kept {a,b,c}; routed kept {a,x} → b,c silently missed,
        # x hallucinated. jaccard = |{a}| / |{a,b,c,x}| = 1/4.
        r = events.compare_stage_a_selections(["a", "x"], ["a", "b", "c"])
        self.assertEqual(r["silent_miss_count"], 2)
        self.assertEqual(r["baseline_only_paths"], ["b", "c"])
        self.assertEqual(r["hallucination_count"], 1)
        self.assertEqual(r["routed_only_paths"], ["x"])
        self.assertAlmostEqual(r["jaccard_similarity"], 0.25)

    def test_emit_pair_identity_no_silent_miss_detected(self) -> None:
        events.begin_run("r1")
        result = events.emit_stage_a_shadow_compare(
            step_idx=0, routed_paths=["a", "b"], baseline_paths=["a", "b"],
        )
        events.end_run()
        self.assertEqual(result["silent_miss_count"], 0)
        rows = self._read_events("r1")
        kinds = [r["kind"] for r in rows]
        self.assertIn("stage_a.shadow_compare.begin", kinds)
        self.assertIn("stage_a.shadow_compare.end", kinds)
        # silent_miss.detected does NOT fire on identity inputs.
        self.assertNotIn("stage_a.silent_miss.detected", kinds)
        # begin carries only the inputs; end carries the four outputs.
        begin = next(r for r in rows
                     if r["kind"] == "stage_a.shadow_compare.begin")["data"]
        # v3 Phase 1c Step 3: begin also carries baseline_source (Step3C-F1).
        self.assertEqual(set(begin) - {"step_idx"},
                         {"routed_paths", "baseline_paths", "baseline_source"})
        self.assertEqual(begin["baseline_source"], "identity")  # default
        end = next(r for r in rows
                   if r["kind"] == "stage_a.shadow_compare.end")["data"]
        for f in ("silent_miss_count", "hallucination_count",
                  "jaccard_similarity", "baseline_only_paths",
                  "routed_only_paths", "disposition"):
            self.assertIn(f, end)
        self.assertEqual(end["jaccard_similarity"], 1.0)
        # v3 Phase 2d Step 2: routed == baseline == {a,b} → 2 distinct paths
        # (diverse) + no miss → genuine-match.
        self.assertEqual(end["disposition"], "genuine-match")

    def test_emit_pair_silent_miss_fires_detected(self) -> None:
        # Cannot happen in Phase 0 (routed always == baseline), but the
        # path must exist and fire for Phase 1. baseline {a,b} is 2 distinct
        # paths → diverse → a drop is graded genuine-mismatch (v3 Phase 2d).
        events.begin_run("r1")
        events.emit_stage_a_shadow_compare(
            step_idx=1, routed_paths=["a"], baseline_paths=["a", "b"],
        )
        events.end_run()
        rows = self._read_events("r1")
        end = next(r for r in rows
                   if r["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(end["disposition"], "genuine-mismatch")
        detected = [r for r in rows
                    if r["kind"] == "stage_a.silent_miss.detected"]
        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0]["data"]["silent_miss_count"], 1)
        self.assertEqual(detected[0]["data"]["baseline_only_paths"], ["b"])
        self.assertEqual(detected[0]["data"]["disposition"], "genuine-mismatch")


class TestComparatorDisposition(_EventsTestBase):
    """v3 Phase 2d Step 2: the 4-disposition comparator hardening (Q-D4 hybrid,
    N=1 per-row, K=2 per-corpus). The pure classifier + the emit-path gating of
    stage_a.silent_miss.detected on genuine-mismatch only."""

    def _classify(self, routed, baseline, **kw):
        result = events.compare_stage_a_selections(routed, baseline)
        return events.classify_comparator_disposition(result, baseline, **kw)

    def test_vacuous_empty_on_empty_baseline(self) -> None:
        # Per-row N=1: an empty baseline is trivial → vacuous-empty (pass).
        self.assertEqual(self._classify([], []), "vacuous-empty")
        self.assertEqual(self._classify(["x"], []), "vacuous-empty")

    def test_per_row_short_circuits_per_corpus(self) -> None:
        # Precedence: an empty baseline returns vacuous-empty even when the
        # corpus is diverse — the per-corpus check is never reached.
        self.assertEqual(
            self._classify([], [], corpus_baselines=[["a"], ["b"], ["c"]]),
            "vacuous-empty")

    def test_vacuous_uniform_single_path_default_corpus(self) -> None:
        # Per-corpus K=2: a lone non-empty baseline (default corpus = this row)
        # holds 1 distinct path < K → no diversity → vacuous-uniform (pass).
        self.assertEqual(self._classify([], ["a"]), "vacuous-uniform")
        self.assertEqual(self._classify(["a"], ["a"]), "vacuous-uniform")

    def test_vacuous_uniform_explicit_uniform_corpus(self) -> None:
        # A corpus whose baselines are all the same single path → 1 distinct
        # path value across the corpus < K=2 → vacuous-uniform.
        self.assertEqual(
            self._classify(["a"], ["a"], corpus_baselines=[["a"], ["a"], ["a"]]),
            "vacuous-uniform")

    def test_genuine_match_diverse_corpus_no_miss(self) -> None:
        # Diverse corpus (>=2 distinct paths) + non-trivial baseline + the
        # routed selection dropped nothing → genuine-match (pass).
        self.assertEqual(
            self._classify(["a", "b"], ["a", "b"],
                           corpus_baselines=[["a"], ["b"]]),
            "genuine-match")

    def test_genuine_mismatch_diverse_corpus_with_miss(self) -> None:
        # Diverse corpus + non-trivial baseline + a dropped baseline path →
        # genuine-mismatch (the only disposition that records a silent miss).
        self.assertEqual(
            self._classify(["a"], ["a", "b"], corpus_baselines=[["a"], ["b"]]),
            "genuine-mismatch")

    def test_hallucination_only_is_not_a_silent_miss(self) -> None:
        # Match/mismatch keys on dropped context, not full-set inequality: the
        # routed selection holds an EXTRA path (hallucination) but dropped none
        # → silent_miss_count 0 → genuine-match, not genuine-mismatch.
        self.assertEqual(
            self._classify(["a", "b", "x"], ["a", "b"],
                           corpus_baselines=[["a"], ["b"]]),
            "genuine-match")

    def test_unknown_on_malformed_input(self) -> None:
        # Defensive never-raise: an un-setifiable baseline → unknown.
        self.assertEqual(
            events.classify_comparator_disposition({"silent_miss_count": 0},
                                                   object()),
            "unknown")

    def test_emit_empty_context_records_vacuous_empty_no_detected(self) -> None:
        # The Phase 2d exit-sweep shape: routed == baseline == [] → vacuous-empty
        # recorded explicitly, no silent_miss.detected (Step3C-F1 hardening:
        # the implicit Phase 1c pass is now explicit).
        events.begin_run("r1")
        events.emit_stage_a_shadow_compare(
            step_idx=0, routed_paths=[], baseline_paths=[])
        events.end_run()
        rows = self._read_events("r1")
        end = next(r for r in rows
                   if r["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(end["disposition"], "vacuous-empty")
        self.assertNotIn("stage_a.silent_miss.detected",
                         [r["kind"] for r in rows])

    def test_emit_vacuous_uniform_with_drop_does_not_fire_detected(self) -> None:
        # The behaviour CHANGE vs Phase 1c-2c: a single-path baseline the routed
        # selection dropped (silent_miss_count=1) is vacuous-uniform (no
        # diversity to grade) → pass, NOT a silent miss. The old binary gate
        # (silent_miss_count > 0) would have fired here; the hardened gate does
        # not. Moot on the empty-context corpus (no non-empty baselines exist).
        events.begin_run("r1")
        result = events.emit_stage_a_shadow_compare(
            step_idx=0, routed_paths=[], baseline_paths=["a"])
        events.end_run()
        self.assertEqual(result["silent_miss_count"], 1)  # a path WAS dropped
        rows = self._read_events("r1")
        end = next(r for r in rows
                   if r["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(end["disposition"], "vacuous-uniform")
        self.assertNotIn("stage_a.silent_miss.detected",
                         [r["kind"] for r in rows])

    def test_emit_threads_corpus_baselines_lifts_single_path_to_genuine_match(
            self) -> None:
        # v3 Phase 3 3b (β-i, Rev B §B.1/B.2): the threading the planner relies
        # on. The SAME single-path identity comparison is vacuous-uniform under
        # the single-row default (corpus_baselines=None) but genuine-match when
        # a diverse cross-row corpus is threaded through — proving
        # emit_stage_a_shadow_compare carries corpus_baselines to the classifier
        # (events.py:451). This is what lifts the single-path accept-shapes
        # (T7/T8/T9/T10/T12) out of vacuous-uniform so 3c can calibrate them.
        events.begin_run("r-default")
        events.emit_stage_a_shadow_compare(
            step_idx=0, routed_paths=["a"], baseline_paths=["a"])
        events.end_run()
        end_default = next(
            r for r in self._read_events("r-default")
            if r["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(end_default["disposition"], "vacuous-uniform")

        events.begin_run("r-corpus")
        events.emit_stage_a_shadow_compare(
            step_idx=0, routed_paths=["a"], baseline_paths=["a"],
            corpus_baselines=[["a", "b"]])  # cross-row vocab wrapped as one sel
        events.end_run()
        end_corpus = next(
            r for r in self._read_events("r-corpus")
            if r["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(end_corpus["disposition"], "genuine-match")
        self.assertEqual(end_corpus["silent_miss_count"], 0)

    def test_emit_corpus_baselines_with_drop_is_genuine_mismatch(self) -> None:
        # v3 Phase 3 3b (β-i): the T11-shaped case. Opus selects 2 paths, Haiku
        # drops one; the populated cross-row corpus passes K=2, so the drop is
        # NOT excused as vacuous-uniform — it is graded genuine-mismatch and
        # fires stage_a.silent_miss.detected. This is the reject-path evidence
        # 3c consumes (and the one designed mismatch in the live 3b sweep).
        events.begin_run("r-mismatch")
        result = events.emit_stage_a_shadow_compare(
            step_idx=0, routed_paths=["a"], baseline_paths=["a", "b"],
            corpus_baselines=[["a", "b"]])  # K=2 satisfied
        events.end_run()
        self.assertEqual(result["silent_miss_count"], 1)  # "b" dropped
        rows = self._read_events("r-mismatch")
        end = next(r for r in rows
                   if r["kind"] == "stage_a.shadow_compare.end")["data"]
        self.assertEqual(end["disposition"],
                         events.DISPOSITION_GENUINE_MISMATCH)
        self.assertNotEqual(end["disposition"], events.DISPOSITION_GENUINE_MATCH)
        detected = [r for r in rows
                    if r["kind"] == "stage_a.silent_miss.detected"]
        self.assertEqual(len(detected), 1)  # the mismatch fires the episode


class TestCacheDiagnosticsHelpers(_EventsTestBase):
    """v3 Phase 0 Step 4 (V3P0-6): the token estimator, block-size
    decomposition, and cache_diagnostics packaging."""

    def test_estimate_tokens_four_chars_per_token(self) -> None:
        self.assertEqual(events._estimate_tokens(""), 0)
        self.assertEqual(events._estimate_tokens("abcd"), 1)        # 4/4
        self.assertEqual(events._estimate_tokens("abcdefgh"), 2)    # 8/4
        self.assertEqual(events._estimate_tokens("a"), 1)           # ceil(1/4)
        self.assertEqual(events._estimate_tokens(None), 0)          # never-raise

    def test_estimate_user_block_sizes(self) -> None:
        sizes = events.estimate_user_block_sizes({
            "brief": "x" * 400, "state": "y" * 40,
            "vault_files": "", "prior_step": "z" * 4,
        })
        self.assertEqual(sizes, {"brief": 100, "state": 10,
                                 "vault_files": 0, "prior_step": 1})

    def test_cache_diagnostics_packages_three_fields(self) -> None:
        d = events.cache_diagnostics(
            vault_index_hit=False,
            candidate_user_block_sizes={"brief": 5},
            seconds_since_cache_creation=None,
        )
        self.assertEqual(set(d), {
            "vault_index_hit", "candidate_user_block_sizes",
            "seconds_since_cache_creation",
        })
        self.assertIs(d["vault_index_hit"], False)
        self.assertEqual(d["candidate_user_block_sizes"], {"brief": 5})
        self.assertIsNone(d["seconds_since_cache_creation"])

    def test_cache_diagnostics_round_trips_through_jsonl(self) -> None:
        events.begin_run("r1")
        events.emit(
            "planner.stage_a.api_end",
            {"model": "claude-opus-4-7", "ok": True, "input_tokens": 9000,
             **events.cache_diagnostics(
                 vault_index_hit=True,
                 candidate_user_block_sizes={"brief": 800, "state": 100,
                                             "vault_files": 40, "prior_step": 0},
                 seconds_since_cache_creation=12.5)},
            step_idx=0,
        )
        events.end_run()
        d = next(r for r in self._read_events("r1")
                 if r["kind"] == "planner.stage_a.api_end")["data"]
        self.assertIs(d["vault_index_hit"], True)
        self.assertEqual(d["candidate_user_block_sizes"]["vault_files"], 40)
        self.assertEqual(d["seconds_since_cache_creation"], 12.5)


if __name__ == "__main__":
    unittest.main()
