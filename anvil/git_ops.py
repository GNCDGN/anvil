"""Git operations (implementation-notes Component 7).

All ops shell out to the `git` binary via subprocess.run with timeouts.
Hard failures raise GitError (Component 12); boolean-contract helpers
(`is_clean`, `push`) return bool. Never hangs (timeouts), never leaks
exceptions other than GitError.

Commit-message format (verbatim from Component 7):

    Step <N>: <step name> — <commit_message_hint or "auto">

    Plan summary: <plan.approach truncated to 200 chars>
    Brief: <brief_name>
    ANVIL run: <run-log-filename>

Signature/format reconciliation (flagged in the Step 7 report): Component 7's
documented signature is `commit_step(repo_path, plan, step_idx)`, but the
documented message needs the brief name, the brief step's commit-message
hint, and the run-log filename — none reachable from `plan`. Resolved by
keeping the positional signature and adding keyword-only optionals with
permanent-shaped defaults (no temporary "not built yet" placeholders). Step 8
wires the real values; nothing here needs removing later.

`commit_step` makes NO empty commit (Component 7 safety check 3): if there is
nothing to commit it returns "" so the caller can record `commit: null`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from anvil.errors import GitError

# Sensible default identity so commits don't fail in an identity-less
# environment. Phase 1+ may make this configurable; documented default.
_GIT_NAME = "ANVIL"
_GIT_EMAIL = "anvil@localhost"
_TIMEOUT = 30


def _git(repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out in {repo_path}") from exc
    except FileNotFoundError as exc:
        raise GitError("git binary not found on PATH") from exc
    if check and r.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {repo_path} "
            f"(exit {r.returncode}): {r.stderr.strip()[:300]}"
        )
    return r


def is_clean(repo_path: Path) -> bool:
    """True if the working tree has no uncommitted changes."""
    return _git(repo_path, "status", "--porcelain").stdout.strip() == ""


def files_changed_since(repo_path: Path, commit_hash: str) -> list[str]:
    """Names of files changed between `commit_hash` and HEAD."""
    out = _git(
        repo_path, "diff", "--name-only", f"{commit_hash}", "HEAD"
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def _build_commit_message(
    plan,
    *,
    brief_name: str | None,
    commit_message_hint: str | None,
    run_log_filename: str | None,
) -> str:
    approach = (plan.approach or "").strip()
    if len(approach) > 200:
        approach = approach[:200].rstrip() + "…"
    header = (
        f"Step {plan.step_number}: {plan.step_name} — "
        f"{commit_message_hint or 'auto'}"
    )
    return (
        f"{header}\n\n"
        f"Plan summary: {approach}\n"
        f"Brief: {brief_name or '(unknown brief)'}\n"
        f"ANVIL run: {run_log_filename or '(no run log)'}\n"
    )


def commit_step(
    repo_path: Path,
    plan,
    step_idx: int,
    *,
    brief_name: str | None = None,
    commit_message_hint: str | None = None,
    run_log_filename: str | None = None,
) -> str:
    """`git add -A`, then commit with the Component 7 message. Returns the
    new commit SHA, or "" if there was nothing to commit (caller records
    `commit: null` — Component 7 safety check 3: never commit empty)."""
    _git(repo_path, "add", "-A")
    # Anything staged? `git diff --cached --quiet` exits 1 iff there are
    # staged changes; 0 iff nothing to commit.
    staged = _git(repo_path, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return ""  # nothing to commit — caller records commit: null
    msg = _build_commit_message(
        plan,
        brief_name=brief_name,
        commit_message_hint=commit_message_hint,
        run_log_filename=run_log_filename,
    )
    _git(
        repo_path,
        "-c", f"user.name={_GIT_NAME}",
        "-c", f"user.email={_GIT_EMAIL}",
        "commit", "-m", msg,
    )
    return _git(repo_path, "rev-parse", "HEAD").stdout.strip()


def revert_to(repo_path: Path, commit_hash: str) -> bool:
    """Hard-reset to `commit_hash` (pause/abort recovery — invoked by a
    human decision, never automatically). True on success, False otherwise."""
    try:
        _git(repo_path, "reset", "--hard", commit_hash)
        return True
    except GitError:
        return False


def push(repo_path: Path, remote: str = "origin", branch: str = "main") -> bool:
    """Push `branch` to `remote`. True on success, False on any failure
    (no remote, auth, network). Never raises. Not exercised against a real
    remote in tests — only in a Step 10 end-to-end run if explicitly enabled.
    """
    r = _git(repo_path, "push", remote, branch, check=False)
    return r.returncode == 0
