"""Shared deploy-history helper — v4 Phase 1c Step 2 (Q-C4).

Tracks one entry per successful deploy in `state/deploy-history.json` so the
deploy connectors (vercel, netlify) can determine first-deploy-vs-subsequent for
the confirmation gate. This is the genuine shared mechanism across both deploy
connectors (Q-C8: deploy-history lifts to a shared module; `_enforce_scope` does
not — the deploy connectors have no per-step scope axis).

Never-raises throughout: a missing / malformed / unreadable history file yields
``[]`` (+ a logged warning, mirroring brief.py's `_emit_parse_warnings`); a
failed write returns ``{"ok": False, "error": ...}``. `state/` is git-ignored
(`.gitignore` line 2), so the file is local-only.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("anvil.integrations.deploy_history")

# Module-level default; functions accept an explicit path for test-tmp injection.
_DEFAULT_PATH = Path("state/deploy-history.json")


def read_history(path: Path = _DEFAULT_PATH) -> list[dict]:
    """Return the parsed history list. Missing file → ``[]``; malformed JSON, a
    non-list payload, or a read error → ``[]`` + a logged warning. Never raises."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "[deploy_history] could not read %s: %s; treating as empty", path, exc
        )
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning(
            "[deploy_history] malformed JSON in %s: %s; treating as empty", path, exc
        )
        return []
    if not isinstance(data, list):
        log.warning(
            "[deploy_history] %s is not a JSON list (%s); treating as empty",
            path, type(data).__name__,
        )
        return []
    return data


def is_first_deploy(history: list[dict], project: str, target: str) -> bool:
    """True if no entry matches ``(project, target)`` with ``result ==
    'success'``. Only successful deploys count as 'this has deployed before' — a
    prior failed deploy still requires confirmation. Pure function over the
    in-memory list."""
    for entry in history or []:
        if (
            entry.get("project") == project
            and entry.get("target") == target
            and entry.get("result") == "success"
        ):
            return False
    return True


def record_deploy(
    path: Path, project: str, target: str, result: str, url: str | None
) -> dict:
    """Append one deploy entry to the history file via an atomic write-tmp-then-
    rename (so an interrupted write cannot corrupt the file). Creates the parent
    directory if absent. Returns ``{"ok": True}`` on success, ``{"ok": False,
    "error": ...}`` on write failure. Never raises.

    Entry shape: ``{"project", "target", "timestamp" (ISO-8601 UTC), "result",
    "url"}``."""
    path = Path(path)
    entry = {
        "project": project,
        "target": target,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "result": result,
        "url": url,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        history = read_history(path)
        history.append(entry)
        # Atomic: write to a tmp file in the same dir, fsync, then os.replace
        # (an atomic rename on POSIX) — never a partially-written history file.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("[deploy_history] could not write %s: %s", path, exc)
        return {"ok": False, "error": f"deploy_history write failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        log.error("[deploy_history] unexpected error writing %s: %s", path, exc)
        return {
            "ok": False,
            "error": f"deploy_history unexpected error: {type(exc).__name__}: {exc}",
        }
    return {"ok": True}
