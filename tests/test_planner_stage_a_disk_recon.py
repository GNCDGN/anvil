"""Phase 2 Step 6 — Stage A disk-reconciliation-note tests.

Separate file from tests/test_planner_stage_a.py to avoid disturbing the
Phase 1 test structure. Four cases per the brief:

  (a) brief with no parse_warnings → Stage A prompt contains no note block
  (b) brief with parse_warnings for the current step → note block present
  (c) brief with parse_warnings for a DIFFERENT step → no note for current step
  (d) multiple parse_warnings for current step → all rendered in the note
"""
from __future__ import annotations

import unittest
from pathlib import Path

from anvil import planner
from anvil.brief import Brief, Step
from anvil.state import init_state


def _make_brief(parse_warnings=None) -> Brief:
    """Construct a Brief object directly (no on-disk markdown) with two
    steps. We bypass parse_brief here because the warnings field is the
    test surface; constructing the Brief by hand is the cleanest way to
    parametrise the warnings."""
    brief = Brief(
        brief_version=1, project="anvil",
        build_name="stage-a-disk-recon",
        target_repo="x", target_repo_path=Path("/tmp"),
        vps_deploy="no",
        steps=[
            Step(number=1, name="One",
                 scope_files=["a.py"], scope_operations=["write", "commit"],
                 smoke="echo s1", confirm="explicit"),
            Step(number=2, name="Two",
                 scope_files=["b.py"], scope_operations=["write", "commit"],
                 smoke="echo s2", confirm="explicit"),
        ],
        parse_warnings=parse_warnings or [],
    )
    return brief


def _state_for(brief: Brief):
    return init_state(brief, "2026-05-18T00:00:00",
                      brief_path="/tmp/stage-a-disk-recon.md")


class StageADiskReconciliationNoteTests(unittest.TestCase):

    # --- (a) no parse_warnings → no note block ---
    def test_a_no_warnings_no_note(self):
        brief = _make_brief(parse_warnings=[])
        state = _state_for(brief)
        prompt = planner._assemble_stage_a_prompt(brief, state, 0, {})
        self.assertNotIn("[disk-reconciliation-note]", prompt)

    # --- (b) parse_warning for current step → note appears ---
    def test_b_warning_for_current_step_renders(self):
        warnings = [{
            "kind": "path-not-found", "step_number": 1,
            "path": "reporter/chat_handler.py",
            "closest_match": "chat_handler.py",
        }]
        brief = _make_brief(parse_warnings=warnings)
        state = _state_for(brief)
        prompt = planner._assemble_stage_a_prompt(brief, state, 0, {})
        self.assertIn("[disk-reconciliation-note]", prompt)
        self.assertIn("reporter/chat_handler.py", prompt)
        self.assertIn("chat_handler.py", prompt)
        self.assertIn("escalation_triggers", prompt)

    # --- (c) warning for a DIFFERENT step → no note for current step ---
    def test_c_warning_for_other_step_does_not_render(self):
        # Warning is for step 2; current step is 0 (step_number=1).
        warnings = [{
            "kind": "path-not-found", "step_number": 2,
            "path": "other/path.py", "closest_match": None,
        }]
        brief = _make_brief(parse_warnings=warnings)
        state = _state_for(brief)
        prompt = planner._assemble_stage_a_prompt(brief, state, 0, {})
        self.assertNotIn("[disk-reconciliation-note]", prompt)
        self.assertNotIn("other/path.py", prompt)

    # --- (d) multiple warnings for current step → all rendered ---
    def test_d_multiple_warnings_all_render(self):
        warnings = [
            {"kind": "path-not-found", "step_number": 1,
             "path": "first/missing.py", "closest_match": "missing.py"},
            {"kind": "path-not-found", "step_number": 1,
             "path": "second/missing.py", "closest_match": None},
        ]
        brief = _make_brief(parse_warnings=warnings)
        state = _state_for(brief)
        prompt = planner._assemble_stage_a_prompt(brief, state, 0, {})
        self.assertEqual(prompt.count("[disk-reconciliation-note]"), 2)
        self.assertIn("first/missing.py", prompt)
        self.assertIn("second/missing.py", prompt)
        # The single-match case renders the match in quotes; the no-match
        # case renders "(no single close match found)"
        self.assertIn("'missing.py'", prompt)
        self.assertIn("(no single close match found)", prompt)


if __name__ == "__main__":
    unittest.main()
