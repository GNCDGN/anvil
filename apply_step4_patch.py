#!/usr/bin/env python3
"""Phase 2 Step 4 — evolve tools/exam_harness.py for Coder evidence,
escalation-rate scoring, total reply count, Phase 2 grading dimensions,
and a --self-check flag.

Idempotent. Wraps semantically-clean insertions around the existing
Phase 1 harness rather than rewriting it. Specifically:

  1. Adds PHASE2_DIMENSIONS const after the existing DIMENSIONS.
  2. Extends Capture with new fields: coder_outputs, reply_events,
     escalation_sources, reconciliations.
  3. Adds Capture.poll() handling for coder_output, status transitions
     that count as reply events, escalation source binning.
  4. Adds render() sections: "Coder outputs", "Escalation rate",
     "Total reply count", "Phase 2 grading dimensions".
  5. Adds --self-check flag to main() that exercises against
     tools/fixtures/probe-state.json (created at first run if absent).

The Phase 1 dimensions and existing render output are preserved — the
harness is additive across phases, not replacing.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "tools" / "exam_harness.py"

if not HARNESS.is_file():
    print(f"error: {HARNESS} not found. Run from ~/Downloads/anvil/.",
          file=sys.stderr)
    sys.exit(1)

src = HARNESS.read_text(encoding="utf-8")
orig = src

MARKER = "# --- Phase 2 Step 4 additions below ---"


# ---------------------------------------------------------------------------
# Edit 1: PHASE2_DIMENSIONS const + escalation-source classification helper.
# Inserted right after the existing DIMENSIONS list closes.
# ---------------------------------------------------------------------------
anchor_after_dims = (
    '_VOICE = (\n'
    '    "Captured evidence only. The harness does not grade. Each dimension "\n'
    '    "below is a hook for the human grader."\n'
    ')\n'
)

phase2_dims_block = (
    '\n'
    '# Phase 2 grading dimensions, from\n'
    '# builds/2026-05-18-anvil-phase-2/design.md Part 10. Additive to\n'
    '# DIMENSIONS — the harness emits both sections; the grader uses\n'
    '# whichever set matches the phase being graded.\n'
    'PHASE2_DIMENSIONS = [\n'
    '    ("Coder scope discipline",\n'
    '     "coder_outputs per step — out_of_scope always empty"),\n'
    '    ("Allow-list behaviour",\n'
    '     "coder_outputs.allowed_tools per step; Layer 2 caught everything if Layer 1 leaked"),\n'
    '    ("Path-prefix reconciliation correctness",\n'
    '     "coder_outputs.reconciliations — triggered when expected, resolved cleanly"),\n'
    '    ("Git introspection",\n'
    '     "state.commit matches git log across all steps; never None for run steps"),\n'
    '    ("Smoke test correctness",\n'
    '     "orchestrator-run smokes correctly distinguish Coder success from smoke success"),\n'
    '    ("Escalation grammar",\n'
    '     "every escalation parsed; no paused-by-user from natural-language replies"),\n'
    '    ("Resume re-plan fix",\n'
    '     "across resume events in the build, zero avoidable Planner calls fire"),\n'
    '    ("Cost",\n'
    '     "Planner spend under $20; Coder cost tracked as duration"),\n'
    '    ("Total Genco reply count",\n'
    '     "under 20% of Phase 1 baseline (counted from run log reply events)"),\n'
    '    ("Phase-1-retroactive",\n'
    '     "Planner escalation calibration on Step 3 (judgment call); Step 4 conditional-skip discipline"),\n'
    ']\n'
    '\n'
    '# Escalation-source bins for Phase 2 scoring. Matched against the\n'
    '# `reason` field of escalation-shaped plans + run-log "escalation"\n'
    '# events. Order matters: more specific patterns first.\n'
    '_ESCALATION_BINS = {\n'
    '    "planner-self": (\n'
    '        "judgment-call", "scope-question", "missing-decision",\n'
    '        "stage-a-missed-context", "planner escalation",\n'
    '    ),\n'
    '    "framework": (\n'
    '        "planner-validation-failure", "smoke test failed",\n'
    '        "coder-out-of-scope", "coder-path-reconciliation-failed",\n'
    '        "coder-failed",\n'
    '    ),\n'
    '    "genco-initiated": (\n'
    '        # paused-by-user via non-grammar reply; the run-log "pause"\n'
    '        # event with a recorded reply text is the signal.\n'
    '        "pause",\n'
    '    ),\n'
    '}\n'
    '\n'
    '\n'
    'def _bin_escalation(reason: str) -> str:\n'
    '    """Return the bin name for an escalation reason, or "other"."""\n'
    '    if not reason:\n'
    '        return "other"\n'
    '    rlow = reason.lower()\n'
    '    for bin_name, patterns in _ESCALATION_BINS.items():\n'
    '        for p in patterns:\n'
    '            if p in rlow:\n'
    '                return bin_name\n'
    '    return "other"\n'
)

if "PHASE2_DIMENSIONS = [" in src:
    print("[1/5] PHASE2_DIMENSIONS already in place; skipping.")
elif anchor_after_dims in src:
    src = src.replace(
        anchor_after_dims, anchor_after_dims + phase2_dims_block, 1,
    )
    print("[1/5] PHASE2_DIMENSIONS + escalation bin helper added.")
else:
    print("error: could not find _VOICE anchor for inserting PHASE2_DIMENSIONS.",
          file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Edit 2: extend Capture.__init__ to track Phase 2 evidence.
# ---------------------------------------------------------------------------
cap_init_old = (
    '    def __init__(self, target_repo: Path):\n'
    '        self.target_repo = target_repo\n'
    '        self.started_at = _now_iso()\n'
    '        self.prev = None\n'
    '        self.status_transitions: list[str] = []\n'
    '        self.step_transitions: list[str] = []\n'
    '        self.plans: dict[int, dict] = {}\n'
    '        self.escalations: list[dict] = []\n'
    '        self.smokes: dict[int, dict] = {}\n'
    '        self.actual_commits: dict[int, str] = {}\n'
    '        self.last_state = None\n'
)

cap_init_new = (
    '    def __init__(self, target_repo: Path):\n'
    '        self.target_repo = target_repo\n'
    '        self.started_at = _now_iso()\n'
    '        self.prev = None\n'
    '        self.status_transitions: list[str] = []\n'
    '        self.step_transitions: list[str] = []\n'
    '        self.plans: dict[int, dict] = {}\n'
    '        self.escalations: list[dict] = []\n'
    '        self.smokes: dict[int, dict] = {}\n'
    '        self.actual_commits: dict[int, str] = {}\n'
    '        self.last_state = None\n'
    '        # Phase 2 Step 4 additions: Coder output per step, path\n'
    '        # reconciliations, reply-event count, escalation source bins.\n'
    '        self.coder_outputs: dict[int, dict] = {}\n'
    '        self.reconciliations: list[dict] = []\n'
    '        self.reply_events: list[dict] = []\n'
    '        self.escalation_bin_counts: dict[str, int] = {\n'
    '            "planner-self": 0, "framework": 0,\n'
    '            "genco-initiated": 0, "other": 0,\n'
    '        }\n'
)

if "self.coder_outputs:" in src:
    print("[2/5] Capture.__init__ already extended; skipping.")
elif cap_init_old in src:
    src = src.replace(cap_init_old, cap_init_new, 1)
    print("[2/5] Capture.__init__ extended with Phase 2 fields.")
else:
    print(
        "error: could not find Capture.__init__ anchor. The harness may "
        "have drifted from Phase 1 shape; review by hand.",
        file=sys.stderr,
    )
    sys.exit(3)


# ---------------------------------------------------------------------------
# Edit 3: extend Capture.poll() — capture coder_output transitions and
# bin escalations as they appear.
# ---------------------------------------------------------------------------
poll_anchor = (
    '            if st.get("smoke") is not None and old.get("smoke") is None:\n'
)
poll_insert = (
    '            # Phase 2 Step 4: capture coder_output as it appears.\n'
    '            co = st.get("coder_output")\n'
    '            old_co = old.get("coder_output")\n'
    '            if co is not None and old_co is None:\n'
    '                # The Phase 2 Coder returns a dict; Phase 1 manual\n'
    '                # mode leaves coder_output as None — both correct.\n'
    '                if isinstance(co, dict):\n'
    '                    self.coder_outputs[n] = co\n'
    '                    for rec in co.get("reconciliations", []) or []:\n'
    '                        self.reconciliations.append({"step": n, **rec})\n'
    '                else:\n'
    '                    # Legacy string shape — store as-is for the grader.\n'
    '                    self.coder_outputs[n] = {"_raw": co}\n'
)

if "# Phase 2 Step 4: capture coder_output" in src:
    print("[3/5] Capture.poll() already extended; skipping.")
elif poll_anchor in src:
    src = src.replace(poll_anchor, poll_insert + poll_anchor, 1)
    print("[3/5] Capture.poll() captures coder_output and reconciliations.")
else:
    print(
        "warning: poll() anchor not matched — coder_output capture NOT added. "
        "Apply by hand: insert the new block in Capture.poll() just BEFORE the "
        "smoke-transition handler, after the plan-transition handler.",
        file=sys.stderr,
    )


# After poll(): bin every escalation that gets recorded. The cleanest spot
# is right where escalations.append happens — but we already inserted into
# poll. Instead we'll bin escalations at render time from the captured
# list, which is simpler and keeps poll() small. No further poll edit.


# ---------------------------------------------------------------------------
# Edit 4: render() additions — Coder outputs section, Escalation rate,
# Total reply count, Phase 2 dimensions. Inserted before the closing
# "## Decisions register" section.
# ---------------------------------------------------------------------------
render_anchor = '    L += ["## Decisions register", ""]\n'

# Build the new sections as a multi-line string the patch inserts before
# the decisions-register block.
render_insert = (
    '\n'
    '    # --- Phase 2 Step 4 additions below ---\n'
    '    # Coder outputs section.\n'
    '    L += ["", "## Coder outputs", ""]\n'
    '    if cap.coder_outputs:\n'
    '        L += [\n'
    '            "| Step | exit | files | out_of_scope | duration_s | "\n'
    '            "allow-list / deny-list |",\n'
    '            "|---|---|---|---|---|---|",\n'
    '        ]\n'
    '        for n in sorted(cap.coder_outputs):\n'
    '            co = cap.coder_outputs[n]\n'
    '            if "_raw" in co:\n'
    '                L.append(\n'
    '                    f"| {n} | (manual) | — | — | — | — |"\n'
    '                )\n'
    '                continue\n'
    '            files = co.get("files_touched", []) or []\n'
    '            oos = co.get("out_of_scope", []) or []\n'
    '            dur = co.get("duration_s")\n'
    '            dur_s = f"{dur:.1f}" if isinstance(dur, (int, float)) else "—"\n'
    '            tools = co.get("allowed_tools") or co.get("disallowed_tools") or "—"\n'
    '            L.append(\n'
    '                f"| {n} | {co.get(\'exit_code\', \'—\')} | "\n'
    '                f"{len(files)} ({\', \'.join(files) or \'—\'}) | "\n'
    '                f"{len(oos)} ({\', \'.join(oos) or \'—\'}) | "\n'
    '                f"{dur_s} | {tools} |"\n'
    '            )\n'
    '    else:\n'
    '        L.append("(no coder_output captured — Phase 1 manual-mode runs leave this empty)")\n'
    '\n'
    '    # Path reconciliations.\n'
    '    L += ["", "### Path reconciliations", ""]\n'
    '    if cap.reconciliations:\n'
    '        L += [\n'
    '            "| Step | original | resolved | status | reason |",\n'
    '            "|---|---|---|---|---|",\n'
    '        ]\n'
    '        for rec in cap.reconciliations:\n'
    '            L.append(\n'
    '                f"| {rec.get(\'step\')} | {rec.get(\'original\', \'—\')} | "\n'
    '                f"{rec.get(\'resolved\') or \'—\'} | "\n'
    '                f"{rec.get(\'status\', \'—\')} | {rec.get(\'reason\', \'—\')} |"\n'
    '            )\n'
    '    else:\n'
    '        L.append("(no path reconciliations recorded)")\n'
    '\n'
    '    # Escalation rate — bin every captured escalation by source.\n'
    '    L += ["", "## Escalation rate (Phase 2 metric)", ""]\n'
    '    bin_counts = {"planner-self": 0, "framework": 0,\n'
    '                  "genco-initiated": 0, "other": 0}\n'
    '    for e in cap.escalations:\n'
    '        bin_counts[_bin_escalation(e.get("reason", ""))] += 1\n'
    '    total_escalations = sum(bin_counts.values())\n'
    '    L += [\n'
    '        f"- planner-self-emitted: {bin_counts[\'planner-self\']}",\n'
    '        f"- framework-emitted: {bin_counts[\'framework\']}",\n'
    '        f"- Genco-initiated (non-grammar reply): {bin_counts[\'genco-initiated\']}",\n'
    '        f"- other: {bin_counts[\'other\']}",\n'
    '        f"- **total**: {total_escalations}",\n'
    '    ]\n'
    '\n'
    '    # Total reply count — counted from run log "coder(manual) reply=",\n'
    '    # "pause reply=", and escalation→user-decision events. The harness\n'
    '    # does not see Telegram directly; the run log is the proxy.\n'
    '    L += ["", "## Total Genco reply count (proxy via run log)", ""]\n'
    '    reply_count = 0\n'
    '    if run_log and Path(run_log).is_file():\n'
    '        try:\n'
    '            run_log_text = Path(run_log).read_text(\n'
    '                encoding="utf-8", errors="replace"\n'
    '            )\n'
    '            for line in run_log_text.splitlines():\n'
    '                if "coder(manual)" in line and "reply=" in line:\n'
    '                    reply_count += 1\n'
    '                elif "**pause**" in line and "reply=" in line:\n'
    '                    reply_count += 1\n'
    '                elif "step-done" in line:\n'
    '                    # An explicit-confirm step-done is the "go" reply.\n'
    '                    reply_count += 1\n'
    '        except Exception as e:\n'
    '            L.append(f"(run-log read error: {e})")\n'
    '    L += [\n'
    '        f"- replies counted from run log: {reply_count}",\n'
    '        "- Phase 1 manual-Coder baseline is documented in the Phase 1 setup-log entry; "\n'
    '        "Phase 2 target is < 20% of that baseline.",\n'
    '    ]\n'
    '\n'
    '    # Phase 2 grading dimensions.\n'
    '    L += ["", "## Phase 2 grading dimensions (ungraded — grader plugs in here)", ""]\n'
    '    for i, (name, feeds) in enumerate(PHASE2_DIMENSIONS, 1):\n'
    '        L += [\n'
    '            f"### Phase 2 dimension {i} — {name}",\n'
    '            "",\n'
    '            f"Grader: assess against Phase 2 rubric dimension {i} ({name}). "\n'
    '            f"Evidence: {feeds}.",\n'
    '            "",\n'
    '        ]\n'
    '    # --- end Phase 2 Step 4 additions ---\n'
    '\n'
)

if "Phase 2 Step 4 additions below" in src:
    print("[4/5] render() Phase 2 sections already in place; skipping.")
elif render_anchor in src:
    src = src.replace(render_anchor, render_insert + render_anchor, 1)
    print("[4/5] render() now emits Coder outputs, reconciliations, escalation rate, reply count, Phase 2 dimensions.")
else:
    print(
        "error: could not find render() anchor for inserting Phase 2 sections.",
        file=sys.stderr,
    )
    sys.exit(4)


# ---------------------------------------------------------------------------
# Edit 5: add --self-check flag to main() that runs against a fixture.
# ---------------------------------------------------------------------------
selfcheck_anchor = (
    '    p.add_argument("--interval", type=float, default=2.0,\n'
    '                   help="poll seconds (default 2)")\n'
    '    args = p.parse_args(argv)\n'
)

selfcheck_insert = (
    '    p.add_argument("--interval", type=float, default=2.0,\n'
    '                   help="poll seconds (default 2)")\n'
    '    p.add_argument("--self-check", action="store_true",\n'
    '                   help="run against tools/fixtures/probe-state.json "\n'
    '                        "and assert the report contains all dimension "\n'
    '                        "sections; exit 0 on pass, non-zero on fail")\n'
    '    args = p.parse_args(argv)\n'
    '\n'
    '    if args.self_check:\n'
    '        return _self_check()\n'
)

if "args.self_check" in src:
    print("[5/5] --self-check flag already wired; skipping.")
elif selfcheck_anchor in src:
    src = src.replace(selfcheck_anchor, selfcheck_insert, 1)
    print("[5/5] --self-check flag added to main().")
else:
    print(
        "error: could not find main() args anchor for --self-check.",
        file=sys.stderr,
    )
    sys.exit(5)


# Append the _self_check function before `if __name__ == "__main__":`.
selfcheck_fn_anchor = 'if __name__ == "__main__":\n'

selfcheck_fn_block = (
    '\n'
    'def _self_check() -> int:\n'
    '    """Self-check: run the harness against a known fixture state and\n'
    '    assert the report contains every dimension section. Creates the\n'
    '    fixture at tools/fixtures/probe-state.json if missing. Stdlib-only.\n'
    '    Returns 0 on pass, 1 on fail."""\n'
    '    import tempfile\n'
    '    fixtures_dir = Path(__file__).resolve().parent / "fixtures"\n'
    '    fixtures_dir.mkdir(parents=True, exist_ok=True)\n'
    '    fixture = fixtures_dir / "probe-state.json"\n'
    '    if not fixture.is_file():\n'
    '        fixture.write_text(json.dumps({\n'
    '            "schema_version": 2,\n'
    '            "brief_path": "/tmp/self-check-brief.md",\n'
    '            "started_at": "2026-05-18T00:00:00",\n'
    '            "status": "done",\n'
    '            "current_step": 1,\n'
    '            "coder_mode": "auto",\n'
    '            "run_log": None,\n'
    '            "steps": [\n'
    '                {\n'
    '                    "n": 1, "name": "fixture step",\n'
    '                    "status": "done",\n'
    '                    "commit": "deadbeef",\n'
    '                    "smoke": "pass",\n'
    '                    "smoke_output": "ok",\n'
    '                    "plan": {"step_number": 1, "step_name": "fixture",\n'
    '                             "approach": "do it", "confidence": "high",\n'
    '                             "escalation_triggers": []},\n'
    '                    "coder_output": {\n'
    '                        "exit_code": 0, "stdout": "done", "stderr": "",\n'
    '                        "files_touched": ["a.py"], "out_of_scope": [],\n'
    '                        "reconciliations": [], "duration_s": 12.3,\n'
    '                        "allowed_tools": ["Edit"],\n'
    '                    },\n'
    '                },\n'
    '            ],\n'
    '        }, indent=2), encoding="utf-8")\n'
    '\n'
    '    with tempfile.TemporaryDirectory() as td:\n'
    '        out = Path(td) / "self-check-exam.md"\n'
    '        rc = main([\n'
    '            "--state-file", str(fixture),\n'
    '            "--log-file", "/tmp/nonexistent-log",\n'
    '            "--target-repo", str(Path(__file__).resolve().parent.parent),\n'
    '            "--out", str(out),\n'
    '            "--once",\n'
    '        ])\n'
    '        if rc != 0 or not out.is_file():\n'
    '            print(f"self-check: harness exited {rc} or no output", file=sys.stderr)\n'
    '            return 1\n'
    '        report = out.read_text(encoding="utf-8")\n'
    '\n'
    '    expected_sections = [\n'
    '        "## Per-step evidence",\n'
    '        "## Status transitions",\n'
    '        "## Token cost",\n'
    '        "## Raw plans",\n'
    '        "## Escalations",\n'
    '        "## Coder outputs",\n'
    '        "### Path reconciliations",\n'
    '        "## Escalation rate (Phase 2 metric)",\n'
    '        "## Total Genco reply count",\n'
    '        "## Phase 2 grading dimensions",\n'
    '        "## Decisions register",\n'
    '    ]\n'
    '    missing = [s for s in expected_sections if s not in report]\n'
    '    if missing:\n'
    '        print(f"self-check: missing sections {missing}", file=sys.stderr)\n'
    '        return 1\n'
    '\n'
    '    # Verify the Coder fixture rendered into the table.\n'
    '    if "deadbeef" not in report or "12.3" not in report:\n'
    '        print("self-check: Coder fixture did not render", file=sys.stderr)\n'
    '        return 1\n'
    '\n'
    '    print("self-check: ok — all dimension sections present")\n'
    '    return 0\n'
    '\n'
    '\n'
)

if "def _self_check()" in src:
    print("[5b/5] _self_check function already defined; skipping.")
elif selfcheck_fn_anchor in src:
    src = src.replace(
        selfcheck_fn_anchor,
        selfcheck_fn_block + selfcheck_fn_anchor,
        1,
    )
    print("[5b/5] _self_check function added.")
else:
    print(
        "error: could not find __main__ anchor for inserting _self_check.",
        file=sys.stderr,
    )
    sys.exit(6)


# ---------------------------------------------------------------------------
# Write back if changed.
# ---------------------------------------------------------------------------
if src == orig:
    print("\nno changes to write. file already at patched state.")
    sys.exit(0)

backup = HARNESS.with_suffix(".py.pre-phase-2-step-4.bak")
backup.write_text(orig, encoding="utf-8")
HARNESS.write_text(src, encoding="utf-8")
print(f"\nwrote {HARNESS} (backup at {backup})")
print("verify with:")
print("  .venv/bin/python -m py_compile tools/exam_harness.py")
print("  .venv/bin/python tools/exam_harness.py --self-check")
print("  .venv/bin/python -m unittest discover tests/ -v")
