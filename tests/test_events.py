"""Tests for anvil.events (v2 Phase 1 Step 1).

Covers: schema validation, emit happy/failure paths, run lifecycle,
sentinel run_id, idempotency, ANVIL_ROOT redirect, drop_count semantics,
multi-emit JSONL integrity, unknown-kind handling.

Each test clears the module-global state in setUp so tests do not bleed
into each other. ANVIL_ROOT is redirected to a tmp_path for every test
so events.jsonl writes never escape the test scope.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from anvil import events


class _EventsTestBase(unittest.TestCase):
    """Shared setUp/tearDown for module-global state and ANVIL_ROOT redirect."""

    def setUp(self) -> None:
        # Reset module state so a prior test's begin_run/emit can't leak.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        # Redirect every write under a per-test temp dir.
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(
            os.environ, {"ANVIL_ROOT": str(self.tmp_path)}
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()
        # Belt-and-braces: clear state again so a test that fails mid-flight
        # cannot break the next test in sequence.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

    def _read_events(self, run_id: str) -> list[dict]:
        """Return the events.jsonl rows as parsed dicts, in order."""
        path = self.tmp_path / "state" / "runs" / run_id / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class TestEventSchema(_EventsTestBase):
    """Pydantic schema rules — kind catalogue, required fields, types."""

    def test_happy_path_constructs(self) -> None:
        e = events.Event(
            ts="2026-05-20T10:15:42.000+00:00",
            run_id="r1",
            kind="run.start",
            data={},
        )
        self.assertEqual(e.kind, "run.start")
        self.assertEqual(e.step_idx, None)
        self.assertEqual(e.elapsed_ms, 0)

    def test_missing_required_field_raises(self) -> None:
        # `ts` is required and has no default.
        with self.assertRaises(ValidationError):
            events.Event(run_id="r1", kind="run.start")  # type: ignore[call-arg]

    def test_invalid_kind_raises_at_construction(self) -> None:
        with self.assertRaises(ValidationError):
            events.Event(
                ts="2026-05-20T10:15:42.000+00:00",
                run_id="r1",
                kind="not.a.real.kind",
            )

    def test_valid_kinds_catalogue_size(self) -> None:
        # Pinned at 45 by the module's assert; this test surfaces drift
        # at the test-suite level too.
        self.assertEqual(len(events.VALID_KINDS), 45)
        # A few canonical kinds present:
        for k in ("run.start", "planner.stage_b.api_end",
                  "ssh.stage.end", "telegram.poll.reply"):
            self.assertIn(k, events.VALID_KINDS)


class TestEmitHappyPath(_EventsTestBase):
    """Emit writes a parseable JSONL row; fields populated correctly."""

    def test_emit_lands_in_jsonl(self) -> None:
        events.begin_run("r1")
        ok = events.emit("step.start", {"step_number": 1}, step_idx=0)
        events.end_run()

        self.assertTrue(ok)
        rows = self._read_events("r1")
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds, ["run.start", "step.start", "run.end"])
        step = rows[1]
        self.assertEqual(step["run_id"], "r1")
        self.assertEqual(step["step_idx"], 0)
        self.assertEqual(step["data"], {"step_number": 1})
        self.assertTrue(step["ts"].endswith("+00:00"))

    def test_emit_with_step_idx_populated(self) -> None:
        events.begin_run("r1")
        events.emit("coder.subprocess.start", {}, step_idx=2)
        events.end_run()
        rows = self._read_events("r1")
        coder = [r for r in rows if r["kind"] == "coder.subprocess.start"][0]
        self.assertEqual(coder["step_idx"], 2)

    def test_emit_with_sentinel_run_id(self) -> None:
        # No begin_run — emit lands under "unknown-run".
        ok = events.emit("step.start", {"step_number": 1})
        self.assertTrue(ok)
        rows = self._read_events("unknown-run")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "unknown-run")
        self.assertEqual(events.current_run_id(), "unknown-run")


class TestEmitFailurePaths(_EventsTestBase):
    """Unknown kinds, write errors, drop_count, never-raise contract."""

    def test_unknown_kind_drops_returns_false(self) -> None:
        events.begin_run("r1")
        start_drops = events.drop_count()
        ok = events.emit("bogus.kind", {})
        self.assertFalse(ok)
        self.assertEqual(events.drop_count(), start_drops + 1)
        # File contains run.start but NOT the bogus emit.
        rows = self._read_events("r1")
        self.assertNotIn("bogus.kind", [r["kind"] for r in rows])

    def test_unknown_kind_logged_once_only(self) -> None:
        events.begin_run("r1")
        with self.assertLogs("anvil.events", level="WARNING") as captured:
            events.emit("bogus.kind", {})
            events.emit("bogus.kind", {})
            events.emit("bogus.kind", {})
        # Three drops but only one log line per unique unknown kind.
        bogus_logs = [m for m in captured.output if "bogus.kind" in m]
        self.assertEqual(len(bogus_logs), 1)
        self.assertEqual(events.drop_count(), 3)

    def test_write_failure_increments_drop_count_no_raise(self) -> None:
        events.begin_run("r1")
        start_drops = events.drop_count()
        # Patch _real_append (the hot path) to raise OSError;
        # emit must catch and drop.
        with mock.patch.object(
            events, "_real_append",
            side_effect=OSError("disk full (simulated)"),
        ):
            ok = events.emit("step.start", {"step_number": 1})
        self.assertFalse(ok)
        self.assertEqual(events.drop_count(), start_drops + 1)

    def test_emit_never_raises_on_unexpected(self) -> None:
        events.begin_run("r1")
        with mock.patch.object(
            events, "_real_append",
            side_effect=RuntimeError("totally unexpected"),
        ):
            ok = events.emit("step.start", {})
        # Did not raise, returned False, drop_count incremented.
        self.assertFalse(ok)
        self.assertGreater(events.drop_count(), 0)


class TestRunLifecycle(_EventsTestBase):
    """begin_run/end_run, anchor semantics, idempotency, no-op end_run."""

    def test_begin_run_sets_state_emits_run_start(self) -> None:
        self.assertIsNone(events._run_id)
        events.begin_run("r1")
        self.assertEqual(events._run_id, "r1")
        self.assertIsNotNone(events._anchor_monotonic)
        rows = self._read_events("r1")
        self.assertEqual(rows[0]["kind"], "run.start")
        self.assertEqual(rows[0]["elapsed_ms"], 0)

    def test_run_start_elapsed_ms_is_zero_subsequent_grows(self) -> None:
        events.begin_run("r1")
        # Let the monotonic clock advance.
        time.sleep(0.02)
        events.emit("step.start", {})
        events.end_run()
        rows = self._read_events("r1")
        run_start = next(r for r in rows if r["kind"] == "run.start")
        step_start = next(r for r in rows if r["kind"] == "step.start")
        self.assertEqual(run_start["elapsed_ms"], 0)
        self.assertGreaterEqual(step_start["elapsed_ms"], 15)

    def test_end_run_emits_run_end_with_drops_and_resets(self) -> None:
        events.begin_run("r1")
        events.emit("bogus.kind", {})  # one drop
        events.end_run()
        rows = self._read_events("r1")
        run_end = next(r for r in rows if r["kind"] == "run.end")
        self.assertEqual(run_end["data"], {"drops": 1})
        # State reset.
        self.assertIsNone(events._run_id)
        self.assertIsNone(events._anchor_monotonic)
        self.assertEqual(events.drop_count(), 0)

    def test_begin_run_idempotent_resets_anchor(self) -> None:
        events.begin_run("r1")
        first_anchor = events._anchor_monotonic
        time.sleep(0.02)
        events.begin_run("r2")
        second_anchor = events._anchor_monotonic
        self.assertNotEqual(first_anchor, second_anchor)
        self.assertEqual(events._run_id, "r2")
        # Both runs produced their own run.start events under distinct
        # run_id dirs.
        self.assertTrue(self._read_events("r1"))
        self.assertEqual(self._read_events("r2")[0]["kind"], "run.start")

    def test_end_run_without_begin_run_is_noop(self) -> None:
        # No state to flush — must not crash, must not emit anything.
        events.end_run()  # no exception
        # No events.jsonl written anywhere under tmp.
        runs_dir = self.tmp_path / "state" / "runs"
        if runs_dir.exists():
            self.assertEqual(list(runs_dir.iterdir()), [])


class TestMultiEmitAndRedirect(_EventsTestBase):
    """JSONL integrity under multiple emits + ANVIL_ROOT redirect honoured."""

    def test_ten_emits_produce_valid_jsonl(self) -> None:
        events.begin_run("r1")
        for i in range(10):
            events.emit("step.start", {"i": i}, step_idx=i)
        events.end_run()
        rows = self._read_events("r1")
        # 1 (run.start) + 10 (step.start) + 1 (run.end) = 12
        self.assertEqual(len(rows), 12)
        step_rows = [r for r in rows if r["kind"] == "step.start"]
        self.assertEqual([r["data"]["i"] for r in step_rows], list(range(10)))

    def test_anvil_root_env_redirect_honoured(self) -> None:
        # tmp_path was already injected via setUp; verify the file lands
        # under that root, not under the live repo's state/runs/.
        events.begin_run("r1")
        events.end_run()
        expected = self.tmp_path / "state" / "runs" / "r1" / "events.jsonl"
        self.assertTrue(expected.is_file(),
                        f"events.jsonl should land at {expected}")


class TestAppendModeHotPath(_EventsTestBase):
    """Step 2 prep: _real_append is the O(1) hot path, replacing the
    Step 1 read-modify-write block. Verify the new write semantics."""

    def test_real_append_used_for_each_emit(self) -> None:
        """Patching _real_append (not _real_write) catches every emit."""
        captured: list[tuple[Path, str]] = []

        def _spy(path: Path, text: str) -> None:
            captured.append((path, text))
            # Still write so the file exists for subsequent assertions.
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)

        events.begin_run("r1")
        with mock.patch.object(events, "_real_append", side_effect=_spy):
            events.emit("step.start", {"i": 0})
            events.emit("step.start", {"i": 1})
            events.emit("step.start", {"i": 2})
        events.end_run()

        # begin_run.run_start + 3 emits + end_run.run_end = 5 captured
        # writes via _real_append (the run.start/end go through emit too).
        # The patched scope above only covers the three middle emits;
        # the run.start fires before mock.patch.object entered, and the
        # run.end fires after it exited. So we count exactly 3.
        self.assertEqual(len(captured), 3)

    def test_append_path_produces_valid_jsonl_for_long_sequence(self) -> None:
        """Appendmode integrity: 30 emits in tight succession still produce
        a JSONL where every line parses independently."""
        events.begin_run("r1")
        for i in range(30):
            events.emit("step.start", {"i": i}, step_idx=i)
        events.end_run()
        rows = self._read_events("r1")
        # 1 run.start + 30 step.start + 1 run.end = 32
        self.assertEqual(len(rows), 32)
        # Every step row carries its i intact, in order.
        step_rows = [r for r in rows if r["kind"] == "step.start"]
        self.assertEqual([r["data"]["i"] for r in step_rows], list(range(30)))


if __name__ == "__main__":
    unittest.main()
