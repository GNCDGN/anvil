#!/usr/bin/env python3
"""Phase 2 Step 7 — add head_hash() helper to git_ops.py.

Closes Phase 1 decisions #14/17 in spirit: commit_step is already real
(it ships since Phase 0 Step 7) but in manual-Coder mode Genco edits
+ commits in his own Claude Code session, so ANVIL's git add -A is a
no-op and commit_step returns "". The orchestrator currently records
state.commit = None for those steps.

The fix needs the orchestrator to populate state.commit from `git
rev-parse HEAD` whenever commit_step returns "" — Step 9 wires that.
Step 7 ships the helper: head_hash(repo_path) returning the current
HEAD SHA, or None on git failure (never raises).

Idempotent. Aborts non-zero if anchors are missing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GIT_OPS = ROOT / "anvil" / "git_ops.py"

if not GIT_OPS.is_file():
    print(f"error: {GIT_OPS} not found.", file=sys.stderr)
    sys.exit(1)

src = GIT_OPS.read_text(encoding="utf-8")
orig = src

# Insert head_hash() right after the existing is_clean() definition.
# is_clean is a small, single-purpose helper at module level; appending
# next to it keeps the boolean-and-introspection helpers grouped.
head_hash_anchor = (
    'def is_clean(repo_path: Path) -> bool:\n'
    '    """True if the working tree has no uncommitted changes."""\n'
    '    return _git(repo_path, "status", "--porcelain").stdout.strip() == ""\n'
)
head_hash_block = (
    'def is_clean(repo_path: Path) -> bool:\n'
    '    """True if the working tree has no uncommitted changes."""\n'
    '    return _git(repo_path, "status", "--porcelain").stdout.strip() == ""\n'
    '\n'
    '\n'
    'def head_hash(repo_path: Path) -> str | None:\n'
    '    """Return the current HEAD commit SHA, or None on git failure.\n'
    '\n'
    '    Phase 2 Step 7 addition (decisions #14/17 close at Step 9): in\n'
    '    manual-Coder mode Genco commits in his own Claude Code session,\n'
    '    so ANVIL\'s git_ops.commit_step() finds nothing to commit and\n'
    '    returns "". The orchestrator falls back to head_hash() to record\n'
    '    the attribution that already exists in the target repo\'s git log\n'
    '    — design Part 3 explicitly: "The state still records the head\n'
    '    commit hash via `git rev-parse HEAD` so attribution holds either\n'
    '    way." Returns None (not raise) on any git failure so the\n'
    '    orchestrator can keep going; state.commit stays None in that\n'
    '    case, same shape as before.\n'
    '    """\n'
    '    try:\n'
    '        r = _git(repo_path, "rev-parse", "HEAD", check=False)\n'
    '        if r.returncode != 0:\n'
    '            return None\n'
    '        sha = r.stdout.strip()\n'
    '        return sha or None\n'
    '    except GitError:\n'
    '        return None\n'
)

if "def head_hash(" in src:
    print("[1/1] head_hash already present; skipping.")
elif head_hash_anchor in src:
    src = src.replace(head_hash_anchor, head_hash_block, 1)
    print("[1/1] head_hash() helper added to git_ops.py.")
else:
    print("error: could not find is_clean anchor in git_ops.py.",
          file=sys.stderr)
    sys.exit(2)


if src != orig:
    backup = GIT_OPS.with_suffix(".py.pre-phase-2-step-7.bak")
    backup.write_text(orig, encoding="utf-8")
    GIT_OPS.write_text(src, encoding="utf-8")
    print(f"wrote {GIT_OPS} (backup at {backup})")
else:
    print("no changes to write.")

print("\nverify with:")
print("  .venv/bin/python -m py_compile anvil/git_ops.py")
print("  .venv/bin/python -m unittest discover tests/ -v")
