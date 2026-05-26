"""Visibility-session state — v4 Phase 2a Step 2 (brief Step 2; Phase 2 design
item D + DC7; Step 0 Q-D3/Q-D6 ratifications).

The persistent state type that pairs with the Step 1 browser substrate
(browser.py): it records what a build's browser observation captured — DOM,
console, network — keyed per ``(run_id, step_idx)``. Both ship
available-but-not-consumed in Phase 2a; the observe-loop that writes a session
during a build lands in Phase 2c.

Storage shape (Q-D7 hybrid / Q-D6): a per-session directory
``state/visibility-sessions/<run_id>/<step_idx>/`` holding the large blobs as
content-typed files (``dom.html``, ``console.json``, ``network.json``) plus a
small structured ``record.json`` that references them by RELATIVE filename. The
structured record is the queryable/auditable surface; the blobs are loaded
lazily by the caller. ``state/`` is git-ignored (.gitignore line 2) so no
.gitignore change is needed (Q-D3). No third-party dependency (stdlib only).

Contract (the connector-wrapper contract; deploy_history.py is the direct
precedent): every public function **never raises** — ``{"ok": True, "result":
…}`` on success, ``{"ok": False, "error": "<reason>"}`` on any failure. The
caller inspects ``["ok"]``.

Write ordering (Q-D6 blobs-first, record-last): ``write_session`` writes the
blob files first, then atomically writes ``record.json`` (tmp-then-os.replace +
fsync — deploy_history.record_deploy's pattern, adapted from append-one-list to
write-one-record). ``record.json`` is the COMMIT POINT: an interrupted write
leaves blobs on disk but no record, and ``read_session`` reports "not found" —
never a record pointing at a missing blob.

digest=None handling (Amendment 3): the record always carries a ``digest`` key;
in Phase 2a it is JSON ``null`` (the observation-digest routing to Haiku via
``call_model_for_subtask`` is Phase 2c). null (an explicit "no digest yet") is
cleaner than an absent field; Phase 2c populates it when it wires the
observe-loop's interpretation.

No new event kinds (DC7): logs via the ``anvil.integrations`` logger, emits no
operations-table event; ``VALID_KINDS`` stays 51. An ``observe.*`` kind lands in
Phase 2c when the observe-loop writes a session during a build (with a
``run_id`` context) — the connector-pattern.md Contract 5 condition.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("anvil.integrations.visibility_session")

# Module-level default; functions accept an explicit base_path for test-tmp
# injection (the deploy_history.py precedent). state/ is git-ignored.
_DEFAULT_BASE_PATH = Path("state/visibility-sessions")

_RECORD_NAME = "record.json"
# Content-typed blob filenames (Q-D6).
_BLOB_NAMES = {"dom": "dom.html", "console": "console.json", "network": "network.json"}


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    _log.warning("[visibility_session] %s", reason)
    return {"ok": False, "error": reason}


def _session_dir(base_path: Path, run_id: str, step_idx: int) -> Path:
    return Path(base_path) / run_id / str(step_idx)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write `data` as JSON to `path` via tmp-then-os.replace + fsync,
    so an interrupted write never leaves a partial/corrupt record. Creates the
    parent dir. Raises on I/O failure (the caller's never-raises ladder catches).
    Mirrors deploy_history.record_deploy's atomic write (mkstemp in the same dir,
    fsync the fd, os.replace the atomic rename, unlink the tmp on any failure)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic rename on POSIX
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_session(
    run_id: str,
    step_idx: int,
    target: str,
    observations: dict,
    digest: str | None = None,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """Write a visibility session (blobs + record) to
    ``<base_path>/<run_id>/<step_idx>/``. Never raises.

    `observations` matches browser.py's return shapes (each key optional / may be
    None):
        {"dom":     {"html": str}                                | None,
         "console": {"entries": [{"type": str, "text": str}, …]} | None,
         "network": {"entries": [{"url": str, "status": int}, …]}| None}

    Ordering (Q-D6): mkdir → write each present blob → atomically write
    record.json (the commit point). Returns ``_ok({"path": <record path>,
    "blobs": {...}})`` or a structured error.
    """
    obs = observations or {}
    session_dir = _session_dir(base_path, run_id, step_idx)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)

        # --- blobs first ---
        blobs: dict = {"dom": None, "console": None, "network": None}
        dom = obs.get("dom")
        if dom is not None:
            (session_dir / _BLOB_NAMES["dom"]).write_text(dom["html"], encoding="utf-8")
            blobs["dom"] = _BLOB_NAMES["dom"]
        console = obs.get("console")
        if console is not None:
            (session_dir / _BLOB_NAMES["console"]).write_text(
                json.dumps(console, indent=2), encoding="utf-8"
            )
            blobs["console"] = _BLOB_NAMES["console"]
        network = obs.get("network")
        if network is not None:
            (session_dir / _BLOB_NAMES["network"]).write_text(
                json.dumps(network, indent=2), encoding="utf-8"
            )
            blobs["network"] = _BLOB_NAMES["network"]

        # --- record last (the commit point) ---
        record = {
            "run_id": run_id,
            "step_idx": step_idx,
            "target": target,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "blobs": blobs,
            "digest": digest,  # null in Phase 2a (Amendment 3); 2c populates
        }
        _atomic_write_json(session_dir / _RECORD_NAME, record)
        return _ok({"path": str(session_dir / _RECORD_NAME), "blobs": blobs})
    except OSError as exc:
        return _err(f"visibility-session write failed ({run_id}/{step_idx}): {exc}")
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        _log.warning(
            "[visibility_session] unexpected error writing %s/%s: %s",
            run_id, step_idx, exc, exc_info=True,
        )
        return _err(
            f"visibility-session unexpected error ({run_id}/{step_idx}): "
            f"{type(exc).__name__}"
        )


def read_session(
    run_id: str,
    step_idx: int,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """Read the ``record.json`` at ``<base_path>/<run_id>/<step_idx>/``. Never
    raises. Does NOT load the blob files — the caller resolves blob paths via the
    record's ``blobs`` field (lazy blob loading). Returns ``_ok(<record dict>)``,
    or ``_err`` with "not found" (missing record) / "malformed" (bad JSON)."""
    record_path = _session_dir(base_path, run_id, step_idx) / _RECORD_NAME
    if not record_path.exists():
        return _err(f"visibility-session not found: {run_id}/{step_idx}")
    try:
        raw = record_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _err(f"visibility-session read failed ({run_id}/{step_idx}): {exc}")
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return _err(f"visibility-session record malformed ({run_id}/{step_idx}): {exc}")
    if not isinstance(record, dict):
        return _err(
            f"visibility-session record malformed ({run_id}/{step_idx}): "
            f"not a JSON object"
        )
    return _ok(record)


def list_sessions(
    run_id: str,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """List all step records under ``<base_path>/<run_id>/``, sorted by
    ``step_idx``. A missing run_id directory is the "no sessions yet" case →
    ``_ok({"sessions": []})`` (not an error; the deploy_history missing-file →
    empty precedent). Malformed/unreadable step records are skipped with a logged
    warning (never-raises). Returns ``_ok({"sessions": [<record dict>, …]})``."""
    run_dir = Path(base_path) / run_id
    if not run_dir.exists():
        return _ok({"sessions": []})
    try:
        step_dirs = [d for d in run_dir.iterdir() if d.is_dir()]
    except OSError as exc:
        return _err(f"visibility-session list failed ({run_id}): {exc}")

    sessions: list[dict] = []
    for d in step_dirs:
        record_path = d / _RECORD_NAME
        if not record_path.exists():
            _log.warning("[visibility_session] %s has no %s; skipping", d, _RECORD_NAME)
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            _log.warning(
                "[visibility_session] skipping malformed record %s: %s", record_path, exc
            )
            continue
        if not isinstance(record, dict):
            _log.warning("[visibility_session] skipping non-object record %s", record_path)
            continue
        sessions.append(record)

    # Sort by the record's step_idx (the source of truth); fall back to a large
    # sentinel for any record missing/!int step_idx so it sorts last, not crashes.
    def _key(rec: dict):
        v = rec.get("step_idx")
        return v if isinstance(v, int) else float("inf")

    sessions.sort(key=_key)
    return _ok({"sessions": sessions})
