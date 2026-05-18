"""Tests for anvil/prompts/planner-system.md (Phase 1 Step 2).

Asserts the prompt file is present and structurally intact: the
{VOICE_SPEC} placeholder, the four discipline-rule headings, the output
discipline section, and the escalation JSON schema. The substitution test
mocks load_voice_spec so it is vault- and snapshot-independent and fast;
Step 2 builds no production substitution code (that is Step 6's
Planner.__init__), so the test performs the .replace itself to prove the
placeholder is exactly substitutable.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import anvil

_PROMPT = Path(anvil.__file__).resolve().parent / "prompts" / "planner-system.md"


class PlannerSystemPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _PROMPT.read_text(encoding="utf-8")

    def test_file_exists_and_non_empty(self):
        self.assertTrue(_PROMPT.is_file())
        self.assertTrue(self.text.strip())

    def test_contains_voice_spec_placeholder(self):
        self.assertIn("{VOICE_SPEC}", self.text)

    def test_contains_four_rule_headings(self):
        for heading in (
            "The escalation rule.",
            "The anti-confabulation rule.",
            "The scope rule.",
            "The context rule.",
        ):
            self.assertIn(heading, self.text)

    def test_contains_output_discipline_section(self):
        self.assertIn("Output discipline.", self.text)

    def test_contains_escalation_json_schema(self):
        self.assertIn('"escalate": true,', self.text)
        self.assertIn('"step_number":', self.text)

    def test_voice_substitution_replaces_placeholder(self):
        sentinel = "VOICE-SPEC-SENTINEL-no-braces-here"
        with mock.patch(
            "anvil.voice.load_voice_spec", return_value=sentinel
        ) as m:
            import anvil.voice

            spec = anvil.voice.load_voice_spec()
        m.assert_called_once()
        result = self.text.replace("{VOICE_SPEC}", spec)
        self.assertIn(sentinel, result)
        self.assertNotIn("{VOICE_SPEC}", result)


if __name__ == "__main__":
    unittest.main()
