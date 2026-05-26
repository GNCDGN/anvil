"""v4 Phase 1c Step 2: tests for anvil/integrations/deploy_history.py.

The shared deploy-history helper. All tests use a tmp path (no live
state/deploy-history.json writes). Covers read_history (happy / missing /
malformed / non-list), is_first_deploy (the success-only semantics), and
record_deploy (append shape, ISO timestamp, parent-dir creation, atomic write,
write-failure → structured error).
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from anvil.integrations import deploy_history


class TestReadHistory(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-dh-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_happy_path(self) -> None:
        p = self._tmp / "h.json"
        entries = [{"project": "a", "target": "vercel", "result": "success"}]
        p.write_text(json.dumps(entries), encoding="utf-8")
        self.assertEqual(deploy_history.read_history(p), entries)

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(deploy_history.read_history(self._tmp / "nope.json"), [])

    def test_malformed_json_returns_empty(self) -> None:
        p = self._tmp / "bad.json"
        p.write_text("not json{", encoding="utf-8")
        self.assertEqual(deploy_history.read_history(p), [])

    def test_non_list_payload_returns_empty(self) -> None:
        p = self._tmp / "obj.json"
        p.write_text('{"project": "a"}', encoding="utf-8")  # a dict, not a list
        self.assertEqual(deploy_history.read_history(p), [])


class TestIsFirstDeploy(unittest.TestCase):
    def test_empty_history_is_first(self) -> None:
        self.assertTrue(deploy_history.is_first_deploy([], "a", "vercel"))

    def test_non_matching_entry_is_first(self) -> None:
        h = [{"project": "b", "target": "vercel", "result": "success"}]
        self.assertTrue(deploy_history.is_first_deploy(h, "a", "vercel"))

    def test_different_target_is_first(self) -> None:
        h = [{"project": "a", "target": "netlify", "result": "success"}]
        self.assertTrue(deploy_history.is_first_deploy(h, "a", "vercel"))

    def test_matching_success_is_not_first(self) -> None:
        h = [{"project": "a", "target": "vercel", "result": "success"}]
        self.assertFalse(deploy_history.is_first_deploy(h, "a", "vercel"))

    def test_matching_failure_is_still_first(self) -> None:
        # Only successful deploys count as "deployed before".
        h = [{"project": "a", "target": "vercel", "result": "fail"}]
        self.assertTrue(deploy_history.is_first_deploy(h, "a", "vercel"))


class TestRecordDeploy(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-dh-rec-"))

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_happy_path_entry_shape(self) -> None:
        p = self._tmp / "h.json"
        res = deploy_history.record_deploy(p, "anvil", "vercel", "success",
                                           "https://x.vercel.app")
        self.assertTrue(res["ok"])
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        e = data[0]
        self.assertEqual(e["project"], "anvil")
        self.assertEqual(e["target"], "vercel")
        self.assertEqual(e["result"], "success")
        self.assertEqual(e["url"], "https://x.vercel.app")
        # timestamp is ISO-8601 parseable
        datetime.fromisoformat(e["timestamp"])

    def test_creates_parent_directory(self) -> None:
        p = self._tmp / "nested" / "deep" / "h.json"
        res = deploy_history.record_deploy(p, "a", "netlify", "success", "https://y")
        self.assertTrue(res["ok"])
        self.assertTrue(p.exists())

    def test_appends_to_existing(self) -> None:
        p = self._tmp / "h.json"
        deploy_history.record_deploy(p, "a", "vercel", "success", "https://1")
        deploy_history.record_deploy(p, "a", "vercel", "success", "https://2")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)
        self.assertEqual(data[1]["url"], "https://2")

    def test_write_failure_returns_structured_error(self) -> None:
        # Place a FILE where the parent directory should be → mkdir(parents)
        # raises an OSError → structured {"ok": False}, never-raises.
        blocker = self._tmp / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        p = blocker / "sub" / "h.json"
        res = deploy_history.record_deploy(p, "a", "vercel", "success", "https://z")
        self.assertFalse(res["ok"])
        self.assertIn("error", res)


if __name__ == "__main__":
    unittest.main()
