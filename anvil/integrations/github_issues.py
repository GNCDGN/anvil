"""GitHub Issues connector — v4 Phase 1b Step 1 (brief Step 1; Phase 1 design
§"Per-sub-build split"; Q-B1/Q-B3/Q-B5/Q-B6).

A thin subprocess wrapper around the `gh` CLI's issue subcommands. Mirrors the
Coder's `claude --print` wrapper (coder.py `_real_run` + the FileNotFoundError /
TimeoutExpired / broad-Exception ladder) and `routing.call_model_for_subtask`:
every public function **never raises** — it returns a structured result on
success (``{"ok": True, "result": …}``) or a structured error on any failure
(``{"ok": False, "error": "<reason>"}``). The caller (a future orchestrator
wiring; Q-B5 leaves it unconsumed in Phase 1b) detects failure by inspecting
``["ok"]``, never a try/except.

Repo targeting (Q-B1): the build's target repo is passed explicitly via
``--repo <repo>`` (the caller passes the brief frontmatter's `target_repo`,
e.g. ``github.com/GNCDGN/anvil``, which `gh` accepts as ``[HOST/]OWNER/REPO``),
so a build can act on its own target repo regardless of cwd.

Scope (Q-B3): the step's declared ``issues:`` scope (``read`` / ``write`` /
None) is passed per call as the keyword-only ``scope`` arg and enforced HERE,
before any `gh` invocation — a `read`-scoped `create` returns a structured
out-of-scope error and never shells out. This is a separate axis from
`scope.operations` (Coder tool grants, coder.py:168-195) and from `model:`
(Planner routing); the three are validated and applied independently.

No new event kinds (Q-B4): the wrapper logs via the `anvil.integrations` logger
(the never-raises convention, like brief.py's `_emit_parse_warnings`) but emits
no operations-table event; `VALID_KINDS` is unchanged.
"""
from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger("anvil.integrations.github_issues")

# Subprocess wall-clock cap, matching the order of the Coder/`git_ops` timeouts.
# `gh` issue reads/creates are single REST round-trips; 30s is generous.
_GH_TIMEOUT = 30

# `--json` field sets. `view` adds `body` (the full issue text); `list` omits it
# to keep list payloads small. Both per the brief Step 1 spec.
_LIST_FIELDS = "number,title,state,labels,createdAt,updatedAt"
_VIEW_FIELDS = "number,title,state,body,labels,createdAt,updatedAt"

# The `issues:` scope axis (mirrors brief.ISSUES_SCOPES — kept local so the
# connector has no import dependency on brief.py; Q-B5 decoupling).
_READ_SCOPES = ("read", "write")  # a write scope permits reads
_WRITE_SCOPES = ("write",)


def _ok(result) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    return {"ok": False, "error": reason}


def _enforce_scope(operation: str, required: str, scope: str | None) -> dict | None:
    """Return a structured out-of-scope error, or None if the call is in scope.

    `operation` is the public-function label used in the message; `required` is
    the `issues:` level the operation needs (``read`` for list/view, ``write``
    for create); `scope` is the step's declared `issues:` scope. An undeclared
    (or unrecognised) scope is refused before any `gh` call, as is a write
    operation under a read-only scope.
    """
    if scope not in ("read", "write"):
        return _err("out-of-scope: issues scope not declared on this step")
    allowed = _WRITE_SCOPES if required == "write" else _READ_SCOPES
    if scope not in allowed:
        return _err(f"out-of-scope: {operation} requires issues: write")
    return None


def _run_gh(args: list[str]) -> dict:
    """Run a `gh` subprocess (never-raises). Returns ``{"ok": True, "stdout":
    …}`` on a zero exit, else a structured ``{"ok": False, "error": …}``. The
    error ladder mirrors coder.py's `_real_run` call site: a missing binary, a
    timeout, an OSError, the (check=False so it cannot fire) CalledProcessError,
    and a final broad catch for the never-raise contract. JSON parsing is the
    caller's concern (`_parse_json`)."""
    cmd = ["gh", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_GH_TIMEOUT,
        )
    except FileNotFoundError:
        return _err("gh CLI not found on PATH (is the GitHub CLI installed?)")
    except subprocess.TimeoutExpired:
        return _err(f"gh timed out after {_GH_TIMEOUT}s")
    except subprocess.CalledProcessError as exc:  # defensive: check=False, won't fire
        return _err(f"gh exited non-zero: {exc}")
    except OSError as exc:
        return _err(f"gh subprocess OSError: {exc}")
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        log.error("[github_issues] unexpected error running %r: %s", cmd, exc)
        return _err(f"gh unexpected error: {type(exc).__name__}: {exc}")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return _err(f"gh exited {proc.returncode}: {stderr[:300]}")
    return {"ok": True, "stdout": proc.stdout or ""}


def _parse_json(stdout: str) -> dict:
    """Parse `gh --json` stdout into a structured result; never-raises on
    malformed output (the design Part 6 / coder.py defensive-parse pattern)."""
    try:
        return _ok(json.loads(stdout))
    except (json.JSONDecodeError, TypeError):
        snippet = (stdout or "").strip()[:200]
        return _err(f"gh returned non-JSON: {snippet!r}")


def list_issues(repo: str, *, state=None, limit=None, scope=None) -> dict:
    """List issues on `repo` via `gh issue list --json …` (read scope).

    Optional `state` (`open`/`closed`/`all`) and `limit` (int) map to
    `--state`/`--limit`. Returns ``{"ok": True, "result": [<issue dicts>]}`` or
    a structured error. Refused (no `gh` call) when `scope` is not `read`/`write`.
    """
    blocked = _enforce_scope("list", "read", scope)
    if blocked:
        return blocked
    args = ["issue", "list", "--repo", repo, "--json", _LIST_FIELDS]
    if state:
        args += ["--state", str(state)]
    if limit is not None:
        args += ["--limit", str(limit)]
    run = _run_gh(args)
    if not run["ok"]:
        return run
    return _parse_json(run["stdout"])


def view_issue(repo: str, number, *, scope=None) -> dict:
    """View one issue on `repo` via `gh issue view <number> --json …` (read
    scope). Returns ``{"ok": True, "result": {<issue dict>}}`` or a structured
    error. Refused (no `gh` call) when `scope` is not `read`/`write`."""
    blocked = _enforce_scope("view", "read", scope)
    if blocked:
        return blocked
    args = ["issue", "view", str(number), "--repo", repo, "--json", _VIEW_FIELDS]
    run = _run_gh(args)
    if not run["ok"]:
        return run
    return _parse_json(run["stdout"])


def create_issue(repo: str, *, title, body, labels=None, scope=None) -> dict:
    """Create an issue on `repo` via `gh issue create` (write scope).

    `labels` (if given) pass through as one `--label <name>` per entry — no
    label-creation/curation logic (per the Phase 1b exclusions). `gh issue
    create` prints the new issue URL on stdout, so the result is
    ``{"ok": True, "result": {"url": "<url>"}}``. Refused WITHOUT a `gh` call
    when `scope` is not `write` (a `read`-scoped create returns
    ``out-of-scope: create requires issues: write``)."""
    blocked = _enforce_scope("create", "write", scope)
    if blocked:
        return blocked
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in (labels or []):
        args += ["--label", label]
    run = _run_gh(args)
    if not run["ok"]:
        return run
    # gh prints the created issue URL (last non-empty line of stdout).
    lines = [ln.strip() for ln in (run["stdout"] or "").splitlines() if ln.strip()]
    return _ok({"url": lines[-1] if lines else ""})
