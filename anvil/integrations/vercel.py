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

**Step 2 retrofit (Amendment 1):** the first-deploy confirmation gate, the
`confirmed` kwarg, and the shared `deploy_history` consultation were added here
in Step 2 (Step 1 shipped the bare wrapper). The gate flow mirrors netlify.py
exactly. The orchestrator's `vps_deploy`/`vps_target_path` deploy chain is
untouched; this connector is available-but-not-consumed in first-pass (Q-C5) —
the wrapper *signals* confirmation-required; the *prompt* is deferred wiring.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from anvil.integrations import deploy_history

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


def deploy(cwd, *, prod=False, project=None, confirmed=False,
           history_path=None) -> dict:
    """Deploy the build in `cwd` to Vercel (never-raises, gate-complete).

    `prod=False` runs `vercel` (a preview deploy); `prod=True` runs
    `vercel --prod` (production). `--yes` is always passed so the non-interactive
    subprocess does not stall on a prompt. The subprocess `cwd` (and its
    `.vercel/project.json` link) determines the deployed project. `project` is
    the deploy-history key (absent → the deploy directory's name); `history_path`
    overrides the history file (tests). The Vercel CLI prints the deployment URL
    on stdout; the result is ``{"ok": True, "result": {"url": "<url>", "status":
    "deployed"}}``. A zero exit with no URL on stdout is malformed output.

    First-deploy gate (Step 2 retrofit per Amendment 1; mirrors netlify.py
    exactly): a first `(project, "vercel")` deploy without `confirmed=True`
    returns a structured ``deploy-confirmation-required`` result WITHOUT invoking
    the CLI; a confirmed first deploy or any subsequent deploy proceeds, recording
    on CLI success.

    (Note vs Step 1: `project` no longer maps to `--cwd` — it is the
    deploy-history identity; the CLI targets the subprocess `cwd`, which is how
    Vercel locates the linked project, so the Step 1 `--cwd` mapping was
    redundant. See V4P1C Step 2 / Q-C4.)
    """
    target = "vercel"
    proj = project if project is not None else Path(str(cwd)).name
    hpath = Path(history_path) if history_path is not None else deploy_history._DEFAULT_PATH

    # First-deploy confirmation gate (Step 2; the same flow as netlify.py).
    history = deploy_history.read_history(hpath)
    if deploy_history.is_first_deploy(history, proj, target) and not confirmed:
        return {
            "ok": False,
            "error": f"deploy-confirmation-required: first deploy of {proj} to {target}",
            "requires_confirmation": True,
        }

    argv = ["vercel"]
    if prod:
        argv.append("--prod")
    argv.append("--yes")  # non-interactive: accept defaults, do not prompt
    run = _run_vercel(argv, cwd=str(cwd), timeout=_VERCEL_TIMEOUT)
    if not run["ok"]:
        return run  # CLI failed — do NOT record (only successes are recorded)
    # vercel prints the deployment URL on stdout (the last http(s) token —
    # handles both own-line and inline "URL: https://..." shapes).
    urls = re.findall(r"https?://[^\s]+", run["stdout"] or "")
    if not urls:
        snippet = (run["stdout"] or "").strip()[:200]
        return _err(f"vercel produced no deploy URL: {snippet!r}")
    url = urls[-1].rstrip(".,;)")
    deploy_history.record_deploy(hpath, proj, target, "success", url)  # best-effort post-success
    return _ok({"url": url, "status": "deployed"})
