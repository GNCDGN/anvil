"""Checkpoint orchestration (Component 10, Phase 4 Step 4).

Thin module — composes Planner artefact drafts with vault_ops writes.
No I/O of its own beyond what vault_ops does. No Anthropic calls (planner
owns those).

Public functions:
  - draft_and_preview(brief, state, planner) — call planner; return draft + preview
  - render_preview_message(draft, setup_log_path, checkpoint_path) — Telegram text
  - derive_checkpoint_path(brief, state, vault_path) — date + slug + outcome suffix
  - compose_checkpoint_frontmatter(brief, state, git_commit) — the seven-field dict
  - execute_writes(draft, setup_log_path, checkpoint_path, frontmatter) — paired writes

Step 0 Finding 3 narrowed the frontmatter shape: seven fields matching
observed corpus practice (not the documented richer shape). Step 0 Step-9-
only refinement narrowed the outcome suffixes: only -shipped and
-shipped-with-caveats (aborted runs never reach step 9, so -aborted is
unreachable).
"""
from __future__ import annotations

import logging
from datetime import date as _date
from pathlib import Path
from typing import Any

from anvil import vault_ops
from anvil.orchestrator import _slug

log = logging.getLogger("anvil.checkpoint")


def draft_and_preview(brief, state, planner) -> tuple[dict | None, str]:
    """Call planner.draft_completion_artefacts; return (draft, preview) on
    success or (None, error_detail) on escalation.

    The orchestrator routes the (None, detail) case to
    completion-artefacts-draft-failed escalation.
    """
    draft = planner.draft_completion_artefacts(brief, state)
    if draft.get("escalate") is True:
        return (None, draft.get("detail", "draft escalated"))
    # The preview message is rendered with paths the caller derives; this
    # function only validates the draft shape. Path rendering happens at
    # the call site after derive_checkpoint_path / derive_setup_log_path.
    return (draft, "")


def render_preview_message(
    draft: dict, setup_log_path: Path, checkpoint_path: Path,
) -> str:
    """Compose the Telegram preview text.

    Format per design §2.8: [ANVIL] prefix, header, both artefacts side by
    side, then the go/abort prompt. Voice-bound; no emoji, no exclamation.
    """
    # v2 Phase 1 Step 6: prefix sourced from voice helper so the
    # CALIBRATION_TELEGRAM_PREFIX env override applies here too.
    from anvil.voice import _prefix
    return (
        f"{_prefix()} Build complete. Drafted artefacts for review.\n\n"
        f"— Setup-log entry (to be appended to {setup_log_path.name}) —\n"
        f"{draft['setup_log_entry']}\n\n"
        f"— Checkpoint (to be written at checkpoints/active/{checkpoint_path.name}) —\n"
        f"{draft['checkpoint']}\n\n"
        f"This look right? go / abort"
    )


def derive_checkpoint_path(brief, state, vault_path: Path) -> Path:
    """YYYY-MM-DD-<slug>-<outcome>.md under checkpoints/active/.

    Slug from brief.build_name via orchestrator._slug (consistent slugging
    across the codebase per Step 0 cross-reference).

    Outcome suffix per Step 0 Step-9-only refinement:
      state.escalation_count == 0 → -shipped
      state.escalation_count > 0  → -shipped-with-caveats
    (No -aborted: step 9 only runs on state.status == "done"; aborted
    runs return 1 before the wrap. Existing corpus has no aborted
    checkpoints.)

    Date from state.started_at (the build's start day; matches existing
    corpus where checkpoint date = build day).
    """
    slug = _slug(brief.build_name)
    started = state.started_at  # ISO string like "2026-05-19T14:22:00+01:00"
    date_part = started.split("T", 1)[0] if "T" in started else started[:10]
    escalations = getattr(state, "escalation_count", 0) or 0
    suffix = "-shipped-with-caveats" if escalations > 0 else "-shipped"
    filename = f"{date_part}-{slug}{suffix}.md"
    return Path(vault_path) / "01-Projects/second-brain/checkpoints/active" / filename


def compose_checkpoint_frontmatter(
    brief, state, git_commit: str | None = None,
) -> dict[str, Any]:
    """Seven fields per Step 0 Finding 3 (corpus-aligned minimal shape).

    date         — ISO date from state.started_at
    source       — "anvil" (the discriminator)
    project      — brief.project
    tags         — [checkpoint, anvil, <brief.project>]
    author       — "claude"
    brief        — relative vault path (best-effort from state.brief_path)
    git_commit   — head of brief.target_repo_path (passed in by caller)

    Skipped per Step 0: time, type, files_touched, step, archive_after/by,
    status. Not in the consumed corpus shape; over-specification.
    """
    started = state.started_at
    date_part = started.split("T", 1)[0] if "T" in started else started[:10]

    # Best-effort relative-path rendering of brief. If brief_path is under
    # the vault, render relative; else render as-is. Pure string-handling
    # so it can't fail.
    brief_str = state.brief_path or ""
    brief_rel = brief_str
    for marker in ("01-Projects/", "00-Inbox/", "02-Areas/"):
        idx = brief_str.find(marker)
        if idx >= 0:
            brief_rel = brief_str[idx:]
            break

    return {
        "date": date_part,
        "source": "anvil",
        "project": brief.project,
        "tags": ["checkpoint", "anvil", brief.project],
        "author": "claude",
        "brief": brief_rel,
        "git_commit": git_commit or "null",
    }


def execute_writes(
    draft: dict,
    setup_log_path: Path,
    checkpoint_path: Path,
    frontmatter: dict[str, Any],
) -> tuple[bool, str]:
    """Append setup-log entry, then write checkpoint. Both atomic.

    Partial-write recovery: if setup-log succeeds but checkpoint fails,
    the setup-log entry stays. The orchestrator's escalation routing
    surfaces the partial state so Genco can write the checkpoint manually.

    Returns (True, "") on both-success; (False, error) on any failure.
    """
    ok1, err1 = vault_ops.append_setup_log_entry(
        setup_log_path, draft["setup_log_entry"]
    )
    if not ok1:
        return (False, f"setup-log append failed: {err1}")

    ok2, err2 = vault_ops.write_checkpoint(
        checkpoint_path, frontmatter, draft["checkpoint"]
    )
    if not ok2:
        return (
            False,
            f"checkpoint write failed (setup-log entry persisted): {err2}",
        )
    return (True, "")
