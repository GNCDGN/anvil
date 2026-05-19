"""Phase 3 Step 3 tests — ssh_ops.py ssh_run + deploy chain.

Mocks anvil.ssh_ops._real_run with mock.patch (the module-scope capture
prevents the recursion bug that Phase 2 Step 8 reset for). Mocks
anvil.git_ops.push for the deploy-chain push stage. Patches time.sleep so
the suite doesn't actually wait 3s per deploy test.

No real SSH anywhere. Real verification lands at Step 7 (live deploy-enabled
Phase 4a build).
"""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from anvil import ssh_ops


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess result."""
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class _FakeBrief:
    """Minimal Brief stand-in for deploy() tests."""
    def __init__(self) -> None:
        self.target_repo_path = "/tmp/fake-repo"
        self.vps_target_path = "/home/vault-reporter/reporter"
        self.service_name = "vault-reporter-bot.service"


class _FakeConfig:
    def __init__(self) -> None:
        self.vps_host = "1.2.3.4"
        self.vps_user = "root"


class TestSshRun(unittest.TestCase):
    """ssh_run: never-raises subprocess wrapper around `ssh user@host cmd`."""

    @patch("anvil.ssh_ops._real_run")
    def test_happy_path_returns_true_and_stdout(self, mock_run) -> None:
        mock_run.return_value = _completed(returncode=0, stdout="ok\n", stderr="")
        ok, output = ssh_ops.ssh_run("1.2.3.4", "root", "echo ok")
        self.assertTrue(ok)
        self.assertIn("ok", output)
        # Verify the ssh command shape
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "ssh")
        self.assertEqual(args[1], "root@1.2.3.4")
        self.assertEqual(args[2], "echo ok")

    @patch("anvil.ssh_ops._real_run")
    def test_nonzero_exit_returns_false_and_combined_output(self, mock_run) -> None:
        mock_run.return_value = _completed(returncode=1, stdout="partial\n", stderr="error: bad\n")
        ok, output = ssh_ops.ssh_run("1.2.3.4", "root", "false")
        self.assertFalse(ok)
        self.assertIn("partial", output)
        self.assertIn("error: bad", output)

    @patch("anvil.ssh_ops._real_run")
    def test_timeout_returns_false_no_raise(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=60)
        ok, output = ssh_ops.ssh_run("1.2.3.4", "root", "sleep 999", timeout=60)
        self.assertFalse(ok)
        self.assertIn("TimeoutExpired", output)

    @patch("anvil.ssh_ops._real_run")
    def test_file_not_found_returns_false(self, mock_run) -> None:
        mock_run.side_effect = FileNotFoundError("ssh not on PATH")
        ok, output = ssh_ops.ssh_run("1.2.3.4", "root", "echo ok")
        self.assertFalse(ok)
        self.assertIn("FileNotFoundError", output)


class TestDeploy(unittest.TestCase):
    """deploy: four-stage chain. Mocks git_ops.push and ssh_ops._real_run."""

    def setUp(self) -> None:
        self.brief = _FakeBrief()
        self.config = _FakeConfig()
        # Patch time.sleep so the 3s settle doesn't actually fire in tests.
        self._sleep_patcher = patch("anvil.ssh_ops.time.sleep")
        self.mock_sleep = self._sleep_patcher.start()

    def tearDown(self) -> None:
        self._sleep_patcher.stop()

    @patch("anvil.ssh_ops._real_run")
    @patch("anvil.git_ops.push")
    def test_clean_run_returns_complete(self, mock_push, mock_ssh) -> None:
        """Happy path: push ok, pull ok, head-rev ok, restart ok, health-check active."""
        mock_push.return_value = (True, "")
        # Four ssh_run calls in deploy: pull, head-rev, restart, health-check
        mock_ssh.side_effect = [
            _completed(0, "Already up to date.\n", ""),       # pull
            _completed(0, "abc123def456\n", ""),              # rev-parse HEAD
            _completed(0, "", ""),                              # restart
            _completed(0, "active\n", ""),                    # is-active
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "complete")
        self.assertTrue(result["ok"])
        self.assertEqual(result["vps_head_sha"], "abc123def456")
        self.assertEqual(result["service_status"], "active")
        self.mock_sleep.assert_called_once_with(3)

    @patch("anvil.ssh_ops._real_run")
    @patch("anvil.git_ops.push")
    def test_push_fails_no_ssh(self, mock_push, mock_ssh) -> None:
        """Push stage fails: no SSH calls made; stage=push, ok=False."""
        mock_push.return_value = (False, "remote rejected: auth failed")
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "push")
        self.assertFalse(result["ok"])
        self.assertIn("auth failed", result["output"])
        self.assertIsNone(result["vps_head_sha"])
        self.assertIsNone(result["service_status"])
        mock_ssh.assert_not_called()

    @patch("anvil.ssh_ops._real_run")
    @patch("anvil.git_ops.push")
    def test_pull_fails_no_restart(self, mock_push, mock_ssh) -> None:
        """Pull stage fails: no restart or health-check; stage=pull."""
        mock_push.return_value = (True, "")
        mock_ssh.side_effect = [
            _completed(1, "", "non-fast-forward"),  # pull fails
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "pull")
        self.assertFalse(result["ok"])
        self.assertIn("non-fast-forward", result["output"])
        self.assertIsNone(result["vps_head_sha"])
        # Only one ssh call (the pull); no restart, no health-check
        self.assertEqual(mock_ssh.call_count, 1)

    @patch("anvil.ssh_ops._real_run")
    @patch("anvil.git_ops.push")
    def test_restart_fails_no_health_check(self, mock_push, mock_ssh) -> None:
        """Restart stage fails: no health-check; stage=restart, vps_head_sha set."""
        mock_push.return_value = (True, "")
        mock_ssh.side_effect = [
            _completed(0, "Already up to date.\n", ""),  # pull
            _completed(0, "abc123\n", ""),                # rev-parse
            _completed(1, "", "unit not found"),            # restart fails
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "restart")
        self.assertFalse(result["ok"])
        self.assertIn("unit not found", result["output"])
        self.assertEqual(result["vps_head_sha"], "abc123")
        self.assertIsNone(result["service_status"])
        self.assertEqual(mock_ssh.call_count, 3)
        # Health-check never reached, so sleep never called
        self.mock_sleep.assert_not_called()

    @patch("anvil.ssh_ops._real_run")
    @patch("anvil.git_ops.push")
    def test_health_check_failed_status(self, mock_push, mock_ssh) -> None:
        """Service comes up but is-active returns 'failed' not 'active'."""
        mock_push.return_value = (True, "")
        mock_ssh.side_effect = [
            _completed(0, "Already up to date.\n", ""),  # pull
            _completed(0, "abc123\n", ""),                # rev-parse
            _completed(0, "", ""),                          # restart succeeded
            _completed(3, "failed\n", ""),                # is-active returns 'failed'
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "health-check")
        self.assertFalse(result["ok"])
        self.assertEqual(result["service_status"], "failed")
        self.assertEqual(result["vps_head_sha"], "abc123")


if __name__ == "__main__":
    unittest.main()
