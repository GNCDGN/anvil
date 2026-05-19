"""Vault filesystem writes (Component 9, Phase 4 Step 2).

Atomic writes to ~/vaults/second-brain via Path.write_text → fsync →
os.replace pattern. Never-raises contract on every public function:
returns tuple[bool, str] on action functions, Path on pure derivation.

Module-scope _real_write captures Path.write_text before any code uses
it. Production code uses _real_write; tests patch anvil.vault_ops._real_write
freely. Same shape as ssh_ops._real_run (Phase 3 Step 3) — the Phase 2
Step 8 reset lesson held across modules.

INFO log markers ([vault_ops] wrote / write failed / checkpoint exists,
skipped) feed the exam harness's Capture parser (Phase 4 Step 6).

The Obsidian Git plugin auto-commits the vault within ~10 minutes of
writes; ANVIL does not run git itself on the vault. The vault filesystem
state on disk is the source of truth; commits are bookkeeping.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

# Module-scope capture — production uses _real_write; tests patch this
# attribute to inject failure modes. Captured before any code below
# uses Path.write_text directly.
_real_write = Path.write_text

log = logging.getLogger("anvil.vault_ops")


def atomic_write_text(path: Path, content: str) -> tuple[bool, str]:
    """Atomically write `content` to `path` via <path>.tmp + os.replace.

    Never raises. Returns (True, "") on success, (False, error) on failure.
    Tmp file is cleaned up on failure (best-effort).
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        _real_write(tmp, content, encoding="utf-8")
        os.replace(tmp, path)
        log.info(f"[vault_ops] wrote {path}")
        return (True, "")
    except (OSError, UnicodeError) as e:
        _cleanup_tmp(tmp)
        msg = f"{type(e).__name__}: {e}"
        log.info(f"[vault_ops] write failed {path}: {msg}")
        return (False, msg)
    except Exception as e:  # noqa: BLE001 — never-raise
        _cleanup_tmp(tmp)
        msg = f"{type(e).__name__}: {e}"
        log.info(f"[vault_ops] write failed {path}: {msg}")
        return (False, msg)


def append_setup_log_entry(setup_log_path: Path, entry: str) -> tuple[bool, str]:
    """Read existing setup-log, concat with `\n\n`, atomic-rewrite.

    Refuses to create if the source file does not exist (returns
    (False, "setup-log not found: <path>")). A missing setup-log
    indicates a misconfigured brief, not a routine state; the caller
    escalates rather than silently creating.

    Never raises. Returns (True, "") on success, (False, error) on failure.
    """
    setup_log_path = Path(setup_log_path)
    if not setup_log_path.is_file():
        msg = f"setup-log not found: {setup_log_path}"
        log.info(f"[vault_ops] write failed {setup_log_path}: {msg}")
        return (False, msg)
    try:
        existing = setup_log_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        msg = f"read failed: {type(e).__name__}: {e}"
        log.info(f"[vault_ops] write failed {setup_log_path}: {msg}")
        return (False, msg)
    except Exception as e:  # noqa: BLE001 — never-raise
        msg = f"read failed: {type(e).__name__}: {e}"
        log.info(f"[vault_ops] write failed {setup_log_path}: {msg}")
        return (False, msg)

    # Ensure exactly one blank line between existing content and the
    # new entry. Empty file → no separator (entry stands alone). Else,
    # pad to two trailing newlines on existing before appending.
    if existing == "":
        separator = ""
    elif existing.endswith("\n\n"):
        separator = ""
    elif existing.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    combined = existing + separator + entry
    if not combined.endswith("\n"):
        combined += "\n"
    return atomic_write_text(setup_log_path, combined)


def write_checkpoint(
    checkpoint_path: Path,
    frontmatter: dict[str, Any],
    body: str,
) -> tuple[bool, str]:
    """Compose YAML frontmatter + body, atomic-write to checkpoint_path.

    Idempotent: if `checkpoint_path` already exists, returns
    (True, "exists; skipped") and logs the skip without writing.
    Re-runs of a completed build do not overwrite the prior checkpoint;
    if a fresh checkpoint is wanted, the prior file must be removed
    manually first.

    Frontmatter rendering: simple YAML serialisation matching the
    observed checkpoint corpus (Step 0 Finding 3) — scalar values
    rendered as-is, list values rendered as JSON-style arrays on a
    single line. No nested structures.

    Never raises. Returns (True, "") on success, (True, "exists; skipped")
    on idempotent skip, (False, error) on failure.
    """
    checkpoint_path = Path(checkpoint_path)
    if checkpoint_path.exists():
        log.info(f"[vault_ops] checkpoint exists, skipped {checkpoint_path}")
        return (True, "exists; skipped")

    rendered = _render_frontmatter(frontmatter)
    full = f"---\n{rendered}---\n\n{body}"
    if not full.endswith("\n"):
        full += "\n"
    return atomic_write_text(checkpoint_path, full)


def derive_setup_log_path(brief_path: Path) -> Path:
    """Return the project's setup-log.md path derived from the brief path.

    Convention: briefs live at
      <vault>/01-Projects/.../<project>/builds/YYYY-MM-DD-<slug>/brief.md
    The setup-log for that project lives at
      <vault>/01-Projects/.../<project>/setup-log.md
    so `brief_path.parent.parent.parent / "setup-log.md"`.

    Pure function — no I/O. Existence check is the caller's responsibility.
    """
    return Path(brief_path).parent.parent.parent / "setup-log.md"


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------

def _cleanup_tmp(tmp: Path) -> None:
    """Best-effort tmp file removal. Swallows any exception."""
    try:
        tmp.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _render_frontmatter(fm: dict[str, Any]) -> str:
    """Render a flat dict as YAML-ish frontmatter lines.

    Matches the observed corpus shape (Step 0 Finding 3): scalar values
    on `key: value` lines, list values as inline JSON-style arrays.
    Strings with special chars are quoted; plain alphanumeric/path-like
    strings are unquoted (matches what Obsidian writes).
    """
    lines = []
    for key, value in fm.items():
        if isinstance(value, list):
            rendered_items = ", ".join(_render_scalar(v) for v in value)
            lines.append(f"{key}: [{rendered_items}]")
        else:
            lines.append(f"{key}: {_render_scalar(value)}")
    return "\n".join(lines) + "\n"


def _render_scalar(value: Any) -> str:
    """Render a scalar for YAML frontmatter.

    None → "null". Plain word/path-like strings → unquoted. Strings with
    spaces, special chars, or starting with a YAML-meaningful char →
    double-quoted (with internal quotes escaped). Other scalars (int,
    bool, etc.) → str() representation.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    # Simple unquoted: alphanumerics, dashes, underscores, dots, slashes,
    # colons (for ISO dates). Anything else gets quoted.
    if s and all(c.isalnum() or c in "-_./:" for c in s):
        return s
    # Quote: escape internal double-quotes and backslashes.
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
