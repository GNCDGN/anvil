"""State file management (implementation-notes Component 5).

`current-run.json` is the machine-readable source of truth; `current-run.md`
is regenerated from it. Both are written atomically: write to a `.tmp`
sibling, then `os.rename` (atomic on the same filesystem) so a reader doing
`cat current-run.md` / `read_state()` mid-transition never observes a
partially-written file.

State dir resolution: `ANVIL_STATE_DIR` env override else `<anvil_root>/state`
(anvil_root resolved the same way config.py does). The env override is a
small, deliberate extension of Component 5's module-level `STATE_DIR` so the
test suite can run hermetically against a tmp dir without touching the real
`~/Downloads/anvil/state/`. The dir is created if absent. Not symlinked into
the vault — clean separation from vault knowledge.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from anvil.errors import StateCorruptError

_STATUS = Literal[
    "running", "waiting", "paused-by-user",
    "paused-mid-execution", "done", "failed", "aborted",
]


class StepState(BaseModel):
    n: int
    name: str
    status: Literal["pending", "running", "done", "failed", "paused-after"] = "pending"
    commit: str | None = None
    smoke: Literal["pass", "fail"] | None = None
    smoke_output: str | None = None
    plan: dict | None = None
    coder_result: dict | None = None


class PendingAction(BaseModel):
    type: Literal[
        "step_confirmation", "escalation", "manual_coder", "resume_confirm"
    ]
    telegram_message_id: int | None = None
    sent_at: str
    expected_reply: str | None = None


class State(BaseModel):
    schema_version: int = 1
    brief_path: str
    started_at: str
    finished_at: str | None = None
    status: _STATUS = "running"
    current_step: int = 1
    steps: list[StepState] = []
    pending_action: PendingAction | None = None
    coder_mode: Literal["manual", "auto"] = "auto"
    # Full path to this run's append-only run log, set when the log is opened
    # in handle_brief so git_ops.commit_step can reference its filename.
    # Added Step 8 (orchestrator-directed); not in Component 5's schema —
    # flagged for doc parity. Optional/back-compatible.
    run_log: str | None = None


def state_dir() -> Path:
    env = os.environ.get("ANVIL_STATE_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    default_root = Path(__file__).resolve().parent.parent
    root = Path(
        os.path.expanduser(os.environ.get("ANVIL_ROOT", str(default_root)))
    ).resolve()
    return root / "state"


def _json_path() -> Path:
    return state_dir() / "current-run.json"


def _md_path() -> Path:
    return state_dir() / "current-run.md"


def init_state(brief, started_at: str, brief_path: str | None = None,
                coder_mode: Literal["manual", "auto"] = "auto") -> State:
    """Build the initial State for a brief.

    Component 5 shows `init_state(brief, started_at)`; the Brief model carries
    no source path, so `brief_path` is an added optional argument (defaults to
    the brief's build_name when not supplied). Documented deviation.
    """
    return State(
        brief_path=brief_path or getattr(brief, "build_name", "") or "",
        started_at=started_at,
        status="running",
        current_step=1,
        steps=[
            StepState(n=s.number, name=s.name, status="pending")
            for s in brief.steps
        ],
        coder_mode=coder_mode,
    )


def write_state(state: State) -> None:
    """Write current-run.json then regenerate current-run.md, each atomically
    (`.tmp` + os.rename). Creates the state dir if absent."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)

    json_path = _json_path()
    tmp_json = json_path.parent / (json_path.name + ".tmp")
    tmp_json.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    os.rename(tmp_json, json_path)

    md_path = _md_path()
    tmp_md = md_path.parent / (md_path.name + ".tmp")
    tmp_md.write_text(_render_state_md(state), encoding="utf-8")
    os.rename(tmp_md, md_path)


def read_state() -> State | None:
    """Return the current State, or None if no current-run.json exists.
    Raises StateCorruptError if the file exists but is unreadable/invalid."""
    json_path = _json_path()
    if not json_path.is_file():
        return None
    try:
        return State.model_validate_json(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StateCorruptError(
            f"current-run.json exists but is invalid: {exc}"
        ) from exc


def transition(state: State, status: str, **kwargs) -> State:
    """Return a new State with `status` (and any kwargs) applied, and persist
    it. Pydantic-immutable-friendly: builds a copy rather than mutating."""
    update: dict = {"status": status, **kwargs}
    new_state = state.model_copy(update=update)
    write_state(new_state)
    return new_state


def _render_state_md(state: State) -> str:
    total = len(state.steps)
    lines: list[str] = [
        "# ANVIL — current run",
        "",
        f"**Brief:** {os.path.basename(state.brief_path) or state.brief_path}",
        f"**Started:** {state.started_at}",
        f"**Status:** {state.status}",
        f"**Step:** {state.current_step} of {total}",
        "",
        "## Steps",
        "",
        "| # | Name | Status | Commit | Smoke |",
        "|---|---|---|---|---|",
    ]
    for s in state.steps:
        lines.append(
            f"| {s.n} | {s.name} | {s.status} | {s.commit or '—'} "
            f"| {s.smoke or '—'} |"
        )
    lines += ["", "## Current step", ""]
    cur = next((s for s in state.steps if s.n == state.current_step), None)
    if cur is not None:
        lines.append(f"**Step {cur.n} — {cur.name}**")
        lines.append(f"Status: {cur.status} · coder_mode: {state.coder_mode}")
    else:
        lines.append("(no current step)")
    lines += ["", "## Pending action", ""]
    pa = state.pending_action
    if pa is None:
        lines.append(f"(none — {state.status})")
    else:
        lines.append(f"**Waiting for:** {pa.type}")
        lines.append(f"**Sent:** {pa.sent_at}")
        lines.append(f"**Expected reply:** {pa.expected_reply or '—'}")
    return "\n".join(lines) + "\n"
