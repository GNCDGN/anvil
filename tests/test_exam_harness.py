"""Phase 2 Step 4 — exam_harness evolution sanity tests.

Six tests max per the brief, non-exhaustive — the harness is exam
infrastructure (decision #20). Tests cover the new functional surfaces
the Step 4 evolution added:

  1. coder_output dict renders compactly into the per-step table
  2. _bin_escalation categorises every known reason source correctly
  3. reply count is correctly proxied from run-log lines
  4. --self-check produces a non-empty report containing every expected
     section
  5. path reconciliations render into the dedicated section
  6. legacy string coder_output back-compat: renders as "(manual)"

Hermetic: tests use tmp paths; no real ANVIL state files touched.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

# Make sure the tools/ dir is on sys.path so we can import exam_harness.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "tools"))
import exam_harness as eh  # noqa: E402


_BASE_STATE = {
    "schema_version": 2,
    "brief_path": "/tmp/test-brief.md",
    "started_at": "2026-05-18T00:00:00",
    "status": "running",
    "current_step": 1,
    "coder_mode": "auto",
    "run_log": None,
    "steps": [],
}


def _step(n, **kwargs):
    base = {
        "n": n, "name": f"Step {n}", "status": "pending",
        "commit": None, "smoke": None, "smoke_output": None,
        "plan": None, "coder_output": None,
    }
    base.update(kwargs)
    return base


class ExamHarnessPhase2Tests(unittest.TestCase):

    # --- 1. Coder dict renders into the per-step table ---
    def test_coder_output_dict_renders_into_per_step_table(self):
        state = {
            **_BASE_STATE, "status": "done",
            "steps": [
                _step(
                    1, status="done", commit="abc123", smoke="pass",
                    coder_output={
                        "exit_code": 0, "stdout": "done", "stderr": "",
                        "files_touched": ["a.py", "b.py"],
                        "out_of_scope": [], "reconciliations": [],
                        "duration_s": 8.4,
                        "allowed_tools": ["Edit", "Read"],
                    },
                ),
            ],
        }
        cap = eh.Capture(Path("/tmp"))
        cap.poll(state)
        cap.snapshot_commits()
        report = eh.render(
            cap, [], [], Path("/dev/null"), Path("/dev/null"),
            None, "test",
        )
        # The Coder outputs section must exist
        self.assertIn("## Coder outputs", report)
        # The step's data must render — exit, file count, duration, tools
        self.assertIn("| 1 | 0 |", report)
        self.assertIn("a.py, b.py", report)
        self.assertIn("8.4", report)
        self.assertIn("Edit", report)

    # --- 2. Escalation bin categorisation across all known reasons ---
    def test_bin_escalation_categories(self):
        cases = [
            ("judgment-call", "planner-self"),
            ("missing-decision", "planner-self"),
            ("stage-a-missed-context", "planner-self"),
            ("planner-validation-failure", "framework"),
            ("smoke test failed", "framework"),
            ("coder-out-of-scope", "framework"),
            ("coder-path-reconciliation-failed", "framework"),
            ("coder-failed", "framework"),
            ("pause", "genco-initiated"),
            ("totally unknown thing", "other"),
            ("", "other"),
            (None, "other"),
        ]
        for reason, expected_bin in cases:
            with self.subTest(reason=reason):
                self.assertEqual(eh._bin_escalation(reason or ""), expected_bin)

    # --- 3. Reply count parsing from a synthetic run log ---
    def test_reply_count_from_run_log(self):
        # Build a minimal state with a runnable shape
        state = {**_BASE_STATE, "status": "done", "steps": [_step(1, status="done")]}
        cap = eh.Capture(Path("/tmp"))
        cap.poll(state)

        with tempfile.TemporaryDirectory() as td:
            rl = Path(td) / "run-log.md"
            rl.write_text(
                "- [12:00:01] **start** — 3 steps\n"
                "- [12:01:00] **coder(manual)** — reply=done\n"
                "- [12:02:00] **step-done** — step 1\n"
                "- [12:03:00] **coder(manual)** — reply=done\n"
                "- [12:04:00] **smoke** — step 2: FAIL\n"
                "- [12:05:00] **escalation** — smoke test failed\n"
                "- [12:06:00] **pause** — reply='fix and re-run'\n"
                "- [12:07:00] **step-done** — step 3\n",
                encoding="utf-8",
            )
            report = eh.render(
                cap, [], [], Path("/dev/null"), Path("/dev/null"),
                rl, "test",
            )
        # Expect 5 reply events counted:
        #   2 coder(manual) + 2 step-done + 1 pause = 5
        self.assertIn("replies counted from run log: 5", report)

    # --- 4. --self-check produces a non-empty report with all sections ---
    def test_self_check_passes(self):
        rc = eh._self_check()
        self.assertEqual(rc, 0, "self-check should exit 0")

    # --- 5. Path reconciliations render into their section ---
    def test_path_reconciliations_render(self):
        state = {
            **_BASE_STATE, "status": "done",
            "steps": [
                _step(
                    1, status="done", commit="abc123", smoke="pass",
                    coder_output={
                        "exit_code": 0, "stdout": "", "stderr": "",
                        "files_touched": ["chat_handler.py"],
                        "out_of_scope": [],
                        "reconciliations": [{
                            "original": "reporter/chat_handler.py",
                            "resolved": "chat_handler.py",
                            "status": "resolved",
                            "reason": "single match on basename",
                        }],
                        "duration_s": 4.0,
                        "allowed_tools": ["Edit"],
                    },
                ),
            ],
        }
        cap = eh.Capture(Path("/tmp"))
        cap.poll(state)
        report = eh.render(
            cap, [], [], Path("/dev/null"), Path("/dev/null"),
            None, "test",
        )
        self.assertIn("### Path reconciliations", report)
        self.assertIn("reporter/chat_handler.py", report)
        self.assertIn("single match on basename", report)
        self.assertIn("resolved", report)

    # --- 6. Legacy string coder_output back-compat ---
    def test_legacy_string_coder_output_renders_manual(self):
        state = {
            **_BASE_STATE, "status": "done",
            "steps": [
                _step(
                    1, status="done", commit="abc123", smoke="pass",
                    coder_output="done\nhash=abc123",  # Phase 1 manual shape
                ),
            ],
        }
        cap = eh.Capture(Path("/tmp"))
        cap.poll(state)
        report = eh.render(
            cap, [], [], Path("/dev/null"), Path("/dev/null"),
            None, "test",
        )
        self.assertIn("## Coder outputs", report)
        self.assertIn("(manual)", report)


if __name__ == "__main__":
    unittest.main()
