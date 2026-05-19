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

from anvil import events as _events

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

    repo_str = str(brief.target_repo_path)

    # 7a — Push from Mac. The ssh.stage.{start,end} pair wraps git_ops.push,
    # which fires its own git.push.{start,end} pair from Step 2. Distinct
    # kinds, no double-count: ssh.stage view is the deploy chain's lens
    # ("was this stage of the deploy ok"); git.push view is the git op's lens
    # ("did the push command succeed"). (notes.md Step 3 constraint 2)
    t = time.monotonic()
    _events.emit(
        "ssh.stage.start",
        {"stage": "push", "target_repo_path": repo_str},
    )
    push_ok, push_out = git_ops.push(Path(brief.target_repo_path), "origin", "main")
    _events.emit(
        "ssh.stage.end",
        {
            "stage": "push",
            "duration_ms": int((time.monotonic() - t) * 1000),
            "ok": push_ok,
            "output_chars": len(push_out or ""),
        },
    )
    if not push_ok:
        return {
            "stage": "push", "ok": False, "output": push_out,
            "vps_head_sha": None, "service_status": None,
        }

    # 7b — Pull on VPS. The pull stage logically encompasses the
    # best-effort HEAD capture below — both contribute to the
    # vps_head_sha surfaced in ssh.stage.end (pull).
    t = time.monotonic()
    _events.emit(
        "ssh.stage.start",
        {"stage": "pull", "target_repo_path": repo_str},
    )
    pull_cmd = f"cd {brief.vps_target_path} && git pull --ff-only"
    pull_ok, pull_out = ssh_run(config.vps_host, config.vps_user, pull_cmd)
    if not pull_ok:
        _events.emit(
            "ssh.stage.end",
            {
                "stage": "pull",
                "duration_ms": int((time.monotonic() - t) * 1000),
                "ok": False,
                "output_chars": len(pull_out or ""),
                "vps_head_sha": None,
            },
        )
        return {
            "stage": "pull", "ok": False, "output": pull_out,
            "vps_head_sha": None, "service_status": None,
        }

    # Capture VPS HEAD after pull (best-effort; failure here doesn't halt deploy)
    head_cmd = f"cd {brief.vps_target_path} && git rev-parse HEAD"
    head_ok, head_out = ssh_run(config.vps_host, config.vps_user, head_cmd)
    vps_head_sha = head_out.strip() if head_ok else None
    _events.emit(
        "ssh.stage.end",
        {
            "stage": "pull",
            "duration_ms": int((time.monotonic() - t) * 1000),
            "ok": True,
            "output_chars": len(pull_out or ""),
            "vps_head_sha": vps_head_sha,
        },
    )

    # 7c — Restart service
    t = time.monotonic()
    _events.emit(
        "ssh.stage.start",
        {"stage": "restart", "target_repo_path": repo_str},
    )
    restart_cmd = f"systemctl restart {brief.service_name}"
    restart_ok, restart_out = ssh_run(config.vps_host, config.vps_user, restart_cmd)
    _events.emit(
        "ssh.stage.end",
        {
            "stage": "restart",
            "duration_ms": int((time.monotonic() - t) * 1000),
            "ok": restart_ok,
            "output_chars": len(restart_out or ""),
        },
    )
    if not restart_ok:
        return {
            "stage": "restart", "ok": False, "output": restart_out,
            "vps_head_sha": vps_head_sha, "service_status": None,
        }

    # 7d — Health check after settle. The settle window precedes the
    # stage timer; it's not part of the health probe's wall clock.
    time.sleep(_SETTLE_SECONDS)
    t = time.monotonic()
    _events.emit(
        "ssh.stage.start",
        {"stage": "health", "target_repo_path": repo_str},
    )
    health_cmd = f"systemctl is-active {brief.service_name}"
    health_ok, health_out = ssh_run(config.vps_host, config.vps_user, health_cmd)
    service_status = health_out.strip()
    ok_health = health_ok and service_status == "active"
    _events.emit(
        "ssh.stage.end",
        {
            "stage": "health",
            "duration_ms": int((time.monotonic() - t) * 1000),
            "ok": ok_health,
            "output_chars": len(health_out or ""),
            "service_status": service_status,
        },
    )
    if not ok_health:
        return {
            "stage": "health-check", "ok": False, "output": health_out,
            "vps_head_sha": vps_head_sha, "service_status": service_status,
        }

    return {
        "stage": "complete", "ok": True, "output": "",
        "vps_head_sha": vps_head_sha, "service_status": service_status,
    }
