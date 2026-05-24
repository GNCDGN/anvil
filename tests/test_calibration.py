"""v3 Phase 1b Step 1 — routing calibration substrate tests.

RoutingCalibration derives an empirically-gated Stage A predicate from historical
shadow data; CalibratedPolicy evaluates features → RouteRecommendation. Unit tests
use synthetic samples (hermetic, no DB); one integration test uses the real Phase
1a exit-sweep DuckDB when present (skipped otherwise).
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from anvil.calibration import (
    CHEAP_STAGE_A_MODEL,
    CalibratedPolicy,
    RouteRecommendation,
    RoutingCalibration,
)
from anvil.policy import PHASE_1A_PLACEHOLDER_MODEL

_EXIT_SWEEP_DB = (
    Path(__file__).resolve().parent.parent
    / "state" / "v3-phase-1a" / "exit-sweep.duckdb"
)

_PHASE_1B_DB = (
    Path(__file__).resolve().parent.parent
    / "state" / "v3-phase-1b" / "exit-sweep.duckdb"
)


def _samples(*pairs):
    """Build calibration samples from (context_paths_count, paths_returned) pairs."""
    return [{"context_paths_count": c, "paths_returned": p} for c, p in pairs]


def _write_synthetic_stage_a_db(path, *, with_selected_paths: bool, n: int = 3):
    """Build a minimal DuckDB with the (shadow_decisions ⋈ planner.stage_a.parsed)
    shape `RoutingCalibration.from_db` joins, holding `n` empty-context Stage A
    rows. `with_selected_paths=True` marks it Phase-2a+ (events.data carries a
    `selected_paths` key); False is the legacy 1a/1b shape. The join reads only
    `paths_returned`, so the two schemas must derive an identical predicate."""
    import duckdb
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE shadow_decisions(run_id VARCHAR, mode VARCHAR, "
                "step_idx INTEGER, stage VARCHAR, shadow_decision_basis JSON)")
    con.execute("CREATE TABLE events(run_id VARCHAR, mode VARCHAR, "
                "step_idx INTEGER, kind VARCHAR, data JSON)")
    data = ('{"paths_returned": 0, "selected_paths": []}' if with_selected_paths
            else '{"paths_returned": 0}')
    for i in range(n):
        rid = f"T{i + 1}"
        con.execute("INSERT INTO shadow_decisions VALUES (?, 'real', 0, 'A', ?)",
                    [rid, '{"context_paths_count": 0, "stage": "A"}'])
        con.execute("INSERT INTO events VALUES "
                    "(?, 'real', 0, 'planner.stage_a.parsed', ?)", [rid, data])
    con.close()


class TestRoutingCalibrationDerivation(unittest.TestCase):

    def test_derives_calibrated_predicate_from_synthetic_samples(self) -> None:
        cal = RoutingCalibration(_samples((0, 0), (0, 0), (0, 0)))
        self.assertTrue(cal.predicate_state["empty_context_calibrated"])
        self.assertEqual(cal.predicate_state["n_empty_context_samples"], 3)
        self.assertEqual(cal.predicate_state["n_empty_context_zero_paths"], 3)

    def test_predicate_degrades_if_any_empty_context_selected_paths(self) -> None:
        # Empirical gate (Step1B-F1): one empty-context call selected 2 paths →
        # the equivalence is broken → the whole feature degrades to uncalibrated.
        cal = RoutingCalibration(_samples((0, 0), (0, 2), (0, 0)))
        self.assertFalse(cal.predicate_state["empty_context_calibrated"])
        rec = cal.policy({"context_paths_count": 0})
        self.assertEqual(rec.recommended_model, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(rec.confidence_band, "unsupported-shape")

    def test_empty_input_degrades(self) -> None:
        cal = RoutingCalibration([])
        self.assertFalse(cal.predicate_state["empty_context_calibrated"])
        rec = cal.policy({"context_paths_count": 0})
        self.assertEqual(rec.recommended_model, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(rec.confidence_band, "unsupported-shape")

    def test_malformed_samples_handled_gracefully(self) -> None:
        # Samples missing keys must not crash derivation; excluded from the gate.
        # No valid empty-context-with-paths_returned sample → not calibrated.
        cal = RoutingCalibration([{"foo": 1}, {"context_paths_count": 0}])
        self.assertFalse(cal.predicate_state["empty_context_calibrated"])

    def test_derivation_is_eager(self) -> None:
        # Predicate frozen at construction: mutating the input list afterwards
        # does not change the derived policy (no re-derivation per call).
        samples = _samples((0, 0))
        cal = RoutingCalibration(samples)
        self.assertTrue(cal.predicate_state["empty_context_calibrated"])
        samples.append({"context_paths_count": 0, "paths_returned": 5})
        self.assertEqual(cal.predicate_state["n_empty_context_samples"], 1)
        self.assertEqual(
            cal.policy({"context_paths_count": 0}).recommended_model,
            CHEAP_STAGE_A_MODEL,
        )


class TestCalibratedPolicyEvaluation(unittest.TestCase):

    def setUp(self) -> None:
        self.cal = RoutingCalibration(_samples((0, 0), (0, 0)))

    def test_recommends_haiku_high_on_empty_context(self) -> None:
        rec = self.cal.policy({"context_paths_count": 0})
        self.assertIsInstance(rec, RouteRecommendation)
        self.assertEqual(rec.recommended_model, CHEAP_STAGE_A_MODEL)
        self.assertEqual(rec.confidence_band, "high")

    def test_opus_unsupported_on_uncalibrated_feature_value(self) -> None:
        rec = self.cal.policy({"context_paths_count": 3})
        self.assertEqual(rec.recommended_model, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(rec.confidence_band, "unsupported-shape")

    def test_opus_unsupported_on_missing_feature(self) -> None:
        rec = self.cal.policy({})
        self.assertEqual(rec.recommended_model, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(rec.confidence_band, "unsupported-shape")

    def test_rationale_carries_feature_values(self) -> None:
        rec = self.cal.policy({"context_paths_count": 0})
        self.assertEqual(rec.rationale["feature"], "context_paths_count")
        self.assertEqual(rec.rationale["feature_value"], 0)
        self.assertTrue(rec.rationale["calibrated"])
        self.assertEqual(rec.rationale["n_empty_context_samples"], 2)

    def test_never_raises_degraded_on_bad_features(self) -> None:
        class _Boom:
            @property
            def context_paths_count(self):  # raises, not AttributeError
                raise RuntimeError("boom")

        rec = self.cal.policy(_Boom())
        self.assertEqual(rec.recommended_model, PHASE_1A_PLACEHOLDER_MODEL)
        self.assertEqual(rec.confidence_band, "unsupported-shape")
        self.assertIn("error", rec.rationale)

    def test_tolerates_simplenamespace_features(self) -> None:
        # A4 defensive idiom: a SimpleNamespace carrying the attr works.
        rec = self.cal.policy(SimpleNamespace(context_paths_count=0))
        self.assertEqual(rec.recommended_model, CHEAP_STAGE_A_MODEL)


class TestCalibrationFromConnection(unittest.TestCase):

    @unittest.skipUnless(
        _EXIT_SWEEP_DB.is_file(),
        f"integration fixture {_EXIT_SWEEP_DB} not present",
    )
    def test_from_connection_derives_from_real_exit_sweep(self) -> None:
        import duckdb
        con = duckdb.connect(str(_EXIT_SWEEP_DB), read_only=True)
        try:
            cal = RoutingCalibration.from_connection(con)
        finally:
            con.close()
        # Phase 1a corpus: every empty-context Stage A call returned 0 paths.
        ps = cal.predicate_state
        self.assertTrue(ps["empty_context_calibrated"])
        self.assertGreater(ps["n_empty_context_samples"], 0)
        self.assertEqual(ps["n_empty_context_samples"], ps["n_empty_context_zero_paths"])
        rec = cal.policy({"context_paths_count": 0})
        self.assertEqual(rec.recommended_model, CHEAP_STAGE_A_MODEL)
        self.assertEqual(rec.confidence_band, "high")

    @unittest.skipUnless(
        _EXIT_SWEEP_DB.is_file(),
        f"integration fixture {_EXIT_SWEEP_DB} not present",
    )
    def test_from_db_opens_path_and_derives(self) -> None:
        # v3 Phase 1b Step 2: from_db opens the DuckDB read-only, derives, closes.
        cal = RoutingCalibration.from_db(_EXIT_SWEEP_DB)
        self.assertTrue(cal.predicate_state["empty_context_calibrated"])
        self.assertEqual(
            cal.policy({"context_paths_count": 0}).recommended_model,
            CHEAP_STAGE_A_MODEL)

    def test_from_db_missing_file_degrades_never_raises(self) -> None:
        # A missing/broken DB must never block the build — empty corpus,
        # degraded predicate (the orchestrator relies on this).
        cal = RoutingCalibration.from_db("/tmp/anvil-no-such-calibration.duckdb")
        self.assertFalse(cal.predicate_state["empty_context_calibrated"])
        self.assertEqual(
            cal.policy({"context_paths_count": 0}).recommended_model,
            PHASE_1A_PLACEHOLDER_MODEL)


class TestFromDbSchemaParity(unittest.TestCase):
    """v3 Phase 2d Step 1 (Q-D2 verification): `from_db` derives the same
    empty-context constant from a legacy DB (Phase 1a/1b, no `selected_paths`)
    and a Phase-2a+ DB (with `selected_paths`). The derivation reads
    `paths_returned` only, so the Phase-2a+ schema is a no-op for the existing
    pipeline — confirming it is stable to extend from. The rich-context
    derivation that WOULD read `selected_paths` is Phase 2d2's new logic."""

    @unittest.skipUnless(
        _PHASE_1B_DB.is_file(),
        f"integration fixture {_PHASE_1B_DB} not present",
    )
    def test_from_db_real_phase1b_derives_production_constant(self) -> None:
        # The current production calibration source: Phase 1b exit-sweep →
        # empty-context calibrated, every empty-context call returned 0 paths.
        cal = RoutingCalibration.from_db(_PHASE_1B_DB)
        ps = cal.predicate_state
        self.assertTrue(ps["empty_context_calibrated"])
        self.assertGreater(ps["n_empty_context_samples"], 0)
        self.assertEqual(ps["n_empty_context_samples"], ps["n_empty_context_zero_paths"])
        rec = cal.policy({"context_paths_count": 0})
        self.assertEqual(rec.recommended_model, CHEAP_STAGE_A_MODEL)
        self.assertEqual(rec.confidence_band, "high")

    def test_from_db_synthetic_phase2a_parity_on_empty_context(self) -> None:
        # A synthetic Phase-2a+ DB (selected_paths recorded, all empty-context)
        # derives the SAME constant as the legacy production source: calibrated,
        # Haiku-high. Behaviour parity across schema versions.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "phase2a.duckdb"
            _write_synthetic_stage_a_db(db, with_selected_paths=True)
            cal = RoutingCalibration.from_db(db)
            self.assertTrue(cal.predicate_state["empty_context_calibrated"])
            rec = cal.policy({"context_paths_count": 0})
            self.assertEqual(rec.recommended_model, CHEAP_STAGE_A_MODEL)
            self.assertEqual(rec.confidence_band, "high")

    def test_from_db_schema_agnostic_to_selected_paths(self) -> None:
        # The substantive parity: legacy and Phase-2a+ DBs with identical
        # empty-context rows derive identical predicate_state — from_db ignores
        # selected_paths entirely (Q-D2: rich-context derivation is new logic).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / "legacy.duckdb"
            phase2a = Path(d) / "phase2a.duckdb"
            _write_synthetic_stage_a_db(legacy, with_selected_paths=False)
            _write_synthetic_stage_a_db(phase2a, with_selected_paths=True)
            self.assertEqual(
                RoutingCalibration.from_db(legacy).predicate_state,
                RoutingCalibration.from_db(phase2a).predicate_state)


if __name__ == "__main__":
    unittest.main()
