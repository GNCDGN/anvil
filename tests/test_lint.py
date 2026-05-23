"""v3 Phase 1a Step 2 — brief lint tests.

Hermetic: lint operates on a Brief OBJECT, so these construct Brief models
directly (the test_brief.py idiom) — no .md fixtures, no vault/git deps. The
five rules, the seven structured_features, the confidence_band values, the
no-mutation guarantee, the JSON round-trip, and the never-raise contract.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from anvil.brief import Brief, Step
from anvil.lint import LintResult, lint_brief
from anvil.state import State


_SEVEN_FEATURES = {
    "brief_token_estimate", "step_count", "total_scope_files",
    "has_vps_deploy", "has_end_to_end_test", "context_paths_count",
    "confidence_band",
}


def _step(n, *, ops=("write", "smoke-test"), confirm="auto",
          files=("a.py",), name=None, smoke="echo x", notes=None) -> Step:
    return Step(
        number=n, name=name or f"Step {n}",
        scope_files=list(files), scope_operations=list(ops),
        smoke=smoke, confirm=confirm, notes=notes,
    )


def _brief(steps, *, vps_deploy="no", service_name=None, vps_target_path=None,
           end_to_end_test=None, goal="goal", context_links=None,
           context_paths=None, parse_warnings=None) -> Brief:
    return Brief(
        brief_version=1, project="anvil", build_name="lint-test",
        target_repo="x", target_repo_path=Path("/tmp"),
        vps_deploy=vps_deploy, service_name=service_name,
        vps_target_path=vps_target_path, goal=goal,
        context_links=context_links or [], context_paths=context_paths or [],
        steps=steps, end_to_end_test=end_to_end_test,
        parse_warnings=parse_warnings or [],
    )


def _t5_brief() -> Brief:
    """The exact T5 calibration shape: vps_deploy=yes, 3 auto steps,
    write/smoke-test ops, no end_to_end_test."""
    return _brief(
        [_step(1), _step(2), _step(3)],
        vps_deploy="yes", service_name="svc.service",
        vps_target_path="/home/app",
    )


class TestLintBriefShape(unittest.TestCase):

    def test_lint_brief_populates_both_fields(self) -> None:
        r = lint_brief(_brief([_step(1, confirm="explicit"), _step(2)]))
        self.assertIsInstance(r, LintResult)
        self.assertIsInstance(r.advisory_warnings, list)
        self.assertEqual(set(r.structured_features), _SEVEN_FEATURES)
        # Feature types/values for this in-corpus brief.
        sf = r.structured_features
        self.assertEqual(sf["step_count"], 2)
        self.assertEqual(sf["total_scope_files"], 2)
        self.assertFalse(sf["has_vps_deploy"])
        self.assertFalse(sf["has_end_to_end_test"])
        self.assertEqual(sf["context_paths_count"], 0)
        self.assertGreater(sf["brief_token_estimate"], 0)
        self.assertEqual(sf["confidence_band"], "high")


class TestLintRules(unittest.TestCase):

    def test_scope_files_likely_dont_exist_augments_parse_warnings(self) -> None:
        r = lint_brief(_brief(
            [_step(1, files=["missing.py"]), _step(2, files=["typo.py"])],
            parse_warnings=[
                {"kind": "path-not-found", "step_number": 1,
                 "path": "missing.py", "closest_match": None},
                {"kind": "path-not-found", "step_number": 2,
                 "path": "typo.py", "closest_match": "typoo.py"},
            ],
        ))
        joined = "\n".join(r.advisory_warnings)
        self.assertIn("missing.py", joined)
        self.assertIn("high-confidence-missing", joined)   # closest_match None
        self.assertIn("typo.py", joined)
        self.assertIn("may-reconcile", joined)             # closest_match present
        self.assertIn("'typoo.py'", joined)

    def test_confirm_explicit_density_fires_only_at_100(self) -> None:
        all_explicit = lint_brief(_brief(
            [_step(1, confirm="explicit"), _step(2, confirm="explicit")]))
        self.assertTrue(any("confirm density" in w
                            for w in all_explicit.advisory_warnings))
        mixed = lint_brief(_brief(
            [_step(1, confirm="explicit"), _step(2, confirm="auto")]))
        self.assertFalse(any("confirm density" in w
                             for w in mixed.advisory_warnings))

    def test_deploy_chain_unsupported_shape(self) -> None:
        # T5 shape exactly → no deploy warning, band stays high.
        t5 = lint_brief(_t5_brief())
        self.assertFalse(any("deploy chain" in w for w in t5.advisory_warnings))
        self.assertEqual(t5.confidence_band, "high")
        # Deviation (declares an end_to_end_test) → fires + escalates band.
        from anvil.brief import EndToEndTest
        deviant = _t5_brief()
        deviant = deviant.model_copy(update={
            "end_to_end_test": EndToEndTest(script="x.sh"),
        })
        r = lint_brief(deviant)
        self.assertTrue(any("deploy chain" in w for w in r.advisory_warnings))
        self.assertEqual(r.confidence_band, "unsupported-shape")
        # Deviation (2 steps instead of 3) → also fires.
        two_step = _brief(
            [_step(1), _step(2)], vps_deploy="yes",
            service_name="svc.service", vps_target_path="/home/app")
        self.assertTrue(any("deploy chain" in w
                            for w in lint_brief(two_step).advisory_warnings))

    def test_step_count_high_advisory_above_six(self) -> None:
        seven = lint_brief(_brief([_step(i) for i in range(1, 8)]))
        self.assertTrue(any("step count" in w
                            for w in seven.advisory_warnings))
        # 7 steps is >6 (advisory) but ≤10 → band stays high (no escalation).
        self.assertEqual(seven.confidence_band, "high")
        # ≤6 steps → no advisory.
        six = lint_brief(_brief([_step(i) for i in range(1, 7)]))
        self.assertFalse(any("step count" in w for w in six.advisory_warnings))

    def test_scope_operations_unsupported_defensive(self) -> None:
        # Raw Brief with an op outside the v2-canonical set (bypasses
        # validate_or_reject, which would normally reject it — Step2-F1).
        r = lint_brief(_brief([_step(1, ops=["write", "frobnicate"])]))
        self.assertTrue(any("frobnicate" in w for w in r.advisory_warnings))
        self.assertEqual(r.confidence_band, "unsupported-shape")


class TestConfidenceBand(unittest.TestCase):
    """Phase 1a emits only 'high' or 'unsupported-shape' — never 'medium'
    (reserved for Phase 1b calibration). This invariant is load-bearing."""

    def test_band_values_high_and_unsupported_never_medium(self) -> None:
        cases = [
            lint_brief(_brief([_step(1), _step(2), _step(3)])),          # high
            lint_brief(_brief([_step(i) for i in range(1, 12)])),        # >10
            lint_brief(_t5_brief().model_copy(update={
                "steps": [_step(1), _step(2)]})),                        # !T5
            lint_brief(_brief([_step(1, ops=["nope"])])),                # bad op
        ]
        bands = [c.confidence_band for c in cases]
        self.assertEqual(bands[0], "high")
        self.assertEqual(bands[1], "unsupported-shape")
        self.assertEqual(bands[2], "unsupported-shape")
        self.assertEqual(bands[3], "unsupported-shape")
        self.assertNotIn("medium", bands)


class TestLintContract(unittest.TestCase):

    def test_lint_never_mutates_brief(self) -> None:
        brief = _brief(
            [_step(1, confirm="explicit"), _step(2)],
            parse_warnings=[{"kind": "path-not-found", "step_number": 1,
                             "path": "x.py", "closest_match": None}],
        )
        before = brief.model_dump()
        lint_brief(brief)
        self.assertEqual(brief.model_dump(), before)

    def test_lint_result_json_round_trip(self) -> None:
        r = lint_brief(_brief([_step(1), _step(2), _step(3)]))
        back = LintResult.model_validate_json(r.model_dump_json())
        self.assertEqual(back.model_dump(), r.model_dump())
        # And embedded in State (the real persistence path).
        st = State(brief_path="x", started_at="2026-01-01", lint_result=r)
        st_back = State.model_validate_json(st.model_dump_json())
        self.assertEqual(st_back.lint_result.model_dump(), r.model_dump())
        self.assertEqual(st_back.lint_result.confidence_band, r.confidence_band)

    def test_lint_brief_never_raises_on_malformed(self) -> None:
        class _Boom:
            goal = "g"

            @property
            def steps(self):  # property that raises, not AttributeError
                raise RuntimeError("boom")

        r = lint_brief(_Boom())
        self.assertIsInstance(r, LintResult)
        self.assertTrue(any("lint failed" in w for w in r.advisory_warnings))
        # All seven keys still present (degraded safe defaults).
        self.assertEqual(set(r.structured_features), _SEVEN_FEATURES)
        self.assertEqual(r.confidence_band, "unsupported-shape")

    def test_lint_tolerates_simplenamespace_brief(self) -> None:
        # The defensive getattr idiom: a partial SimpleNamespace brief lints
        # without raising (feeds the never-raise contract).
        ns = SimpleNamespace(goal="g", steps=[], context_links=[])
        r = lint_brief(ns)
        self.assertIsInstance(r, LintResult)
        self.assertEqual(set(r.structured_features), _SEVEN_FEATURES)


if __name__ == "__main__":
    unittest.main()
