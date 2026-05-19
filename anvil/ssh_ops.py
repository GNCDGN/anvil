"""SSH-to-VPS operations (implementation-notes Component 8, Phase 3).

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
