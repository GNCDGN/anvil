"""Tests for anvil.integrations.visibility_session — v4 Phase 2a Step 2.

All tests use a tmp base_path (no live state/visibility-sessions/ writes), the
deploy_history.py test pattern. Covers write_session (all/partial observations,
digest handling), read_session (happy / missing / malformed), list_sessions
(empty / multiple / skip-malformed), the Q-D6 blobs-first-record-last atomic
ordering, never-raises across write/read failures, and the AC4 round-trip with a
browser.py-shaped observation fixture (no code coupling — browser.py does not
import visibility_session.py).
"""
from __future__ import annotations

import inspect
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from anvil.integrations import visibility_session as vs


# A fixture matching exactly what browser.py's methods return as ["result"]:
#   snapshot_dom()    -> {"html": str}
#   capture_console() -> {"entries": [{"type": str, "text": str}, ...]}
#   capture_network() -> {"entries": [{"url": str, "status": int}, ...]}
_BROWSER_SHAPED_OBS = {
    "dom": {"html": "<html><body><p>hi</p></body></html>"},
    "console": {"entries": [{"type": "log", "text": "hello"},
                            {"type": "error", "text": "boom"}]},
    "network": {"entries": [{"url": "https://x.test/api", "status": 200},
                            {"url": "https://x.test/missing", "status": 404}]},
}


class _TmpBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-vs-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestWriteSessionHappyPath(_TmpBase):
    def test_write_all_three_observations(self) -> None:
        r = vs.write_session("run-1", 0, "http://localhost:3000",
                             _BROWSER_SHAPED_OBS, base_path=self._tmp)
        self.assertTrue(r["ok"], r)
        d = self._tmp / "run-1" / "0"
        self.assertTrue((d / "record.json").exists())
        self.assertTrue((d / "dom.html").exists())
        self.assertTrue((d / "console.json").exists())
        self.assertTrue((d / "network.json").exists())
        rec = json.loads((d / "record.json").read_text())
        # record references blobs by RELATIVE filename
        self.assertEqual(rec["blobs"],
                         {"dom": "dom.html", "console": "console.json", "network": "network.json"})
        self.assertEqual(rec["run_id"], "run-1")
        self.assertEqual(rec["step_idx"], 0)
        self.assertEqual(rec["target"], "http://localhost:3000")
        datetime.fromisoformat(rec["timestamp"])  # ISO-8601 parseable
        self.assertIsNone(rec["digest"])  # null in Phase 2a
        # the result echoes the record path + blobs
        self.assertEqual(r["result"]["path"], str(d / "record.json"))

    def test_write_dom_only(self) -> None:
        r = vs.write_session("run-2", 1, "http://localhost", {"dom": {"html": "<p>x</p>"}},
                             base_path=self._tmp)
        self.assertTrue(r["ok"], r)
        d = self._tmp / "run-2" / "1"
        self.assertTrue((d / "dom.html").exists())
        self.assertFalse((d / "console.json").exists())
        self.assertFalse((d / "network.json").exists())
        rec = json.loads((d / "record.json").read_text())
        self.assertEqual(rec["blobs"], {"dom": "dom.html", "console": None, "network": None})

    def test_write_with_digest(self) -> None:
        r = vs.write_session("run-3", 0, "t", {"dom": {"html": "<p/>"}},
                             digest="janky transition; layout thrash", base_path=self._tmp)
        self.assertTrue(r["ok"], r)
        rec = json.loads((self._tmp / "run-3" / "0" / "record.json").read_text())
        self.assertEqual(rec["digest"], "janky transition; layout thrash")

    def test_write_digest_none_is_json_null(self) -> None:
        vs.write_session("run-4", 0, "t", {"dom": {"html": "<p/>"}}, digest=None,
                         base_path=self._tmp)
        raw = (self._tmp / "run-4" / "0" / "record.json").read_text()
        self.assertIn('"digest": null', raw)  # explicit null, not absent

    def test_write_empty_observations(self) -> None:
        # No observations at all → record with all-None blobs, no blob files.
        r = vs.write_session("run-5", 0, "t", {}, base_path=self._tmp)
        self.assertTrue(r["ok"], r)
        rec = json.loads((self._tmp / "run-5" / "0" / "record.json").read_text())
        self.assertEqual(rec["blobs"], {"dom": None, "console": None, "network": None})


class TestReadSession(_TmpBase):
    def test_read_existing_round_trips_record(self) -> None:
        vs.write_session("run-1", 2, "tgt", _BROWSER_SHAPED_OBS, base_path=self._tmp)
        r = vs.read_session("run-1", 2, base_path=self._tmp)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["run_id"], "run-1")
        self.assertEqual(r["result"]["step_idx"], 2)
        self.assertEqual(r["result"]["blobs"]["dom"], "dom.html")

    def test_read_missing(self) -> None:
        r = vs.read_session("nope", 9, base_path=self._tmp)
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_read_malformed(self) -> None:
        d = self._tmp / "run-bad" / "0"
        d.mkdir(parents=True)
        (d / "record.json").write_text("not json{", encoding="utf-8")
        r = vs.read_session("run-bad", 0, base_path=self._tmp)
        self.assertFalse(r["ok"])
        self.assertIn("malformed", r["error"])


class TestListSessions(_TmpBase):
    def test_list_empty_missing_run(self) -> None:
        r = vs.list_sessions("never-ran", base_path=self._tmp)
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["sessions"], [])

    def test_list_multiple_steps_sorted(self) -> None:
        # write out of order; assert list returns sorted by step_idx
        for idx in (2, 0, 1):
            vs.write_session("run-x", idx, "t", {"dom": {"html": f"<p>{idx}</p>"}},
                             base_path=self._tmp)
        r = vs.list_sessions("run-x", base_path=self._tmp)
        self.assertTrue(r["ok"])
        self.assertEqual([s["step_idx"] for s in r["result"]["sessions"]], [0, 1, 2])

    def test_list_skips_malformed(self) -> None:
        vs.write_session("run-y", 0, "t", {"dom": {"html": "<p/>"}}, base_path=self._tmp)
        bad = self._tmp / "run-y" / "1"
        bad.mkdir(parents=True)
        (bad / "record.json").write_text("garbage{", encoding="utf-8")
        r = vs.list_sessions("run-y", base_path=self._tmp)
        self.assertTrue(r["ok"])
        # only the valid step-0 record survives
        self.assertEqual([s["step_idx"] for s in r["result"]["sessions"]], [0])


class TestAtomicWriteSafety(_TmpBase):
    def test_blobs_written_before_record(self) -> None:
        """Q-D6: blobs-first, record-last. If the record write fails, the blobs
        are on disk but record.json is absent — read reports 'not found'."""
        with mock.patch.object(vs, "_atomic_write_json", side_effect=RuntimeError("interrupt")):
            r = vs.write_session("run-int", 0, "t", _BROWSER_SHAPED_OBS, base_path=self._tmp)
        self.assertFalse(r["ok"])  # never-raises: structured error
        d = self._tmp / "run-int" / "0"
        # blobs exist (written before the record), record does NOT
        self.assertTrue((d / "dom.html").exists())
        self.assertTrue((d / "console.json").exists())
        self.assertFalse((d / "record.json").exists())
        # the record is the commit point → read sees nothing
        self.assertFalse(vs.read_session("run-int", 0, base_path=self._tmp)["ok"])

    def test_atomic_record_write_no_corruption(self) -> None:
        """A failure during os.replace leaves no record.json and no leftover
        .tmp file (the tmp is unlinked, the exception re-raised + caught)."""
        with mock.patch.object(vs.os, "replace", side_effect=OSError("rename failed")):
            r = vs.write_session("run-rep", 0, "t", {"dom": {"html": "<p/>"}},
                                 base_path=self._tmp)
        self.assertFalse(r["ok"])
        d = self._tmp / "run-rep" / "0"
        self.assertFalse((d / "record.json").exists())
        # no leftover .tmp files
        self.assertEqual(list(d.glob("*.tmp")), [])


class TestNeverRaises(_TmpBase):
    def test_write_failure_returns_structured_error(self) -> None:
        # Put a FILE where the base dir's run subtree must go → mkdir raises
        # OSError → structured error, never raises.
        blocker = self._tmp / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        r = vs.write_session("run", 0, "t", {"dom": {"html": "<p/>"}}, base_path=blocker)
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_write_malformed_observation_never_raises(self) -> None:
        # dom present but missing "html" → KeyError caught by the broad ladder.
        r = vs.write_session("run-mal", 0, "t", {"dom": {"not_html": "x"}},
                             base_path=self._tmp)
        self.assertFalse(r["ok"])
        self.assertIn("unexpected error", r["error"])


class TestRoundTripWithBrowserShape(_TmpBase):
    """Step 2 AC4: the two modules' data shapes fit WITHOUT code coupling."""

    def test_browser_observations_round_trip(self) -> None:
        # write the browser-shaped fixture, read the record, resolve + load blobs
        wr = vs.write_session("rt", 0, "http://localhost:5173", _BROWSER_SHAPED_OBS,
                              base_path=self._tmp)
        self.assertTrue(wr["ok"], wr)
        rd = vs.read_session("rt", 0, base_path=self._tmp)
        self.assertTrue(rd["ok"], rd)
        rec = rd["result"]
        d = self._tmp / "rt" / "0"
        # resolve blobs via the record's relative pointers and compare to source
        dom_html = (d / rec["blobs"]["dom"]).read_text(encoding="utf-8")
        self.assertEqual(dom_html, _BROWSER_SHAPED_OBS["dom"]["html"])
        console = json.loads((d / rec["blobs"]["console"]).read_text(encoding="utf-8"))
        self.assertEqual(console, _BROWSER_SHAPED_OBS["console"])
        network = json.loads((d / rec["blobs"]["network"]).read_text(encoding="utf-8"))
        self.assertEqual(network, _BROWSER_SHAPED_OBS["network"])

    def test_no_code_coupling_between_modules(self) -> None:
        # AC4: browser.py does NOT import visibility_session.py (and vice versa).
        import anvil.integrations.browser as browser_mod
        browser_src = inspect.getsource(browser_mod)
        vs_src = inspect.getsource(vs)
        # browser.py does not reference visibility_session at all
        self.assertNotIn("visibility_session", browser_src)
        # visibility_session.py does not import browser
        self.assertFalse(any("import" in ln and "browser" in ln
                             for ln in vs_src.splitlines()))


if __name__ == "__main__":
    unittest.main()
