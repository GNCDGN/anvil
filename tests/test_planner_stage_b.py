"""Tests for Phase 1 Stage B loader / assembly / parse — Step 4.

Covers _load_files, _assemble_stage_b_prompt, _parse_plan_json. Validation
(_validate_plan_structure) is tested separately in
test_planner_validation.py. No Anthropic call anywhere — Step 5 introduces
_call_anthropic.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from anvil import planner
from anvil.brief import parse_brief
from anvil.state import init_state

_FIX = Path(__file__).resolve().parent / "fixtures" / "planner"
_TRIVIAL_BRIEF = Path(__file__).resolve().parent / "fixtures" / "trivial-test-brief.md"


def _brief_and_state():
    brief = parse_brief(_TRIVIAL_BRIEF)
    state = init_state(brief, "2026-05-18T00:00:00", brief_path=str(_TRIVIAL_BRIEF))
    return brief, state


class AssembleStageBPromptTests(unittest.TestCase):
    def test_assemble_sections_present_and_ordered(self):
        """Asserts substituted placeholders absent BY NAME, not a blanket
        "{" check: the embedded JSON state and vault-file blocks contain
        literal { } braces that are correct content. Do not tighten this.
        """
        brief, state = _brief_and_state()
        result = planner._assemble_stage_b_prompt(brief, state, 0, {})
        for token in (
            "{BRIEF_MARKDOWN}",
            "{STATE_JSON}",
            "{PRIOR_STEP_BLOCK}",
            "{VAULT_FILES_BLOCKS}",
            "{STEP_NUMBER}",
            "{STEP_NAME}",
            "{STEP_SCOPE_FILES}",
            "{STEP_SCOPE_OPERATIONS}",
            "{STEP_NOTES}",
            "{CONTEXT_PATHS}",
        ):
            self.assertNotIn(token, result)
        i_brief = result.index("## Build brief")
        i_prior = result.index("## Prior step outcome")
        i_files = result.index("## Selected vault files")
        i_step = result.index("## Step being planned")
        i_instr = result.index("## Instruction")
        self.assertLess(i_brief, i_prior)
        self.assertLess(i_prior, i_files)
        self.assertLess(i_files, i_step)
        self.assertLess(i_step, i_instr)

    def test_prior_step_block_first_step(self):
        brief, state = _brief_and_state()
        result = planner._assemble_stage_b_prompt(brief, state, 0, {})
        self.assertIn("(none — this is the first step)", result)

    def test_prior_step_block_later_step(self):
        brief, state = _brief_and_state()
        state.steps[0].plan = {"step_number": 1, "approach": "did the thing"}
        state.steps[0].smoke = "pass"
        state.steps[0].commit = "abc1234"
        result = planner._assemble_stage_b_prompt(brief, state, 1, {})
        self.assertIn(f"Step {state.steps[0].n}:", result)
        self.assertIn("Plan: ", result)
        self.assertIn("Smoke test result: pass", result)
        self.assertIn("Commit hash: abc1234", result)
        self.assertNotIn("(none — this is the first step)", result)

    def test_vault_files_blocks_render(self):
        brief, state = _brief_and_state()
        result = planner._assemble_stage_b_prompt(
            brief, state, 0, {"x.py": "print(1)"}
        )
        self.assertIn('<vault_file path="x.py">', result)
        self.assertIn("print(1)", result)
        self.assertIn("</vault_file>", result)

    def test_vault_files_blocks_none_selected(self):
        brief, state = _brief_and_state()
        result = planner._assemble_stage_b_prompt(brief, state, 0, {})
        self.assertIn("## Selected vault files", result)
        self.assertIn("(none selected)", result)


class ParsePlanJsonTests(unittest.TestCase):
    def test_parse_plan_json_valid(self):
        text = (_FIX / "stage_b_valid_plan.txt").read_text(encoding="utf-8")
        plan = planner._parse_plan_json(text)
        self.assertEqual(plan["step_number"], 1)
        self.assertEqual(plan["confidence"], "high")

    def test_parse_plan_json_fenced_raises(self):
        text = (_FIX / "stage_b_with_fence.txt").read_text(encoding="utf-8")
        with self.assertRaises(planner.PlanParseError):
            planner._parse_plan_json(text)

    def test_parse_plan_json_prose_raises(self):
        text = 'Here is the plan you asked for:\n{"step_number": 1}\nDone.'
        with self.assertRaises(planner.PlanParseError):
            planner._parse_plan_json(text)


class LoadFilesTests(unittest.TestCase):
    def test_load_files_truncates_at_50k(self):
        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "big.txt"
            big.write_text("x" * 50_001, encoding="utf-8")
            out = planner._load_files([str(big)], Path(d))
        content = out[str(big)]
        self.assertTrue(content.endswith("\n\n[... truncated at 50000 chars]"))
        before = content[: -len("\n\n[... truncated at 50000 chars]")]
        self.assertEqual(len(before), 50_000)

    def test_load_files_missing_file_omitted(self):
        with tempfile.TemporaryDirectory() as d:
            present = Path(d) / "here.txt"
            present.write_text("content", encoding="utf-8")
            missing = Path(d) / "gone.txt"
            out = planner._load_files([str(present), str(missing)], Path(d))
        self.assertIn(str(present), out)
        self.assertNotIn(str(missing), out)
        self.assertEqual(out[str(present)], "content")


if __name__ == "__main__":
    unittest.main()
