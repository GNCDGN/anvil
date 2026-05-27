"""Visibility-session state — v4 Phase 2a Step 2 + v4 Phase 3a Step 3 extension.

The persistent state type that records what a build's (or a co-pilot session's)
observation captured — DOM/console/network (browser-aware, Phase 2) and, from
Phase 3a, screen frames + the accessibility tree (screen-aware). Two keyings:

- **build mode** (Phase 2a): per-``(run_id, step_idx)`` during a build —
  ``state/visibility-sessions/<run_id>/<step_idx>/``.
- **co-pilot mode** (Phase 3a Step 3 / DC6): per-``(session_id, capture_idx)`` for
  a screen-aware co-pilot session (no run_id) —
  ``state/visibility-sessions/co-pilot-<session_id>/<capture_idx>/``. The
  ``co-pilot-`` directory prefix disambiguates the keyspace (Q-A7).

Both ship available-but-not-consumed in Phase 3a; the screen-aware observe-loop
(mid-build + co-pilot) that writes during a build/session lands in Phase 3c.

Storage shape (Q-D7 / Q-D6): a per-session directory holding the large blobs as
content-typed files plus a small structured ``record.json`` that references them
by RELATIVE filename. Blob types (Phase 3a Step 3 adds the last two):
``dom.html`` / ``console.json`` / ``network.json`` (text) + ``frame.png``
(**binary** PNG bytes — written via ``write_bytes``, NOT json) + ``accessibility.json``
(text). ``state/`` is git-ignored (Q-D3).

Contract (deploy_history.py / browser.py precedent): every public function NEVER
raises — ``{"ok": True, "result": …}`` / ``{"ok": False, "error": "<reason>"}``.

Write ordering (Q-D6 blobs-first, record-last): blobs written first, then the
record atomically (tmp-then-os.replace + fsync). ``record.json`` is the COMMIT
POINT — an interrupted write leaves blobs but no record, and the read reports "not
found", never a record pointing at a missing blob.

The record carries a ``mode`` field (Phase 3a Step 3): ``"build"`` or
``"co-pilot"``. It defaults to ``"build"`` so pre-3a records (no ``mode`` key) read
as build — the backwards-compat floor (Q-A7). ``digest`` is null until an
interpreter populates it (Phase 2c for browser/text; Phase 3c for screen/vision).

No new event kinds here — the ``screen.captured`` kind is added in Step 3's
``events.py`` change; the screen-aware observe-loop emits it in Phase 3c.
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
# Content-typed blob filenames (Q-D6; Phase 3a Step 3 adds frame + accessibility).
_BLOB_NAMES = {
    "dom": "dom.html",
    "console": "console.json",
    "network": "network.json",
    "frame": "frame.png",  # binary — PNG bytes via write_bytes (NOT json)
    "accessibility": "accessibility.json",
}
# "frame" carries raw PNG bytes (the binary blob); all other blobs are text/JSON.
_BINARY_BLOBS = {"frame"}

# Session modes (Phase 3a Step 3 / DC6). `mode` defaults to "build" so pre-3a
# records (no mode key) read as build — the backwards-compat floor (Q-A7).
_MODE_BUILD = "build"
_MODE_COPILOT = "co-pilot"
_COPILOT_PREFIX = "co-pilot-"


def _ok(result: dict) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    _log.warning("[visibility_session] %s", reason)
    return {"ok": False, "error": reason}


def _session_dir(base_path: Path, top_key: str, sub_idx: int) -> Path:
    """The per-session directory. build: ``<run_id>/<step_idx>``; co-pilot:
    ``co-pilot-<session_id>/<capture_idx>`` (the caller passes the prefixed
    top_key)."""
    return Path(base_path) / str(top_key) / str(sub_idx)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write `data` as JSON via tmp-then-os.replace + fsync. Creates
    the parent dir. Raises on I/O failure (the caller's never-raises ladder
    catches). Mirrors deploy_history.record_deploy's atomic write."""
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


def _write_blobs(session_dir: Path, obs: dict) -> dict:
    """Write each present blob (binary ``frame`` via write_bytes; ``dom`` as html;
    console/network/accessibility as JSON). Returns the blob-pointer dict — all
    blob keys present, ``None`` where the observation was absent."""
    blobs: dict = {k: None for k in _BLOB_NAMES}
    for key, fname in _BLOB_NAMES.items():
        val = obs.get(key)
        if val is None:
            continue
        path = session_dir / fname
        if key in _BINARY_BLOBS:
            path.write_bytes(val["frame_png"])  # raw PNG bytes (Step 3)
        elif key == "dom":
            path.write_text(val["html"], encoding="utf-8")
        else:  # console / network / accessibility → JSON
            path.write_text(json.dumps(val, indent=2), encoding="utf-8")
        blobs[key] = fname
    return blobs


def _write_record(
    base_path: Path,
    top_key: str,
    sub_idx: int,
    ids: dict,
    target: str,
    observations: dict,
    digest: str | None,
    mode: str,
    label: str,
) -> dict:
    """Shared write core for build + co-pilot. Blobs-first, record-last (Q-D6);
    the record is the commit point. `ids` are the mode-specific identity fields
    ({run_id, step_idx} or {session_id, capture_idx}). Never raises."""
    obs = observations or {}
    session_dir = _session_dir(base_path, top_key, sub_idx)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        blobs = _write_blobs(session_dir, obs)  # --- blobs first ---
        record = {
            **ids,
            "target": target,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": mode,
            "blobs": blobs,
            "digest": digest,  # null until an interpreter populates it
        }
        _atomic_write_json(session_dir / _RECORD_NAME, record)  # --- record last ---
        return _ok({"path": str(session_dir / _RECORD_NAME), "blobs": blobs})
    except OSError as exc:
        return _err(f"visibility-session write failed ({label}): {exc}")
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        _log.warning(
            "[visibility_session] unexpected error writing %s: %s",
            label, exc, exc_info=True,
        )
        return _err(f"visibility-session unexpected error ({label}): {type(exc).__name__}")


def write_session(
    run_id: str,
    step_idx: int,
    target: str,
    observations: dict,
    digest: str | None = None,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """Write a BUILD-mode visibility session (blobs + record) to
    ``<base_path>/<run_id>/<step_idx>/``. Never raises. `observations` matches
    browser.py / screen_capture.py / screen_browser.py return shapes (each key
    optional / may be None): ``dom`` {"html"}, ``console``/``network`` {"entries"},
    ``frame`` {"frame_png": bytes, …}, ``accessibility`` {"elements": […]}.
    Returns ``_ok({"path": <record path>, "blobs": {...}})``."""
    return _write_record(
        base_path, run_id, step_idx,
        {"run_id": run_id, "step_idx": step_idx},
        target, observations, digest, _MODE_BUILD, f"{run_id}/{step_idx}",
    )


def start_copilot_session(target: str, *, base_path: Path = _DEFAULT_BASE_PATH) -> dict:
    """Mint a CO-PILOT session id and create its directory. Returns
    ``_ok({"session_id": <id>, "target": target})``. The screen-aware co-pilot
    observe-loop (Phase 3c) writes captures into it via write_copilot_capture.
    Never raises."""
    session_id = "cp-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    try:
        (Path(base_path) / f"{_COPILOT_PREFIX}{session_id}").mkdir(parents=True, exist_ok=True)
        return _ok({"session_id": session_id, "target": target})
    except OSError as exc:
        return _err(f"co-pilot session start failed: {exc}")


def write_copilot_capture(
    session_id: str,
    capture_idx: int,
    target: str,
    observations: dict,
    digest: str | None = None,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """Write a CO-PILOT-mode capture to
    ``<base_path>/co-pilot-<session_id>/<capture_idx>/``. Same blob/record shape
    as write_session, with ``mode="co-pilot"`` + ``session_id``/``capture_idx``
    identity. Never raises."""
    return _write_record(
        base_path, f"{_COPILOT_PREFIX}{session_id}", capture_idx,
        {"session_id": session_id, "capture_idx": capture_idx},
        target, observations, digest, _MODE_COPILOT,
        f"co-pilot {session_id}/{capture_idx}",
    )


def _read_record(base_path: Path, top_key: str, sub_idx: int, label: str) -> dict:
    """Read + parse a ``record.json``. Never raises. ``_err`` with "not found"
    (missing) / "malformed" (bad JSON / non-object). Does NOT load blobs — the
    caller resolves blob paths via the record's ``blobs`` field (lazy)."""
    record_path = _session_dir(base_path, top_key, sub_idx) / _RECORD_NAME
    if not record_path.exists():
        return _err(f"visibility-session not found: {label}")
    try:
        raw = record_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _err(f"visibility-session read failed ({label}): {exc}")
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return _err(f"visibility-session record malformed ({label}): {exc}")
    if not isinstance(record, dict):
        return _err(f"visibility-session record malformed ({label}): not a JSON object")
    return _ok(record)


def read_session(
    run_id: str,
    step_idx: int,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """Read the BUILD-mode record at ``<base_path>/<run_id>/<step_idx>/``. Never
    raises. A pre-3a record (no ``mode`` key) reads unchanged — the reader does
    not require ``mode`` (the backwards-compat floor, Q-A7)."""
    return _read_record(base_path, run_id, step_idx, f"{run_id}/{step_idx}")


def read_copilot_capture(
    session_id: str,
    capture_idx: int,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """Read the CO-PILOT-mode record at
    ``<base_path>/co-pilot-<session_id>/<capture_idx>/``. Never raises."""
    return _read_record(
        base_path, f"{_COPILOT_PREFIX}{session_id}", capture_idx,
        f"co-pilot {session_id}/{capture_idx}",
    )


def list_sessions(
    run_id: str,
    *,
    base_path: Path = _DEFAULT_BASE_PATH,
) -> dict:
    """List all BUILD-mode step records under ``<base_path>/<run_id>/``, sorted by
    ``step_idx``. Missing run_id dir → ``_ok({"sessions": []})`` (the deploy_history
    missing-file → empty precedent). Malformed/unreadable records skipped with a
    logged warning. Never raises."""
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
