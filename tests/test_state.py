"""Step 4 tests — state round-trip, MD regeneration, atomic write, transition.

Hermetic: ANVIL_STATE_DIR points at a fresh tmp dir per test (set in setUp,
restored + removed in tearDown) so nothing touches ~/Downloads/anvil/state/.

Atomic-write note (load-bearing per orchestrator): true process death
mid-write is not mocked — that isn't cleanly reproducible in unittest.
Instead `test_atomic_write_reader_never_sees_partial` fails `os.rename` at
exactly the json-rename boundary (the crash window) and proves the property
the brief actually cares about: a reader mid-failed-write sees the previous
*valid* current-run.json, never a partial/corrupt one. Plus a clean write
leaves no `.tmp` behind. This is the honest substitute, surfaced not faked.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from anvil.brief import parse_brief
from anvil.state import (
    PendingAction,
    State,
    init_state,
    read_state,
    state_dir,
    transition,
    write_state,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TRIVIAL = FIXTURES / "trivial-test-brief.md"


class TestState(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_env = os.environ.get("ANVIL_STATE_DIR")
        self._dir = Path(tempfile.mkdtemp(prefix="anvil-test-state-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._dir)
        self.brief = parse_brief(TRIVIAL)

    def tearDown(self) -> None:
        if self._prev_env is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev_env
        shutil.rmtree(self._dir, ignore_errors=True)

    def _mk_state(self) -> State:
        return init_state(
            self.brief,
            started_at="2026-05-17T20:00:00+01:00",
            brief_path="inbox/trivial-test-brief.md",
            coder_mode="manual",
        )

    def test_state_dir_is_tmp(self) -> None:
        self.assertEqual(state_dir(), self._dir.resolve())

    def test_read_none_when_no_state(self) -> None:
        self.assertIsNone(read_state())

    def test_round_trip(self) -> None:
        s = self._mk_state()
        write_state(s)
        back = read_state()
        self.assertIsNotNone(back)
        self.assertEqual(back.model_dump(), s.model_dump())
        self.assertEqual([st.n for st in back.steps], [1, 2, 3])
        self.assertEqual(back.steps[0].name, "Create a file")
        self.assertEqual(back.coder_mode, "manual")
        self.assertEqual(back.status, "running")

    def test_md_regenerated(self) -> None:
        s = self._mk_state()
        write_state(s)
        md = (self._dir / "current-run.md").read_text()
        self.assertIn("# ANVIL — current run", md)
        self.assertIn("**Status:** running", md)
        self.assertIn("**Step:** 1 of 3", md)
        self.assertIn("trivial-test-brief.md", md)
        self.assertIn("| 1 | Create a file | pending |", md)
        self.assertIn("(none — running)", md)

    def test_no_tmp_after_clean_write(self) -> None:
        write_state(self._mk_state())
        tmps = list(self._dir.glob("*.tmp"))
        self.assertEqual(tmps, [], f"leftover tmp files: {tmps}")
        # canonical json is valid and reloadable
        loaded = json.loads((self._dir / "current-run.json").read_text())
        self.assertEqual(loaded["status"], "running")
        self.assertIsNotNone(read_state())

    def test_atomic_write_reader_never_sees_partial(self) -> None:
        # 1. Establish a known-good canonical state A.
        a = self._mk_state()
        write_state(a)
        a_json = (self._dir / "current-run.json").read_text()

        # 2. Attempt to overwrite with B, but crash at the json-rename
        #    boundary (the exact window a real process death would hit).
        b_state = a.model_copy(update={"status": "done", "current_step": 3})

        real_rename = os.rename

        def boom(src, dst, *args, **kw):
            if str(dst).endswith("current-run.json"):
                raise RuntimeError("simulated crash mid-write (json rename)")
            return real_rename(src, dst, *args, **kw)

        with patch("anvil.state.os.rename", side_effect=boom):
            with self.assertRaises(RuntimeError):
                write_state(b_state)

        # 3. Reader-perspective atomicity: canonical json is byte-identical
        #    to A (never half-written), and read_state() returns valid A.
        self.assertEqual(
            (self._dir / "current-run.json").read_text(), a_json,
            "canonical current-run.json was modified by a failed write",
        )
        recovered = read_state()
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.model_dump(), a.model_dump())
        self.assertEqual(recovered.status, "running")  # A, not B's "done"
        # A leftover current-run.json.tmp may exist — harmless, it is not the
        # canonical file and state/ is gitignored. The invariant that matters
        # (reader never sees a partial canonical file) holds.

    # ---- Phase 1 Step 7: plan / coder_output / schema_version=2 ----

    def test_stepstate_new_fields_defaults(self) -> None:
        s = self._mk_state()
        for st in s.steps:
            self.assertIsNone(st.plan)
            self.assertIsNone(st.coder_output)

    def test_schema_version_defaults_to_2(self) -> None:
        s = self._mk_state()
        self.assertEqual(s.schema_version, 2)
        write_state(s)
        self.assertEqual(read_state().schema_version, 2)

    def test_v1_state_file_loads_legacy(self) -> None:
        s = self._mk_state()
        raw = s.model_dump()
        raw["schema_version"] = 1
        for st in raw["steps"]:
            st.pop("plan", None)
            st.pop("coder_output", None)
        (self._dir / "current-run.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
        back = read_state()
        self.assertIsNotNone(back)
        self.assertEqual(back.schema_version, 1)  # legacy preserved
        self.assertTrue(all(st.plan is None for st in back.steps))
        self.assertTrue(all(st.coder_output is None for st in back.steps))

    def test_round_trip_preserves_plan(self) -> None:
        s = self._mk_state()
        s.steps[0].plan = {"step_number": 1, "approach": "x"}
        write_state(s)
        self.assertEqual(
            read_state().steps[0].plan, {"step_number": 1, "approach": "x"}
        )

    def test_md_plan_stored(self) -> None:
        s = self._mk_state()
        s.steps[0].plan = {"step_number": 1, "approach": "x"}
        write_state(s)
        md = (self._dir / "current-run.md").read_text()
        self.assertIn("Plan: stored", md)
        self.assertNotIn("Plan: (escalated)", md)

    def test_md_plan_escalated(self) -> None:
        s = self._mk_state()
        s.steps[0].plan = {"escalate": True, "reason": "missing-decision"}
        write_state(s)
        md = (self._dir / "current-run.md").read_text()
        self.assertIn("Plan: (escalated)", md)
        self.assertNotIn("Plan: stored", md)

    def test_md_plan_none_no_line(self) -> None:
        s = self._mk_state()  # no plan set
        write_state(s)
        md = (self._dir / "current-run.md").read_text()
        self.assertNotIn("Plan: stored", md)
        self.assertNotIn("Plan: (escalated)", md)

    def test_transition_persists(self) -> None:
        s = self._mk_state()
        write_state(s)
        pa = PendingAction(
            type="step_confirmation",
            telegram_message_id=123,
            sent_at="2026-05-17T20:01:00+01:00",
            expected_reply="go",
        )
        new = transition(s, "waiting", pending_action=pa, current_step=1)
        self.assertEqual(new.status, "waiting")
        self.assertIsNotNone(new.pending_action)
        # persisted
        back = read_state()
        self.assertEqual(back.status, "waiting")
        self.assertEqual(back.pending_action.expected_reply, "go")
        md = (self._dir / "current-run.md").read_text()
        self.assertIn("**Waiting for:** step_confirmation", md)


if __name__ == "__main__":
    unittest.main()
