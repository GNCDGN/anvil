"""v5 Phase 1b — the Mac-side wake handler (_handle_wake + _resolve_wake_brief).
Mock-first: no Telegram, no real Orchestrator. Tests the per-reply logic + the
brief-path validation; the wake-listen loop is a thin wrapper over these."""
import os
import tempfile
import unittest

from anvil import cli


class _FakeTG:
    def __init__(self):
        self.sent = []
    def send(self, text):
        self.sent.append(text)
        return 1


class TestResolveWakeBrief(unittest.TestCase):
    def test_existing_md_resolves(self):
        fd, p = tempfile.mkstemp(suffix=".md"); os.close(fd)
        try:
            self.assertIsNotNone(cli._resolve_wake_brief(p, config=None))
        finally:
            os.unlink(p)

    def test_nonexistent_is_none(self):
        self.assertIsNone(cli._resolve_wake_brief("/nope/x.md", config=None))

    def test_non_md_is_none(self):
        fd, p = tempfile.mkstemp(suffix=".txt"); os.close(fd)
        try:
            self.assertIsNone(cli._resolve_wake_brief(p, config=None))
        finally:
            os.unlink(p)


class TestHandleWake(unittest.TestCase):
    def setUp(self):
        self.tg = _FakeTG()
        self.ran = []

    def _run_build(self, p):
        self.ran.append(p)
        return 0

    def test_go_valid_runs_build(self):
        fd, p = tempfile.mkstemp(suffix=".md"); os.close(fd)
        try:
            res = cli._handle_wake(f"go {p}", config=None, tg=self.tg, run_build=self._run_build)
            self.assertEqual(res["action"], "ran")
            self.assertEqual(res["rc"], 0)
            self.assertEqual([str(x) for x in self.ran], [os.path.realpath(p)])
            self.assertTrue(any("starting" in s for s in self.tg.sent))
            self.assertTrue(any("done" in s for s in self.tg.sent))
        finally:
            os.unlink(p)

    def test_skip(self):
        res = cli._handle_wake("skip", config=None, tg=self.tg, run_build=self._run_build)
        self.assertEqual(res["action"], "skipped")
        self.assertEqual(self.ran, [])
        self.assertTrue(any("skipped" in s for s in self.tg.sent))

    def test_go_invalid_path_does_not_run(self):
        res = cli._handle_wake("go /nope/x.md", config=None, tg=self.tg, run_build=self._run_build)
        self.assertEqual(res["action"], "invalid")
        self.assertEqual(self.ran, [])
        self.assertTrue(any("invalid" in s for s in self.tg.sent))

    def test_unrelated_message_ignored(self):
        res = cli._handle_wake("hello", config=None, tg=self.tg, run_build=self._run_build)
        self.assertEqual(res["action"], "ignored")
        self.assertEqual(self.ran, [])

    def test_build_error_caught_does_not_crash(self):
        fd, p = tempfile.mkstemp(suffix=".md"); os.close(fd)
        def boom(_): raise RuntimeError("orchestrator blew up")
        try:
            res = cli._handle_wake(f"go {p}", config=None, tg=self.tg, run_build=boom)
            self.assertEqual(res["action"], "ran")
            self.assertIsNone(res["rc"])
            self.assertTrue(any("errored" in s for s in self.tg.sent))
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
