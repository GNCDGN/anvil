#!/usr/bin/env python3
"""Phase 2 Step 2 — apply the decision #15 fix to anvil/orchestrator.py.

Idempotent. Run from ~/Downloads/anvil/ ; the script edits
anvil/orchestrator.py in-place and prints what it changed.

What it does:
  1. Adds `resumed_state: State | None = None` kwarg to handle_brief.
  2. On the resume path, bypasses init_state / _open_run_log / _move_brief
     and re-uses the loaded state.
  3. Skips already-done steps in the step loop (status == "done").
  4. Threads `resumed_state=st` through from Orchestrator.resume()'s call
     to handle_brief.

If any of the expected anchor strings is missing, the script aborts
with a non-zero exit and explains what it couldn't find — meaning the
file has drifted from the Phase 1 shape this fix was designed against,
and the patch needs review before being applied.
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
# Edit 1: import the State type so the new type hint resolves.
# ---------------------------------------------------------------------------
import_anchor = "from anvil.state import (\n    PendingAction,\n    init_state,"
new_import = (
    "from anvil.state import (\n    PendingAction,\n    State,\n    init_state,"
)
if import_anchor not in src:
    print("error: could not find expected `from anvil.state import` block.",
          file=sys.stderr)
    print("file may have drifted from Phase 1 shape; review the patch by hand.",
          file=sys.stderr)
    sys.exit(2)
if "    State,\n" not in src.split("from anvil.state import (")[1].split(")")[0]:
    src = src.replace(import_anchor, new_import, 1)
    print("[1/4] added `State` to the anvil.state import block.")
else:
    print("[1/4] `State` already imported; skipping.")

# ---------------------------------------------------------------------------
# Edit 2: handle_brief signature gains resumed_state kwarg, and the
# init/open-log/move-brief preamble is wrapped so it only runs on a
# fresh run (not on resume).
# ---------------------------------------------------------------------------
sig_anchor = "    def handle_brief(self, brief_path: Path) -> int:"
sig_new = (
    "    def handle_brief(\n"
    "        self, brief_path: Path, *, resumed_state: State | None = None,\n"
    "    ) -> int:"
)
if sig_anchor not in src and sig_new not in src:
    print("error: could not find handle_brief signature anchor.", file=sys.stderr)
    sys.exit(3)
if sig_anchor in src:
    src = src.replace(sig_anchor, sig_new, 1)
    print("[2/4] handle_brief signature now accepts resumed_state kwarg.")
else:
    print("[2/4] handle_brief signature already patched; skipping.")

# Replace the init-state preamble block with a conditional that branches on
# resumed_state. The anchor matches everything from `brief = parse_brief(...)`
# through `self._move_brief(brief_path)`. Whitespace tolerated via regex.
preamble_pattern = re.compile(
    r"(            brief = parse_brief\(brief_path\)\n"
    r"            validate_or_reject\(brief\)[^\n]*\n"
    r"(?:[^\n]*\n)*?"                                # comment lines (decision #9)
    r"            brief = resolve_context_paths\(brief, self\.config\.vault_path\)\n)"
    r"(\s*\n"
    r"            started_at = datetime\.now\(_UK\)\.isoformat\(timespec=\"seconds\"\)\n"
    r"            state = init_state\(\n"
    r"                brief, started_at, brief_path=str\(brief_path\),\n"
    r"                coder_mode=\"manual\",\n"
    r"            \)\n"
    r"            self\._state = state\n"
    r"\s*\n"
    r"            self\._open_run_log\(brief, started_at\)\n"
    r"            state = transition\(state, \"running\",\n"
    r"\s*run_log=str\(self\._run_log\)\)\n"
    r"            self\._state = state\n"
    r"            self\._log_event\(\"start\", f\"\{len\(brief\.steps\)\} steps; manual\n"
    r"\s*mode\"\)\n"
    r"\s*\n"
    r"            self\._move_brief\(brief_path\)\n)"
)

# Looser fallback pattern if exact spacing has drifted.
preamble_pattern_fallback = re.compile(
    r"(            brief = parse_brief\(brief_path\)\n.*?"
    r"resolve_context_paths\(brief, self\.config\.vault_path\)\n)"
    r"(.*?self\._move_brief\(brief_path\)\n)",
    re.DOTALL,
)

PREAMBLE_REPLACEMENT_HEAD = r"\1"
PREAMBLE_REPLACEMENT_BODY = (
    "\n"
    "            if resumed_state is not None:\n"
    "                # Decision #15 fix (Phase 2 Step 2): on resume, reuse the\n"
    "                # loaded state instead of clobbering it with init_state.\n"
    "                # _plan_step's reuse-guard depends on state.steps[i].plan\n"
    "                # being populated; init_state always sets plan=None and so\n"
    "                # silently invalidated the guard on the resume path before.\n"
    "                state = resumed_state\n"
    "                self._state = state\n"
    "                # Reopen the existing run log for append, if known.\n"
    "                if state.run_log:\n"
    "                    self._run_log = Path(state.run_log)\n"
    "                self._log_event(\n"
    "                    \"resume\", f\"resumed at step {state.current_step}\"\n"
    "                )\n"
    "                # The brief is already in active/ from the original run; do\n"
    "                # not re-move it. transition() back to \"running\" so the\n"
    "                # loop's status checks see a runnable state.\n"
    "                state = transition(state, \"running\", pending_action=None)\n"
    "                self._state = state\n"
    "            else:\n"
    "                started_at = datetime.now(_UK).isoformat(timespec=\"seconds\")\n"
    "                state = init_state(\n"
    "                    brief, started_at, brief_path=str(brief_path),\n"
    "                    coder_mode=\"manual\",\n"
    "                )\n"
    "                self._state = state\n"
    "\n"
    "                self._open_run_log(brief, started_at)\n"
    "                state = transition(state, \"running\",\n"
    "                                   run_log=str(self._run_log))\n"
    "                self._state = state\n"
    "                self._log_event(\n"
    "                    \"start\", f\"{len(brief.steps)} steps; manual mode\"\n"
    "                )\n"
    "\n"
    "                self._move_brief(brief_path)\n"
)

if "if resumed_state is not None:" in src:
    print("[3/4] resume-aware preamble already in place; skipping.")
else:
    m = preamble_pattern.search(src)
    if m:
        src = (
            src[:m.start()]
            + PREAMBLE_REPLACEMENT_HEAD.replace(r"\1", m.group(1))
            + PREAMBLE_REPLACEMENT_BODY
            + src[m.end():]
        )
        print("[3/4] resume-aware preamble inserted (exact-match pattern).")
    else:
        m = preamble_pattern_fallback.search(src)
        if not m:
            print(
                "error: could not find the init_state preamble block in "
                "handle_brief. The file has drifted from the Phase 1 shape; "
                "apply the patch manually using the README in this same patch "
                "directory.",
                file=sys.stderr,
            )
            sys.exit(4)
        src = (
            src[:m.start()]
            + m.group(1)
            + PREAMBLE_REPLACEMENT_BODY
            + src[m.end():]
        )
        print("[3/4] resume-aware preamble inserted (fallback pattern).")

# ---------------------------------------------------------------------------
# Edit 3: the step loop should skip already-done steps. The fix sits at the
# top of the loop body, before the status reset.
# ---------------------------------------------------------------------------
loop_anchor = (
    "            for idx, bstep in enumerate(brief.steps):\n"
    "                state.steps[idx].status = \"running\""
)
loop_new = (
    "            for idx, bstep in enumerate(brief.steps):\n"
    "                # Decision #15 fix (Phase 2 Step 2): skip steps already\n"
    "                # marked done from a prior session. Without this, resume\n"
    "                # re-executes completed steps with their persisted plans —\n"
    "                # which is worse than re-planning. The reuse-guard alone\n"
    "                # is not enough; we must not enter the step body at all.\n"
    "                if state.steps[idx].status == \"done\":\n"
    "                    continue\n"
    "                state.steps[idx].status = \"running\""
)
if "# Decision #15 fix (Phase 2 Step 2): skip steps already" in src:
    print("[3b/4] done-step skip already in place; skipping.")
elif loop_anchor in src:
    src = src.replace(loop_anchor, loop_new, 1)
    print("[3b/4] done-step skip inserted into the step loop.")
else:
    print(
        "warning: step loop anchor not matched exactly. The patch's done-step "
        "skip was NOT applied. Apply manually: at the top of the loop body "
        "(inside `for idx, bstep in enumerate(brief.steps):`), add the two-line "
        "guard `if state.steps[idx].status == \"done\": continue` BEFORE the "
        "existing `state.steps[idx].status = \"running\"` line.",
        file=sys.stderr,
    )

# ---------------------------------------------------------------------------
# Edit 4: thread resumed_state=st from resume() into handle_brief.
# ---------------------------------------------------------------------------
resume_anchor = "            return self.handle_brief(Path(st.brief_path))"
resume_new = (
    "            return self.handle_brief(Path(st.brief_path), resumed_state=st)"
)
if resume_anchor not in src and resume_new not in src:
    print(
        "error: could not find the resume() → handle_brief call site.",
        file=sys.stderr,
    )
    sys.exit(5)
if resume_anchor in src:
    src = src.replace(resume_anchor, resume_new, 1)
    print("[4/4] resume() now threads resumed_state through to handle_brief.")
else:
    print("[4/4] resume() already threaded; skipping.")

# ---------------------------------------------------------------------------
# Write back if changed.
# ---------------------------------------------------------------------------
if src == orig:
    print("\nno changes to write. file already at patched state.")
    sys.exit(0)

backup = ORCH.with_suffix(".py.pre-phase-2-step-2.bak")
backup.write_text(orig, encoding="utf-8")
ORCH.write_text(src, encoding="utf-8")
print(f"\nwrote {ORCH} (backup at {backup})")
print("verify with:")
print("  .venv/bin/python -m py_compile anvil/orchestrator.py")
print("  .venv/bin/python -m unittest discover tests/ -v")
