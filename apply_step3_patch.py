#!/usr/bin/env python3
"""Phase 3 Step 3 patch — ssh_ops.py (new) + git_ops.push signature upgrade.

Subsumes the originally-planned Step 4 (git_ops.push) — the existing push()
returned bool with zero callers; Phase 3 needs the stderr for deploy escalation
messages, so upgrade the signature in-place to tuple[bool, str].

Applies:
  1. Upgrade git_ops.push from bool to tuple[bool, str]
  2. Create anvil/ssh_ops.py with ssh_run() and deploy()
  3. Create tests/test_ssh_ops.py with 9 test cases
  4. Update existing tests/test_git_ops.py for the push() signature change
     (if any test references push() — check first)

Run from ~/Downloads/anvil:
    .venv/bin/python apply_step3_patch.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
GIT_OPS_PY = REPO / "anvil" / "git_ops.py"
SSH_OPS_PY = REPO / "anvil" / "ssh_ops.py"
TEST_SSH_OPS_PY = REPO / "tests" / "test_ssh_ops.py"
TEST_GIT_OPS_PY = REPO / "tests" / "test_git_ops.py"


def fail(msg: str) -> None:
    print(f"[step3-patch] FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[step3-patch] {msg}")


def _backup(p: Path) -> None:
    bak = p.with_suffix(p.suffix + ".pre-phase-3-step-3.bak")
    if not bak.exists():
        shutil.copy2(p, bak)
        info(f"backed up {p.name} -> {bak.name}")


def _apply_unique(text: str, old: str, new: str, label: str) -> str:
    occurrences = text.count(old)
    if occurrences == 0:
        fail(f"{label}: anchor not found")
    if occurrences > 1:
        fail(f"{label}: anchor found {occurrences} times, expected 1")
    return text.replace(old, new, 1)


def patch_git_ops() -> bool:
    text = GIT_OPS_PY.read_text()
    # Detect if already upgraded (the new signature mentions tuple)
    if "def push(repo_path: Path, remote: str = \"origin\", branch: str = \"main\") -> tuple[bool, str]:" in text:
        info("git_ops.push already upgraded — skipping")
        return False

    _backup(GIT_OPS_PY)

    old_push = '''def push(repo_path: Path, remote: str = "origin", branch: str = "main") -> bool:
    """Push `branch` to `remote`. True on success, False on any failure
    (no remote, auth, network). Never raises. Not exercised against a real
    remote in tests — only in a Step 10 end-to-end run if explicitly enabled.
    """
    r = _git(repo_path, "push", remote, branch, check=False)
    return r.returncode == 0'''

    new_push = '''def push(repo_path: Path, remote: str = "origin", branch: str = "main") -> tuple[bool, str]:
    """Push `branch` to `remote`. Returns (ok, output). Never raises.

    Phase 3 Step 3: signature upgraded from bool to tuple[bool, str] to
    surface stderr to the deploy-stage escalation message. Zero pre-existing
    callers per Phase 3 Step 3 grep; safe to change in-place.

    No-op push (nothing to push, 'Everything up-to-date') returns (True, output).
    Non-zero exit returns (False, stdout+stderr).
    """
    r = _git(repo_path, "push", remote, branch, check=False)
    output = (r.stdout or "") + (r.stderr or "")
    return (r.returncode == 0, output)'''

    text = _apply_unique(text, old_push, new_push, "edit 1: git_ops.push signature upgrade")
    GIT_OPS_PY.write_text(text)
    info("patched git_ops.push (bool -> tuple[bool, str])")
    return True


def create_ssh_ops() -> bool:
    if SSH_OPS_PY.exists():
        info("anvil/ssh_ops.py already exists — skipping")
        return False

    content = '''"""SSH-to-VPS operations (implementation-notes Component 8, Phase 3).

Never-raises wrapper around ssh subprocess invocations, plus the four-stage
deploy chain: push, pull, restart, health-check.

Module-scope `_real_run` capture (Phase 2 Step 8 reset lesson): global
mock.patch on subprocess.run recurses if a delegating fake calls subprocess.run
during the patch. Production code uses _real_run; tests patch _real_run freely.
"""
from __future__ import annotations

import subprocess as _subprocess
import time
from pathlib import Path

# Captured before any test patch can install. Tests patch anvil.ssh_ops._real_run.
_real_run = _subprocess.run

# Default timeouts (seconds). Settle window after restart before is-active check.
_DEFAULT_TIMEOUT = 60
_SETTLE_SECONDS = 3


def ssh_run(host: str, user: str, cmd: str, timeout: int = _DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Run a remote command via SSH. Returns (ok, output). Never raises.

    On non-zero exit: ok=False, output=stdout+stderr concatenated.
    On TimeoutExpired / FileNotFoundError / other Exception: ok=False,
    output=repr(e).

    Uses the OpenSSH client's default key discovery (Mac's existing ~/.ssh
    keys per master design Part 7). No -i flag needed in the canonical
    deploy environment.
    """
    try:
        r = _real_run(
            ["ssh", f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except _subprocess.TimeoutExpired as e:
        return (False, f"TimeoutExpired({timeout}s): {e!r}")
    except FileNotFoundError as e:
        return (False, f"FileNotFoundError: {e!r}")
    except Exception as e:
        return (False, repr(e))

    output = (r.stdout or "") + (r.stderr or "")
    return (r.returncode == 0, output)


def deploy(brief, config) -> dict:
    """Full deploy chain: push, pull, restart, health-check.

    Returns dict with keys:
      stage: "push" | "pull" | "restart" | "health-check" | "complete"
      ok: bool
      output: str  # captured output from the failing stage, or empty for complete
      vps_head_sha: str | None  # post-pull HEAD on VPS, populated when stage advances past pull
      service_status: str | None  # systemctl is-active output, populated when stage advances past restart

    Never raises. Each sub-stage failure halts the chain and returns the
    corresponding failure dict; the orchestrator routes via deploy-{stage}-failed
    escalation reason.

    Expects brief.target_repo_path (Path), brief.vps_target_path (str),
    brief.service_name (str), config.vps_host (str, not None — caller verified),
    config.vps_user (str).
    """
    # Lazy import to avoid a circular dependency at module-load time
    # (ssh_ops -> git_ops is one-directional; this just defers it).
    from anvil import git_ops

    # 7a — Push from Mac
    push_ok, push_out = git_ops.push(Path(brief.target_repo_path), "origin", "main")
    if not push_ok:
        return {
            "stage": "push", "ok": False, "output": push_out,
            "vps_head_sha": None, "service_status": None,
        }

    # 7b — Pull on VPS
    pull_cmd = f"cd {brief.vps_target_path} && git pull --ff-only"
    pull_ok, pull_out = ssh_run(config.vps_host, config.vps_user, pull_cmd)
    if not pull_ok:
        return {
            "stage": "pull", "ok": False, "output": pull_out,
            "vps_head_sha": None, "service_status": None,
        }

    # Capture VPS HEAD after pull (best-effort; failure here doesn't halt deploy)
    head_cmd = f"cd {brief.vps_target_path} && git rev-parse HEAD"
    head_ok, head_out = ssh_run(config.vps_host, config.vps_user, head_cmd)
    vps_head_sha = head_out.strip() if head_ok else None

    # 7c — Restart service
    restart_cmd = f"systemctl restart {brief.service_name}"
    restart_ok, restart_out = ssh_run(config.vps_host, config.vps_user, restart_cmd)
    if not restart_ok:
        return {
            "stage": "restart", "ok": False, "output": restart_out,
            "vps_head_sha": vps_head_sha, "service_status": None,
        }

    # 7d — Health check after settle
    time.sleep(_SETTLE_SECONDS)
    health_cmd = f"systemctl is-active {brief.service_name}"
    health_ok, health_out = ssh_run(config.vps_host, config.vps_user, health_cmd)
    service_status = health_out.strip()
    if not health_ok or service_status != "active":
        return {
            "stage": "health-check", "ok": False, "output": health_out,
            "vps_head_sha": vps_head_sha, "service_status": service_status,
        }

    return {
        "stage": "complete", "ok": True, "output": "",
        "vps_head_sha": vps_head_sha, "service_status": service_status,
    }
'''
    SSH_OPS_PY.write_text(content)
    info("created anvil/ssh_ops.py")
    return True


def create_test_ssh_ops() -> bool:
    if TEST_SSH_OPS_PY.exists():
        info("tests/test_ssh_ops.py already exists — skipping")
        return False

    content = '''"""Phase 3 Step 3 tests — ssh_ops.py ssh_run + deploy chain.

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
        mock_run.return_value = _completed(returncode=0, stdout="ok\\n", stderr="")
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
        mock_run.return_value = _completed(returncode=1, stdout="partial\\n", stderr="error: bad\\n")
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
            _completed(0, "Already up to date.\\n", ""),       # pull
            _completed(0, "abc123def456\\n", ""),              # rev-parse HEAD
            _completed(0, "", ""),                              # restart
            _completed(0, "active\\n", ""),                    # is-active
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
            _completed(0, "Already up to date.\\n", ""),  # pull
            _completed(0, "abc123\\n", ""),                # rev-parse
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
            _completed(0, "Already up to date.\\n", ""),  # pull
            _completed(0, "abc123\\n", ""),                # rev-parse
            _completed(0, "", ""),                          # restart succeeded
            _completed(3, "failed\\n", ""),                # is-active returns 'failed'
        ]
        result = ssh_ops.deploy(self.brief, self.config)
        self.assertEqual(result["stage"], "health-check")
        self.assertFalse(result["ok"])
        self.assertEqual(result["service_status"], "failed")
        self.assertEqual(result["vps_head_sha"], "abc123")


if __name__ == "__main__":
    unittest.main()
'''
    TEST_SSH_OPS_PY.write_text(content)
    info("created tests/test_ssh_ops.py")
    return True


def patch_existing_git_ops_tests() -> bool:
    """Update existing tests/test_git_ops.py if it asserts the old bool return.
    If no push-related assertions, nothing to do."""
    if not TEST_GIT_OPS_PY.exists():
        info("tests/test_git_ops.py doesn't exist — nothing to update")
        return False

    text = TEST_GIT_OPS_PY.read_text()
    if "git_ops.push" not in text and "from anvil.git_ops import" not in text:
        info("tests/test_git_ops.py doesn't reference push — nothing to update")
        return False
    # If it imports/references push, check whether it asserts on the bool shape
    if "push(" in text:
        info("tests/test_git_ops.py references push(); manual inspection may be needed")
        info("(no automatic update applied — Step 3's smoke will reveal if anything breaks)")
    return False


def main() -> int:
    if not GIT_OPS_PY.exists():
        fail(f"git_ops.py not found at {GIT_OPS_PY}")

    changed_git = patch_git_ops()
    changed_ssh = create_ssh_ops()
    changed_test = create_test_ssh_ops()
    _ = patch_existing_git_ops_tests()

    if not (changed_git or changed_ssh or changed_test):
        info("nothing to do — patch already fully applied")
        return 0

    import py_compile
    try:
        py_compile.compile(str(GIT_OPS_PY), doraise=True)
        py_compile.compile(str(SSH_OPS_PY), doraise=True)
        py_compile.compile(str(TEST_SSH_OPS_PY), doraise=True)
        info("compile-check passed")
    except py_compile.PyCompileError as e:
        fail(f"compile-check failed: {e}")

    info("Step 3 patch applied. Next: run smoke")
    info("  .venv/bin/python -m unittest tests.test_ssh_ops tests.test_git_ops -v")
    info("  (then full discover suite)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
