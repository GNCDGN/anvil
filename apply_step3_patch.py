#!/usr/bin/env python3
"""Phase 2 Step 3 — escalation reply grammar alignment.

Closes Phase 1 decision #19. Aligns the Telegram-message options text
to literal command tokens that the parser accepts. The previous prose
options ("fix and re-run / abort") never matched the hardcoded grammar
("go", "continue", "proceed", "abort") and a natural reply like
"fix and re-run" fell through to paused-by-user — costing ~$2 in re-spend
on Phase 1 Step 9.

What this script changes in anvil/orchestrator.py:
  1. _await_user_decision now accepts an `options` tuple and proceeds on
     any token in that tuple (case-insensitive). The hardcoded
     ("go", "continue", "proceed") set is retired.
  2. _escalate now takes `options` as a tuple of literal command tokens
     (default ("go", "abort")), pre-formats it into a display string, and
     remembers the tuple on `self._pending_options` so the next
     _await_user_decision picks it up. Source-compatible: existing call
     sites that pass a string still work but emit a warning the first time.
  3. The smoke-fail call site at line ~298 changes its options arg from
     the prose string "fix and re-run / abort" to ("go", "abort").
  4. The Planner-escalation call site at line ~260 keeps passing the
     Planner-emitted options (descriptive prose); _escalate now renders
     both the prose options as numbered context AND a "Reply go or abort"
     grammar line, and the await_user_decision call always uses ("go",
     "abort") for the grammar tokens regardless of the descriptive options.

Idempotent. Aborts non-zero if the expected anchors aren't found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORCH = ROOT / "anvil" / "orchestrator.py"

if not ORCH.is_file():
    print(f"error: {ORCH} not found. Run from ~/Downloads/anvil/.",
          file=sys.stderr)
    sys.exit(1)

src = ORCH.read_text(encoding="utf-8")
orig = src

# ---------------------------------------------------------------------------
# Edit 1: replace _escalate body so it accepts a tuple of literal tokens,
# remembers them for _await_user_decision, and renders descriptive options
# (from the Planner) as numbered context plus the grammar line.
# ---------------------------------------------------------------------------
escalate_old = (
    "    # ---- escalation ----\n"
    "    def _escalate(self, state, reason, detail, options) -> None:\n"
    "        self.telegram.send(voice.format_escalation(state, reason, detail, options))\n"
    "        self._log_event(\"escalation\", reason)\n"
)

escalate_new = (
    "    # ---- escalation ----\n"
    "    # Decision #19 (Phase 2 Step 3): the `options` argument is now a\n"
    "    # tuple of literal command tokens — ('go', 'abort') in the common\n"
    "    # case, ('abort',) when proceeding past the escalation makes no\n"
    "    # sense (e.g. planner-validation-failure). Planner-self-emitted\n"
    "    # `options` (a list of descriptive prose like 'amend brief to widen\n"
    "    # scope') are rendered as numbered context; the *grammar* the user\n"
    "    # replies with stays ('go', 'abort'). Source-compatible: a legacy\n"
    "    # string-shaped options arg still works (rendered as-is, grammar\n"
    "    # falls back to ('go', 'abort')) but emits a one-time warning.\n"
    "    def _escalate(self, state, reason, detail, options=(\"go\", \"abort\")) -> None:\n"
    "        prose_lines: list[str] = []\n"
    "        if isinstance(options, (list, tuple)) and options and all(\n"
    "            isinstance(o, str) for o in options\n"
    "        ):\n"
    "            # If every element looks like a single short token, treat as\n"
    "            # the grammar tuple. Otherwise treat as descriptive prose.\n"
    "            grammar = tuple(o.strip().lower() for o in options)\n"
    "            looks_like_tokens = all(\n"
    "                len(o) <= 16 and \" \" not in o for o in grammar\n"
    "            )\n"
    "            if looks_like_tokens:\n"
    "                display = \" / \".join(grammar)\n"
    "            else:\n"
    "                # Descriptive prose options from the Planner. Render as\n"
    "                # numbered list; grammar is the standard go/abort pair.\n"
    "                prose_lines = [\n"
    "                    f\"  {i + 1}. {opt}\" for i, opt in enumerate(options)\n"
    "                ]\n"
    "                grammar = (\"go\", \"abort\")\n"
    "                display = \"go / abort\"\n"
    "        elif isinstance(options, str):\n"
    "            # Legacy: a single string. Honour the contract but warn so\n"
    "            # remaining call sites get migrated.\n"
    "            log.warning(\n"
    "                \"_escalate received legacy string options=%r; \"\n"
    "                \"call sites should pass a tuple of literal tokens.\",\n"
    "                options,\n"
    "            )\n"
    "            grammar = (\"go\", \"abort\")\n"
    "            display = options\n"
    "        else:\n"
    "            grammar = (\"go\", \"abort\")\n"
    "            display = \"go / abort\"\n"
    "\n"
    "        if prose_lines:\n"
    "            detail_with_options = (\n"
    "                f\"{detail}\\n\\nPlanner suggests:\\n\"\n"
    "                + \"\\n\".join(prose_lines)\n"
    "                + \"\\n\\nReply: \" + display\n"
    "            )\n"
    "        else:\n"
    "            detail_with_options = detail\n"
    "\n"
    "        self.telegram.send(\n"
    "            voice.format_escalation(state, reason, detail_with_options, display)\n"
    "        )\n"
    "        self._log_event(\"escalation\", reason)\n"
    "        # Remembered for the immediately-following _await_user_decision.\n"
    "        self._pending_options = grammar\n"
)

if "self._pending_options = grammar" in src:
    print("[1/3] _escalate already migrated; skipping.")
elif escalate_old in src:
    src = src.replace(escalate_old, escalate_new, 1)
    print("[1/3] _escalate rewritten — options is now a token tuple.")
else:
    print(
        "error: could not find the exact _escalate definition to replace. "
        "Inspect anvil/orchestrator.py around the `_escalate` definition "
        "and apply by hand.",
        file=sys.stderr,
    )
    sys.exit(2)

# ---------------------------------------------------------------------------
# Edit 2: _await_user_decision reads self._pending_options (set by the
# preceding _escalate) instead of the hardcoded ("go", "continue",
# "proceed") tuple. The hardcoded set is retired.
# ---------------------------------------------------------------------------
await_old = (
    "    def _await_user_decision(self, state) -> bool:\n"
    "        \"\"\"Return True to proceed, False if the user aborts/pauses.\"\"\"\n"
    "        reply = self.telegram.wait_for_reply(timeout=None)\n"
    "        text = (reply.text.strip().lower() if reply else \"\")\n"
    "        if text in (\"go\", \"continue\", \"proceed\"):\n"
    "            return True\n"
    "        transition(state, \"aborted\" if text == \"abort\" else \"paused-by-user\")\n"
    "        return False\n"
)

await_new = (
    "    def _await_user_decision(self, state) -> bool:\n"
    "        \"\"\"Return True to proceed, False if the user aborts/pauses.\n"
    "\n"
    "        Decision #19 (Phase 2 Step 3): the accepted-token set is now\n"
    "        whatever the most recent _escalate stored on self._pending_options.\n"
    "        The previous hardcoded ('go', 'continue', 'proceed') set is retired;\n"
    "        the user-facing options line now lists literal command tokens\n"
    "        that match the grammar exactly. The grammar always includes\n"
    "        'abort' as the abort path.\n"
    "        \"\"\"\n"
    "        options = getattr(self, \"_pending_options\", (\"go\", \"abort\"))\n"
    "        reply = self.telegram.wait_for_reply(timeout=None)\n"
    "        text = (reply.text.strip().lower() if reply else \"\")\n"
    "        # An empty/missing reply is treated as paused-by-user, same as\n"
    "        # any other non-matching reply.\n"
    "        if text and text != \"abort\" and text in options:\n"
    "            return True\n"
    "        transition(state, \"aborted\" if text == \"abort\" else \"paused-by-user\")\n"
    "        return False\n"
)

if "getattr(self, \"_pending_options\"" in src:
    print("[2/3] _await_user_decision already migrated; skipping.")
elif await_old in src:
    src = src.replace(await_old, await_new, 1)
    print("[2/3] _await_user_decision rewritten — accepts the active escalation's tokens.")
else:
    print(
        "error: could not find the exact _await_user_decision definition to "
        "replace. Inspect anvil/orchestrator.py and apply by hand.",
        file=sys.stderr,
    )
    sys.exit(3)

# ---------------------------------------------------------------------------
# Edit 3: smoke-fail call site — replace prose options with token tuple.
# ---------------------------------------------------------------------------
smoke_old = (
    "                    self._escalate(\n"
    "                        state, \"smoke test failed\",\n"
    "                        smoke_out, \"fix and re-run / abort\",\n"
    "                    )\n"
)
smoke_new = (
    "                    self._escalate(\n"
    "                        state, \"smoke test failed\",\n"
    "                        smoke_out, (\"go\", \"abort\"),\n"
    "                    )\n"
)

if smoke_new in src:
    print("[3/3] smoke-fail call site already migrated; skipping.")
elif smoke_old in src:
    src = src.replace(smoke_old, smoke_new, 1)
    print("[3/3] smoke-fail call site now passes ('go', 'abort').")
else:
    # The Planner-escalation call site at line ~260 still passes
    # result.get("options") — that's Planner-emitted prose and is handled
    # by the new _escalate logic. Nothing to change there.
    print(
        "warning: smoke-fail call site anchor not matched. Inspect "
        "anvil/orchestrator.py around 'smoke test failed' and update the "
        "options argument by hand. The Planner-escalation site at the top "
        "of the step loop intentionally keeps passing result.get('options').",
        file=sys.stderr,
    )

# ---------------------------------------------------------------------------
# Write back if changed.
# ---------------------------------------------------------------------------
if src == orig:
    print("\nno changes to write. file already at patched state.")
    sys.exit(0)

backup = ORCH.with_suffix(".py.pre-phase-2-step-3.bak")
backup.write_text(orig, encoding="utf-8")
ORCH.write_text(src, encoding="utf-8")
print(f"\nwrote {ORCH} (backup at {backup})")
print("verify with:")
print("  .venv/bin/python -m py_compile anvil/orchestrator.py")
print("  .venv/bin/python -m unittest discover tests/ -v")
