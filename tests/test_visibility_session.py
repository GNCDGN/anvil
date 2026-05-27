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
                         {"dom": "dom.html", "console": "console.json", "network": "network.json",
                          "frame": None, "accessibility": None})  # Phase 3a Step 3 keys
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
        self.assertEqual(rec["blobs"], {"dom": "dom.html", "console": None, "network": None,
                                        "frame": None, "accessibility": None})

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
        self.assertEqual(rec["blobs"], {"dom": None, "console": None, "network": None,
                                        "frame": None, "accessibility": None})


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


# v4 Phase 3a Step 3 fixtures — screen-aware blob shapes (screen_capture.py /
# screen_browser.py ["result"] shapes): frame {"frame_png": bytes, …},
# accessibility {"elements": [...], "query": …}.
_SCREEN_FRAME = b"\x89PNG\r\n\x1a\n_fake_screen_frame_bytes"
_ACCESSIBILITY_OBS = {
    "elements": [{"role": "AXButton", "label": "CX22",
                  "frame": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}}],
    "query": "cx22",
}


class TestScreenBlobsAndCopilot(_TmpBase):
    """Phase 3a Step 3: the screen blob types (binary frame + accessibility JSON),
    the mode field, and the co-pilot keyspace."""

    def test_frame_blob_is_binary(self) -> None:
        # frame.png is written via write_bytes (raw PNG bytes), NOT json.dump.
        r = vs.write_session(
            "run-f", 0, "screen://main",
            {"frame": {"frame_png": _SCREEN_FRAME, "width": 1440, "height": 900}},
            base_path=self._tmp,
        )
        self.assertTrue(r["ok"], r)
        frame_path = self._tmp / "run-f" / "0" / "frame.png"
        self.assertTrue(frame_path.exists())
        self.assertEqual(frame_path.read_bytes(), _SCREEN_FRAME)  # exact bytes, binary
        rec = json.loads((self._tmp / "run-f" / "0" / "record.json").read_text())
        self.assertEqual(rec["blobs"]["frame"], "frame.png")

    def test_accessibility_blob_is_json(self) -> None:
        r = vs.write_session(
            "run-a", 0, "screen://main", {"accessibility": _ACCESSIBILITY_OBS},
            base_path=self._tmp,
        )
        self.assertTrue(r["ok"], r)
        ax_path = self._tmp / "run-a" / "0" / "accessibility.json"
        self.assertEqual(json.loads(ax_path.read_text()), _ACCESSIBILITY_OBS)
        rec = json.loads((self._tmp / "run-a" / "0" / "record.json").read_text())
        self.assertEqual(rec["blobs"]["accessibility"], "accessibility.json")

    def test_build_record_has_mode_build(self) -> None:
        vs.write_session("run-m", 0, "t", {"dom": {"html": "<p/>"}}, base_path=self._tmp)
        rec = json.loads((self._tmp / "run-m" / "0" / "record.json").read_text())
        self.assertEqual(rec["mode"], "build")

    def test_copilot_session_start_and_roundtrip(self) -> None:
        s = vs.start_copilot_session("screen://Hetzner-console", base_path=self._tmp)
        self.assertTrue(s["ok"], s)
        sid = s["result"]["session_id"]
        self.assertTrue((self._tmp / f"co-pilot-{sid}").is_dir())
        w = vs.write_copilot_capture(
            sid, 0, "screen://Hetzner-console",
            {"frame": {"frame_png": _SCREEN_FRAME, "width": 800, "height": 600},
             "accessibility": _ACCESSIBILITY_OBS},
            base_path=self._tmp,
        )
        self.assertTrue(w["ok"], w)
        cap_dir = self._tmp / f"co-pilot-{sid}" / "0"
        self.assertTrue((cap_dir / "frame.png").exists())
        self.assertTrue((cap_dir / "accessibility.json").exists())
        r = vs.read_copilot_capture(sid, 0, base_path=self._tmp)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["mode"], "co-pilot")
        self.assertEqual(r["result"]["session_id"], sid)
        self.assertEqual(r["result"]["capture_idx"], 0)

    def test_copilot_keyspace_disjoint_from_build(self) -> None:
        # The co-pilot- prefix keeps the co-pilot keyspace disjoint from build run_ids.
        s = vs.start_copilot_session("t", base_path=self._tmp)
        sid = s["result"]["session_id"]
        vs.write_session("run-x", 0, "t", {"dom": {"html": "<p/>"}}, base_path=self._tmp)
        self.assertTrue((self._tmp / "run-x").is_dir())             # build keyspace
        self.assertTrue((self._tmp / f"co-pilot-{sid}").is_dir())   # co-pilot keyspace
        self.assertTrue(f"co-pilot-{sid}".startswith("co-pilot-"))

    def test_backwards_compat_record_without_mode_reads(self) -> None:
        # A pre-3a record (no `mode` key) must still read — the reader does not
        # require mode (the backwards-compat floor, Q-A7).
        d = self._tmp / "run-old" / "0"
        d.mkdir(parents=True)
        (d / "record.json").write_text(json.dumps(
            {"run_id": "run-old", "step_idx": 0, "target": "t",
             "blobs": {"dom": None, "console": None, "network": None}, "digest": None}))
        r = vs.read_session("run-old", 0, base_path=self._tmp)
        self.assertTrue(r["ok"], r)
        self.assertNotIn("mode", r["result"])  # absent in the old record; read tolerates it


if __name__ == "__main__":
    unittest.main()
