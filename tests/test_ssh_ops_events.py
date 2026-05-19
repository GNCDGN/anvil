"""v2 Phase 1 Step 3 — ssh_ops.deploy event instrumentation.

Mocks anvil.ssh_ops._real_run for the four ssh_run calls (pull, head-rev,
restart, health) and anvil.git_ops.push for the push stage. Patches
time.sleep so the 3s settle window is instant. Hermetic ANVIL_ROOT
redirect under tmp_path so events.jsonl writes don't escape the test.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from anvil import events
from anvil import ssh_ops


def _completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=rc, stdout=stdout, stderr=stderr,
    )


class _FakeBrief:
    def __init__(self) -> None:
        self.target_repo_path = "/tmp/fake-repo"
        self.vps_target_path = "/home/anvil-calibration-noop/app"
        self.service_name = "anvil-v2-calibration-noop.service"


class _FakeConfig:
    def __init__(self) -> None:
        self.vps_host = "1.2.3.4"
        self.vps_user = "root"


class _SshEventsBase(unittest.TestCase):

    def setUp(self) -> None:
        # Module state reset.
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(
            os.environ, {"ANVIL_ROOT": str(self.tmp_path)}
        )
        self._env_patch.start()
        events.begin_run("ssh-events-test")

        # Skip the 3s settle window.
        self._sleep_patch = mock.patch("anvil.ssh_ops.time.sleep")
        self._sleep_patch.start()

        self.brief = _FakeBrief()
        self.config = _FakeConfig()

    def tearDown(self) -> None:
        events.end_run()
        self._sleep_patch.stop()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _events(self) -> list[dict]:
        path = (
            self.tmp_path / "state" / "runs"
            / "ssh-events-test" / "events.jsonl"
        )
        if not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]

    def _stage_events(self) -> list[dict]:
        return [e for e in self._events() if e["kind"].startswith("ssh.stage.")]


class TestDeployEvents(_SshEventsBase):

    @mock.patch("anvil.ssh_ops._real_run")
    @mock.patch("anvil.git_ops.push")
    def test_happy_path_emits_four_pairs(self, mock_push, mock_ssh) -> None:
        mock_push.return_value = (True, "Everything up-to-date\n")
        # ssh_run order: pull, head-rev, restart, health.
        mock_ssh.side_effect = [
            _completed(0, "Already up to date.\n", ""),
            _completed(0, "abc123def456\n", ""),
            _completed(0, "", ""),
            _completed(0, "active\n", ""),
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "complete")
        self.assertTrue(result["ok"])

        stages = self._stage_events()
        # 4 start + 4 end = 8 ssh.stage events
        self.assertEqual(len(stages), 8)
        # Pairs in order: push.start, push.end, pull.start, pull.end, ...
        seq = [(e["kind"], e["data"]["stage"]) for e in stages]
        self.assertEqual(seq, [
            ("ssh.stage.start", "push"),
            ("ssh.stage.end",   "push"),
            ("ssh.stage.start", "pull"),
            ("ssh.stage.end",   "pull"),
            ("ssh.stage.start", "restart"),
            ("ssh.stage.end",   "restart"),
            ("ssh.stage.start", "health"),
            ("ssh.stage.end",   "health"),
        ])
        # All four .end events report ok=True.
        for e in stages:
            if e["kind"] == "ssh.stage.end":
                self.assertTrue(e["data"]["ok"], f"stage {e['data']['stage']} not ok")
        # pull.end carries vps_head_sha; health.end carries service_status.
        pull_end = next(e for e in stages
                        if e["kind"] == "ssh.stage.end" and e["data"]["stage"] == "pull")
        health_end = next(e for e in stages
                          if e["kind"] == "ssh.stage.end" and e["data"]["stage"] == "health")
        self.assertEqual(pull_end["data"]["vps_head_sha"], "abc123def456")
        self.assertEqual(health_end["data"]["service_status"], "active")

    @mock.patch("anvil.ssh_ops._real_run")
    @mock.patch("anvil.git_ops.push")
    def test_pull_failure_no_restart_or_health(self, mock_push, mock_ssh) -> None:
        mock_push.return_value = (True, "")
        # First ssh_run = pull, returns non-zero (failure).
        mock_ssh.side_effect = [
            _completed(1, "", "fatal: not a git repository\n"),
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "pull")
        self.assertFalse(result["ok"])

        stages = self._stage_events()
        seq = [(e["kind"], e["data"]["stage"]) for e in stages]
        # push pair (ok), pull pair (failed) — no restart, no health.
        self.assertEqual(seq, [
            ("ssh.stage.start", "push"),
            ("ssh.stage.end",   "push"),
            ("ssh.stage.start", "pull"),
            ("ssh.stage.end",   "pull"),
        ])
        push_end = stages[1]
        pull_end = stages[3]
        self.assertTrue(push_end["data"]["ok"])
        self.assertFalse(pull_end["data"]["ok"])
        # The failed pull.end carries vps_head_sha=None (head capture not attempted).
        self.assertIsNone(pull_end["data"]["vps_head_sha"])

    @mock.patch("anvil.ssh_ops._real_run")
    @mock.patch("anvil.git_ops.push")
    def test_restart_failure_no_health(self, mock_push, mock_ssh) -> None:
        mock_push.return_value = (True, "")
        mock_ssh.side_effect = [
            _completed(0, "Already up to date.\n", ""),    # pull
            _completed(0, "deadbeef\n", ""),                # head-rev
            _completed(1, "", "Unit not found\n"),          # restart fails
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "restart")
        self.assertFalse(result["ok"])

        stages = self._stage_events()
        seq = [(e["kind"], e["data"]["stage"]) for e in stages]
        self.assertEqual(seq, [
            ("ssh.stage.start", "push"),
            ("ssh.stage.end",   "push"),
            ("ssh.stage.start", "pull"),
            ("ssh.stage.end",   "pull"),
            ("ssh.stage.start", "restart"),
            ("ssh.stage.end",   "restart"),
        ])
        restart_end = stages[-1]
        self.assertFalse(restart_end["data"]["ok"])

    @mock.patch("anvil.ssh_ops._real_run")
    @mock.patch("anvil.git_ops.push")
    def test_stage_labels_all_four(self, mock_push, mock_ssh) -> None:
        mock_push.return_value = (True, "")
        mock_ssh.side_effect = [
            _completed(0, "ok\n", ""),
            _completed(0, "sha\n", ""),
            _completed(0, "", ""),
            _completed(0, "active\n", ""),
        ]
        ssh_ops.deploy(self.brief, self.config)
        labels = {e["data"]["stage"] for e in self._stage_events()}
        self.assertEqual(labels, {"push", "pull", "restart", "health"})

    @mock.patch("anvil.ssh_ops._real_run")
    @mock.patch("anvil.git_ops.push")
    def test_duration_ms_captured(self, mock_push, mock_ssh) -> None:
        # Inject a busy-wait in the push helper so duration_ms > 0.
        # (Plain time.sleep won't work — setUp patches anvil.ssh_ops.time.sleep
        # at the module level, which replaces time.sleep globally for the
        # duration of the test. A monotonic-clock busy-wait is unaffected.)
        def slow_push(*args, **kwargs):
            t0 = time.monotonic()
            while (time.monotonic() - t0) < 0.02:
                pass
            return (True, "")

        mock_push.side_effect = slow_push
        mock_ssh.side_effect = [
            _completed(0, "ok\n", ""),
            _completed(0, "sha\n", ""),
            _completed(0, "", ""),
            _completed(0, "active\n", ""),
        ]
        ssh_ops.deploy(self.brief, self.config)
        push_end = next(
            e for e in self._stage_events()
            if e["kind"] == "ssh.stage.end" and e["data"]["stage"] == "push"
        )
        self.assertGreaterEqual(push_end["data"]["duration_ms"], 15)


if __name__ == "__main__":
    unittest.main()
