"""Vercel deploy connector — v4 Phase 1c Step 1 (brief Step 1; Q-C1).

A thin subprocess wrapper around the `vercel` CLI, mirroring the never-raises
shape of github_issues.py's `_run_gh` (FileNotFoundError / TimeoutExpired /
CalledProcessError / OSError / broad-Exception ladder). Every call returns a
structured ``{"ok": True, "result": {...}}`` on success or ``{"ok": False,
"error": "<reason>"}`` on any failure — no exception escapes.

Auth posture (Q-C1): relies on the operator's existing `vercel login` — the
connector does not manage auth (the same posture as `gh`). The Vercel CLI is
**not installed on the build Mac** (Step 0 Q-C1-F1), so the argv shape below is
taken from Vercel's published CLI docs and is pending live ratification when the
CLI is installed; the test suite is mock-only regardless (Q-C1/Q-C6 hermetic).

**Step 1 ships the bare wrapper only.** There is NO confirmation gate, NO
`confirmed` parameter, NO `deploy-history.json` consultation — Step 2 retrofits
all three (per brief Amendment 1; the deploy-history helper does not exist until
Step 2). The orchestrator's `vps_deploy`/`vps_target_path` deploy chain is
untouched; this connector is available-but-not-consumed in first-pass (Q-C5).
"""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger("anvil.integrations.vercel")

# Deploys are slower than the read connectors' calls (a build + upload), so the
# wall-clock cap is generous relative to the gh wrapper's 30s.
_VERCEL_TIMEOUT = 300


def _ok(result) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    return {"ok": False, "error": reason}


def _run_vercel(argv: list[str], *, cwd: str, timeout: int) -> dict:
    """Run a `vercel` subprocess (never-raises). Returns ``{"ok": True,
    "stdout": ...}`` on a zero exit, else a structured error. The error ladder
    mirrors github_issues._run_gh: a missing binary, a timeout, the (check=False
    so it cannot fire) CalledProcessError, an OSError, and a final broad catch
    for the never-raise contract. Output parsing is the caller's concern."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return _err(
            "vercel CLI not found on PATH (is the Vercel CLI installed and "
            "`vercel login` done?)"
        )
    except subprocess.TimeoutExpired:
        return _err(f"vercel timed out after {timeout}s")
    except subprocess.CalledProcessError as exc:  # defensive: check=False, won't fire
        return _err(f"vercel exited non-zero: {exc}")
    except OSError as exc:
        return _err(f"vercel subprocess OSError: {exc}")
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        log.error("[vercel] unexpected error running %r: %s", argv, exc)
        return _err(f"vercel unexpected error: {type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return _err(f"vercel exited {proc.returncode}: {stderr[:300]}")
    return {"ok": True, "stdout": proc.stdout or ""}


def deploy(cwd, *, prod=False, project=None) -> dict:
    """Deploy the build in `cwd` to Vercel (never-raises).

    `prod=False` runs `vercel` (a preview deploy); `prod=True` runs
    `vercel --prod` (production). `--yes` is always passed so the non-interactive
    subprocess does not stall on a prompt. `project` (optional) targets a
    specific project directory via Vercel's `--cwd` flag; when `None`, the
    subprocess `cwd` (and its `.vercel/project.json` link) determines the
    project. The Vercel CLI prints the deployment URL on stdout; the result is
    ``{"ok": True, "result": {"url": "<url>", "status": "deployed"}}``. A zero
    exit with no URL on stdout is treated as malformed output (structured error).

    Step 1 has NO confirmation gate — a first-vs-subsequent check + a `confirmed`
    kwarg are retrofitted in Step 2 (Amendment 1).
    """
    argv = ["vercel"]
    if prod:
        argv.append("--prod")
    argv.append("--yes")  # non-interactive: accept defaults, do not prompt
    if project is not None:
        argv += ["--cwd", str(project)]  # target a specific project directory
    run = _run_vercel(argv, cwd=str(cwd), timeout=_VERCEL_TIMEOUT)
    if not run["ok"]:
        return run
    # vercel prints the deployment URL on stdout (last http(s) line).
    lines = [ln.strip() for ln in (run["stdout"] or "").splitlines() if ln.strip()]
    url = next((ln for ln in reversed(lines) if ln.startswith("http")), "")
    if not url:
        snippet = (run["stdout"] or "").strip()[:200]
        return _err(f"vercel produced no deploy URL: {snippet!r}")
    return _ok({"url": url, "status": "deployed"})
