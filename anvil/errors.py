"""ANVIL exception hierarchy (implementation-notes Component 12).

All ANVIL errors derive from AnvilError. They are caught at component
boundaries and translated into escalations or state transitions; none should
propagate out of Orchestrator.run() except KeyboardInterrupt (caught at the
very top, persists state, exits cleanly).

ConfigError is part of this hierarchy as of Step 3 (folded in per orchestrator
instruction; added to the Component 12 enumeration in implementation-notes in
Step 3's Section B vault patch). config.py imports it from here rather than
defining it locally.
"""
from __future__ import annotations


class AnvilError(Exception):
    """Base. Should never escape Orchestrator.run()."""


class ConfigError(AnvilError):
    """Required configuration missing or malformed (see config.py)."""


class BriefValidationError(AnvilError):
    """A submitted brief violates one or more schema rules.

    Carries the full list of violations so the orchestrator can surface all
    of them in a single Telegram message, not just the first.
    """

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            "Brief rejected:\n- " + "\n- ".join(self.errors)
            if self.errors
            else "Brief rejected (no detail)"
        )


class PlannerError(AnvilError):
    """Planner produced no usable plan (timeout, malformed JSON, etc.)."""


class CoderError(AnvilError):
    """Coder step execution failed or exceeded its scope."""


class StateCorruptError(AnvilError):
    """current-run.json is unreadable or internally inconsistent."""


class TelegramError(AnvilError):
    """Telegram send/poll failed past its retry budget."""


class GitError(AnvilError):
    """A git operation failed."""


class SshError(AnvilError):
    """An SSH-to-VPS operation failed (Phase 3)."""


class VaultWriteError(AnvilError):
    """A vault filesystem write failed (Phase 4)."""
