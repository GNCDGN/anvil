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
_STAGE_B_PROMPT = (
    Path(anvil.__file__).resolve().parent / "prompts" / "planner-stage-b.md"
)

# v2 Phase 2 Step 3: the eleven _REQUIRED_PLAN_FIELDS that
# `_validate_plan_structure` enforces at runtime. The hardened prompts
# must enumerate every one — that is the load-bearing change this step
# makes.
_REQUIRED_PLAN_FIELDS = (
    "step_number",
    "step_name",
    "files_to_touch",
    "operations",
    "approach",
    "smoke_test",
    "expected_outcome",
    "commit_message",
    "scope_boundaries",
    "confidence",
    "escalation_triggers",
)


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


class PlannerPromptHardeningTests(unittest.TestCase):
    """v2 Phase 2 Step 3: the hard-schema block, the eleven-field
    enumeration, the few-shot worked example, and the closing reminder
    in planner-system.md, plus the matching eleven-field checklist in
    planner-stage-b.md. These are the load-bearing prompt changes
    targeted at the validation-failure rate measured in v2 Phase 1."""

    def setUp(self) -> None:
        self.system_text = _PROMPT.read_text(encoding="utf-8")
        self.stage_b_text = _STAGE_B_PROMPT.read_text(encoding="utf-8")

    def test_system_prompt_lists_every_required_field(self):
        """All eleven _REQUIRED_PLAN_FIELDS names appear in the system
        prompt. Drives Step 3's primary claim: the prompt now names
        every field the validator checks."""
        for field in _REQUIRED_PLAN_FIELDS:
            self.assertIn(
                field, self.system_text,
                f"required field {field!r} not in planner-system.md",
            )

    def test_system_prompt_opens_hard_schema_block(self):
        """The hard-schema opening phrase locks the block's framing —
        not a suggestion, a non-negotiable requirement."""
        self.assertIn(
            "Your plan JSON MUST contain all of the following fields",
            self.system_text,
        )

    def test_system_prompt_states_structural_constraints(self):
        """The six structural constraints beyond field-presence are
        present verbatim enough for the model to reproduce them."""
        for fragment in (
            'confidence must be exactly one of: "high", "medium", "low"',
            'scope_boundaries must be a JSON object',
            'escalation_triggers must be a JSON array of strings',
            'files_to_touch entries must be within the step\'s declared scope_files',
            'operations entries must be within the step\'s declared scope_operations',
            'step_number must equal the brief\'s step number exactly',
        ):
            self.assertIn(fragment, self.system_text,
                          f"constraint fragment {fragment!r} missing")

    def test_system_prompt_carries_few_shot_example(self):
        """The few-shot worked example is present. Anchor on the
        synthetic step_name + the JSON fence opener so the test
        survives whitespace edits but breaks if the example is removed
        or substantively replaced."""
        self.assertIn("```json", self.system_text,
                      "no fenced JSON block in planner-system.md")
        self.assertIn('"step_number": 1', self.system_text)
        self.assertIn('"step_name": "Add logging config"', self.system_text)
        self.assertIn('"files_to_touch": ["config/logging.py"]',
                      self.system_text)

    def test_system_prompt_closes_with_verification_reminder(self):
        """The closing one-line reminder is the LAST instruction the
        model sees before generating. Asserting it lives near the end
        of the file (within the final 300 chars) protects against
        future edits that bury it mid-document."""
        reminder = (
            "Before outputting: verify your response contains all "
            "eleven plan fields and satisfies all six structural "
            "constraints."
        )
        self.assertIn(reminder, self.system_text)
        self.assertIn(reminder, self.system_text[-400:],
                      "reminder is not near the end of the prompt")

    def test_stage_b_prompt_lists_every_required_field(self):
        """The user-prompt instruction block carries the eleven-field
        checklist too — defence in depth, since the user prompt is the
        last thing the model reads before responding."""
        for field in _REQUIRED_PLAN_FIELDS:
            self.assertIn(
                field, self.stage_b_text,
                f"required field {field!r} not in planner-stage-b.md",
            )

    def test_stage_b_prompt_carries_pre_output_checklist_phrase(self):
        """The Stage B user prompt frames the eleven-field check as a
        pre-output verification step, mirroring the system-prompt
        closing reminder."""
        self.assertIn(
            "Check that your output contains all eleven required fields",
            self.stage_b_text,
        )


if __name__ == "__main__":
    unittest.main()
