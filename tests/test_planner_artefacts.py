"""Phase 4 Step 4 tests — Planner.draft_completion_artefacts.

Hermetic: mocks _call_anthropic at the method level (same pattern as
test_planner_retry.py). No real API calls.

Covers:
  - happy path (valid JSON, validation passes)
  - retry-once-with-error path (first response bad JSON, second valid)
  - both-attempts-fail → escalation block returned
  - empty first response → escalation
  - missing key validation → triggers retry
  - escalation passthrough (model self-emits escalate block)
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from anvil.planner import Planner


def _fake_state():
    """Minimal state-like object for prompt assembly."""
    return SimpleNamespace(
        brief_path="/tmp/does-not-exist-brief.md",  # OSError → brief_md=""
        deploy=None,
        model_dump_json=lambda indent=2: '{"status": "done"}',
    )


def _fake_brief():
    return SimpleNamespace(
        project="anvil",
        build_name="Phase 4 — vault writes",
    )


class TestDraftCompletionArtefacts(unittest.TestCase):

    def _planner(self) -> Planner:
        """Construct a Planner with no API key — _client=None, _system_prompt
        and _artefacts_prompt loaded from disk."""
        return Planner()

    def test_happy_path_returns_draft(self) -> None:
        valid = json.dumps({
            "setup_log_entry": "## 2026-05-19 — anvil Phase 4 shipped\n\nfoo",
            "checkpoint": "# anvil Phase 4 shipped\n\n## What changed\n\nbar",
        })
        with patch.object(Planner, "_call_anthropic", return_value=valid):
            result = self._planner().draft_completion_artefacts(
                _fake_brief(), _fake_state()
            )
        self.assertNotIn("escalate", result)
        self.assertIn("setup_log_entry", result)
        self.assertIn("checkpoint", result)
        self.assertTrue(result["setup_log_entry"].startswith("## "))

    def test_retry_once_with_error_succeeds(self) -> None:
        """First response bad JSON → retry → second response good."""
        valid = json.dumps({
            "setup_log_entry": "## 2026-05-19 — heading\n\nbody",
            "checkpoint": "# Title\n\n## What changed\n\nx",
        })
        responses = iter(["not json at all", valid])
        with patch.object(Planner, "_call_anthropic",
                          side_effect=lambda **kw: next(responses)):
            result = self._planner().draft_completion_artefacts(
                _fake_brief(), _fake_state()
            )
        self.assertNotIn("escalate", result)
        self.assertIn("setup_log_entry", result)

    def test_both_attempts_fail_escalates(self) -> None:
        """Both calls return bad JSON → escalation block."""
        responses = iter(["garbage 1", "garbage 2"])
        with patch.object(Planner, "_call_anthropic",
                          side_effect=lambda **kw: next(responses)):
            result = self._planner().draft_completion_artefacts(
                _fake_brief(), _fake_state()
            )
        self.assertTrue(result.get("escalate"))
        self.assertEqual(result["reason"], "completion-artefacts-draft-failed")
        self.assertEqual(result["step_number"], 0)
        self.assertIn("twice", result["detail"].lower())

    def test_empty_first_response_escalates(self) -> None:
        """Empty response (rate-limit, timeout) → immediate escalation, no retry."""
        with patch.object(Planner, "_call_anthropic", return_value=""):
            result = self._planner().draft_completion_artefacts(
                _fake_brief(), _fake_state()
            )
        self.assertTrue(result.get("escalate"))
        self.assertEqual(result["reason"], "completion-artefacts-draft-failed")
        self.assertIn("empty", result["detail"].lower())

    def test_missing_key_triggers_retry(self) -> None:
        """First response missing 'checkpoint' key → retry → second good."""
        missing_key = json.dumps({
            "setup_log_entry": "## heading\n\nbody",
            # 'checkpoint' missing
        })
        valid = json.dumps({
            "setup_log_entry": "## heading\n\nbody",
            "checkpoint": "# Title\n\n## What changed\n\nx",
        })
        responses = iter([missing_key, valid])
        with patch.object(Planner, "_call_anthropic",
                          side_effect=lambda **kw: next(responses)):
            result = self._planner().draft_completion_artefacts(
                _fake_brief(), _fake_state()
            )
        self.assertNotIn("escalate", result)
        self.assertIn("setup_log_entry", result)

    def test_model_self_emits_escalation(self) -> None:
        """If the model returns its own escalate block, validation accepts
        and the caller routes it."""
        escalation = json.dumps({
            "escalate": True,
            "reason": "missing-context",
            "detail": "Cannot draft without brief.build_name",
            "step_number": 0,
        })
        with patch.object(Planner, "_call_anthropic", return_value=escalation):
            result = self._planner().draft_completion_artefacts(
                _fake_brief(), _fake_state()
            )
        self.assertTrue(result.get("escalate"))
        self.assertEqual(result["reason"], "missing-context")


if __name__ == "__main__":
    unittest.main()
