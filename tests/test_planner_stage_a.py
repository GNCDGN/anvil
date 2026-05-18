"""Tests for Phase 1 Stage A (planner.py) — Step 3.

Index tests (a-c) build temp directory trees and assert on
_build_vault_index output directly. Parser tests (e-h) use the committed
tests/fixtures/planner/*.txt fixtures plus a hand-built vault_index whose
keys are byte-identical to the in-index fixture lines (literal hardcoded
alignment, not tmp-generated). No Anthropic call anywhere — Step 5
introduces _call_anthropic.
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

_IN_INDEX = [
    "01-Projects/code-workspace/anvil/design.md",
    "01-Projects/code-workspace/anvil/brief.md",
    "01-Projects/code-workspace/anvil/decisions.md",
    "01-Projects/code-workspace/anvil/setup-log.md",
]


class BuildVaultIndexTests(unittest.TestCase):
    def test_build_vault_index_parses_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.md").write_text(
                "---\nproject: anvil\nstatus: active\n---\nbody\n",
                encoding="utf-8",
            )
            (root / "b.md").write_text(
                "---\ntags:\n  - x\n  - y\n---\nbody\n", encoding="utf-8"
            )
            index = planner._build_vault_index([str(root)], Path(d))
        self.assertEqual(
            index[str(root / "a.md")], {"project": "anvil", "status": "active"}
        )
        self.assertEqual(index[str(root / "b.md")], {"tags": ["x", "y"]})

    def test_folder_recursion_respects_depth_2(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sub" / "subsub").mkdir(parents=True)
            (root / "f1.md").write_text("---\nk: 1\n---\n", encoding="utf-8")
            (root / "sub" / "f2.md").write_text(
                "---\nk: 2\n---\n", encoding="utf-8"
            )
            (root / "sub" / "subsub" / "f3.md").write_text(
                "---\nk: 3\n---\n", encoding="utf-8"
            )
            index = planner._build_vault_index([str(root)], Path(d))
        self.assertIn(str(root / "f1.md"), index)
        self.assertIn(str(root / "sub" / "f2.md"), index)
        self.assertNotIn(str(root / "sub" / "subsub" / "f3.md"), index)

    def test_files_without_frontmatter_appear_as_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "plain.md").write_text("no frontmatter here\n", encoding="utf-8")
            (root / "broken.md").write_text(
                "---\n: : bad yaml :\n---\n", encoding="utf-8"
            )
            index = planner._build_vault_index([str(root)], Path(d))
        self.assertEqual(index[str(root / "plain.md")], {})
        self.assertIn(str(root / "broken.md"), index)
        self.assertEqual(index[str(root / "broken.md")], {})


class AssembleStageAPromptTests(unittest.TestCase):
    def test_assemble_stage_a_prompt_substitutes_and_orders(self):
        """Asserts each substituted placeholder is absent BY NAME after
        substitution. It deliberately does not assert blanket "{" absence:
        the YAML vault-index output and the embedded JSON state contain
        literal { } braces that are correct content, not unsubstituted
        tokens. Do not tighten this to a blanket brace check.
        """
        brief = parse_brief(_TRIVIAL_BRIEF)
        state = init_state(
            brief, "2026-05-18T00:00:00", brief_path=str(_TRIVIAL_BRIEF)
        )
        result = planner._assemble_stage_a_prompt(brief, state, 0, {})

        for token in (
            "{BRIEF_MARKDOWN}",
            "{STATE_JSON}",
            "{STEP_NUMBER}",
            "{STEP_NAME}",
            "{STEP_SCOPE_FILES}",
            "{STEP_SCOPE_OPERATIONS}",
            "{STEP_NOTES}",
            "{CONTEXT_PATHS}",
            "{VAULT_INDEX_YAML}",
        ):
            self.assertNotIn(token, result)

        i_brief = result.index("## Build brief")
        i_index = result.index("## Vault index")
        i_instr = result.index("## Instruction")
        self.assertLess(i_brief, i_index)
        self.assertLess(i_index, i_instr)


class ParseStageAResponseTests(unittest.TestCase):
    def test_parse_valid_fixture(self):
        text = (_FIX / "stage_a_valid.txt").read_text(encoding="utf-8")
        index = {p: {} for p in _IN_INDEX}
        self.assertEqual(
            planner._parse_stage_a_response(text, index), _IN_INDEX
        )

    def test_hallucination_guard_drops_unknown_paths(self):
        text = (_FIX / "stage_a_with_hallucinations.txt").read_text(
            encoding="utf-8"
        )
        index = {
            "01-Projects/code-workspace/anvil/design.md": {},
            "01-Projects/code-workspace/anvil/brief.md": {},
            "01-Projects/code-workspace/anvil/decisions.md": {},
        }
        self.assertEqual(
            planner._parse_stage_a_response(text, index),
            [
                "01-Projects/code-workspace/anvil/design.md",
                "01-Projects/code-workspace/anvil/brief.md",
                "01-Projects/code-workspace/anvil/decisions.md",
            ],
        )

    def test_empty_response_returns_empty_list(self):
        text = (_FIX / "stage_a_empty.txt").read_text(encoding="utf-8")
        self.assertEqual(planner._parse_stage_a_response(text, {"x": {}}), [])
        self.assertEqual(planner._parse_stage_a_response("", {"x": {}}), [])

    def test_duplicates_collapse_to_first_occurrence(self):
        index = {"a": {}, "b": {}, "c": {}}
        self.assertEqual(
            planner._parse_stage_a_response("a\nb\na\nc\n", index),
            ["a", "b", "c"],
        )


if __name__ == "__main__":
    unittest.main()
