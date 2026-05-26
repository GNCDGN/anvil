"""Netlify deploy connector — v4 Phase 1c Step 2 (brief Step 2; Q-C2/Q-C4/Q-C5).

The second deploy connector, mirroring vercel.py's never-raises subprocess
shape and gate-complete from the start: it consults the shared `deploy_history`
helper before invoking the CLI, refuses a first deploy without `confirmed=True`
(the scope-refusal pattern, no CLI call), and records a successful deploy.

Auth posture (Q-C2): the operator's existing `netlify login` — no auth
management (the `gh`/`vercel` posture). The Netlify CLI is **not installed on
the build Mac** (Step 0 Q-C2-F1), so the argv shape below is taken from
Netlify's published CLI docs and is pending live ratification when the CLI is
installed; the test suite is mock-only regardless (Q-C2/Q-C6 hermetic).

`project` (the deploy-history key) is the stable logical project identifier
(the orchestrator supplies `brief.project`; absent → the deploy directory's
name). `site` (CLI targeting) is a separate concern — Netlify's `--site` flag
selects the site to deploy, distinct from the history identity (Q-C4).

The orchestrator's deploy chain is untouched; this connector is available-but-
not-consumed in first-pass (Q-C5) — the confirmation *prompt* (supplying
`confirmed`) is deferred build-loop wiring; the wrapper only *signals*.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from anvil.integrations import deploy_history

log = logging.getLogger("anvil.integrations.netlify")

_NETLIFY_TIMEOUT = 300


def _ok(result) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    return {"ok": False, "error": reason}


def _run_netlify(argv: list[str], *, cwd: str, timeout: int) -> dict:
    """Run a `netlify` subprocess (never-raises). Mirrors vercel._run_vercel's
    error ladder: a missing binary, a timeout, the (check=False so it cannot
    fire) CalledProcessError, an OSError, and a final broad catch."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return _err(
            "netlify CLI not found on PATH (is the Netlify CLI installed and "
            "`netlify login` done?)"
        )
    except subprocess.TimeoutExpired:
        return _err(f"netlify timed out after {timeout}s")
    except subprocess.CalledProcessError as exc:  # defensive: check=False, won't fire
        return _err(f"netlify exited non-zero: {exc}")
    except OSError as exc:
        return _err(f"netlify subprocess OSError: {exc}")
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        log.error("[netlify] unexpected error running %r: %s", argv, exc)
        return _err(f"netlify unexpected error: {type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return _err(f"netlify exited {proc.returncode}: {stderr[:300]}")
    return {"ok": True, "stdout": proc.stdout or ""}


def deploy(cwd, *, prod=False, site=None, confirmed=False, project=None,
           history_path=None) -> dict:
    """Deploy the build in `cwd` to Netlify (never-raises, gate-complete).

    `prod=False` runs `netlify deploy` (a draft/preview deploy); `prod=True`
    runs `netlify deploy --prod`. `site` (optional) targets a specific Netlify
    site via `--site <id-or-slug>`; absent → the cwd-linked site
    (`.netlify/state.json`). `project` is the deploy-history key (absent → the
    deploy directory's name). `history_path` overrides the history file (tests).

    First-deploy gate (mirrors vercel.py exactly): a first `(project, "netlify")`
    deploy without `confirmed=True` returns a structured
    ``deploy-confirmation-required`` result WITHOUT invoking the CLI; a confirmed
    first deploy or any subsequent deploy proceeds, recording on CLI success.
    """
    target = "netlify"
    proj = project if project is not None else Path(str(cwd)).name
    hpath = Path(history_path) if history_path is not None else deploy_history._DEFAULT_PATH

    # First-deploy confirmation gate (Step 2; the same flow as vercel.py).
    history = deploy_history.read_history(hpath)
    if deploy_history.is_first_deploy(history, proj, target) and not confirmed:
        return {
            "ok": False,
            "error": f"deploy-confirmation-required: first deploy of {proj} to {target}",
            "requires_confirmation": True,
        }

    argv = ["netlify", "deploy"]
    if prod:
        argv.append("--prod")
    if site is not None:
        argv += ["--site", str(site)]
    run = _run_netlify(argv, cwd=str(cwd), timeout=_NETLIFY_TIMEOUT)
    if not run["ok"]:
        return run  # CLI failed — do NOT record (only successes are recorded)
    # netlify prints the deploy URL inline (e.g. "Website Draft URL: https://…");
    # take the last http(s) token anywhere in stdout.
    urls = re.findall(r"https?://[^\s]+", run["stdout"] or "")
    if not urls:
        snippet = (run["stdout"] or "").strip()[:200]
        return _err(f"netlify produced no deploy URL: {snippet!r}")
    url = urls[-1].rstrip(".,;)")
    deploy_history.record_deploy(hpath, proj, target, "success", url)  # best-effort post-success
    return _ok({"url": url, "status": "deployed"})
