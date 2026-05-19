#!/usr/bin/env python3
"""exam_harness.py — passive live-run evidence capture for ANVIL.

Phase 1 Step 9 mid-build addition (decision #20). This watches an ANVIL
run's state file and log, captures evidence per state transition, and
writes one ungraded exam-evidence markdown when the run reaches a
terminal status (or on SIGINT, or immediately under --once).

It does NOT drive ANVIL and it does NOT grade. Telegram remains Genco's
channel. The harness only reads files. The output mirrors the section
shape of builds/2026-05-18-anvil-phase-1/exam.md so a human grader can
slot it in without reorganising; every grading dimension is left as a
"Grader:" hook over captured evidence.

If the harness breaks mid-run the grader can recover from
state/current-run.json directly — nothing here is load-bearing for the
build itself.
"""

import argparse
import json
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- Pricing. Opus 4.7, USD per token, current as of 2026-05-18. Update
# here if Anthropic pricing changes; surface --rate-* flags if these go
# stale during a run. cache_creation has no spec-named rate — the 1.25x
# input convention is used and flagged. ---
RATE_INPUT = 15.0 / 1_000_000
RATE_OUTPUT = 75.0 / 1_000_000
RATE_CACHE_READ = 1.50 / 1_000_000
RATE_CACHE_CREATION = 18.75 / 1_000_000  # 1.25x input (convention; not spec-named)
BUDGET_USD = 20.0

TERMINAL = {"done", "failed", "aborted"}

DEFAULT_STATE = Path("~/Downloads/anvil/state/current-run.json").expanduser()
DEFAULT_LOG = Path("~/Downloads/anvil/anvil.log").expanduser()
DEFAULT_TARGET = Path("~/Downloads/vault-reporter").expanduser()

# The nine ANVIL Phase 1 grading dimensions, verbatim from
# builds/2026-05-18-anvil-phase-1/exam.md. Hardcoded, not templated off
# the vault file (that file is being superseded; depending on it would
# be fragile).
DIMENSIONS = [
    ("Planner Stage A — context selection quality",
     "raw plans + per-step table (which files Stage A selected vs what Stage B needed)"),
    ("Planner Stage B — plan executability",
     "raw plans + the manual-Coder deviations recorded against each (from the run log / Telegram, not captured here)"),
    ("Anti-confabulation discipline",
     "raw plans' escalation_triggers and confidence; escalations section"),
    ("Escalation trigger calibration",
     "escalations section + each plan's escalation_triggers vs what actually bit"),
    ("State persistence integrity",
     "per-step table (plan persisted? status transitions) + status-transition log"),
    ("Cost", "token-cost section against the $20 cap"),
    ("Test discipline",
     "out of scope for the live harness — grader uses the build's 96/96 record"),
    ("Manual-Coder workflow integrity",
     "status transitions, smoke results, state.commit vs git actual_commit divergence"),
    ("Surfaced defects",
     "decisions register + any divergences/notes captured below"),
]

_VOICE = (
    "Captured evidence only. The harness does not grade. Each dimension "
    "below is a hook for the human grader."
)

# Phase 2 grading dimensions, from
# builds/2026-05-18-anvil-phase-2/design.md Part 10. Additive to
# DIMENSIONS — the harness emits both sections; the grader uses
# whichever set matches the phase being graded.
PHASE2_DIMENSIONS = [
    ("Coder scope discipline",
     "coder_outputs per step — out_of_scope always empty"),
    ("Allow-list behaviour",
     "coder_outputs.allowed_tools per step; Layer 2 caught everything if Layer 1 leaked"),
    ("Path-prefix reconciliation correctness",
     "coder_outputs.reconciliations — triggered when expected, resolved cleanly"),
    ("Git introspection",
     "state.commit matches git log across all steps; never None for run steps"),
    ("Smoke test correctness",
     "orchestrator-run smokes correctly distinguish Coder success from smoke success"),
    ("Escalation grammar",
     "every escalation parsed; no paused-by-user from natural-language replies"),
    ("Resume re-plan fix",
     "across resume events in the build, zero avoidable Planner calls fire"),
    ("Cost",
     "Planner spend under $20; Coder cost tracked as duration"),
    ("Total Genco reply count",
     "under 20% of Phase 1 baseline (counted from run log reply events)"),
    ("Phase-1-retroactive",
     "Planner escalation calibration on Step 3 (judgment call); Step 4 conditional-skip discipline"),
]

PHASE3_DIMENSIONS = [
    ("Deploy chain correctness",
     "state.deploy.stage == 'complete'; vps_head_sha matches; service_status == 'active'"),
    ("Sub-stage escalation routing",
     "any deploy-{stage}-failed escalation names the right stage; unit-evidenced if no live failure"),
    ("E2E ordering",
     "Mac-resident e2e gates deploy; VPS-resident e2e runs post-deploy per design 2.7"),
    ("Service health verification",
     "post-restart systemctl is-active returns active within 3s settle; negative case unit-evidenced"),
    ("Phase-2-retroactive (P2-10 closure)",
     "Planner Step 1 threshold judgment: escalate or grounded plan (pass); confabulate (fail). "
     "Step 2 conditional: decline-when-unmet or grounded-when-met (pass); invent (fail)."),
    ("Total Genco reply count",
     "under 20% of Phase 1 baseline; same metric as Phase 2"),
]

# Phase 4 grading dimensions, per design Part 8. Five dimensions —
# artefact drafting + confirmation + writes are the substance Step 9
# exercises. The harness emits this section in render(); the grader
# uses it when the run reaches step 9.
PHASE4_DIMENSIONS = [
    ("Artefact-drafting correctness",
     "drafted setup_log_entry and checkpoint preserve brief facts and "
     "state.deploy without invention; cross-check raw plans + draft text"),
    ("Anti-confabulation under sparse-notes",
     "when brief step notes are empty, draft writes 'unclear from build "
     "context' rather than inventing rationale; positive-evidence only "
     "when sparse-notes step exists in the build"),
    ("Confirmation gate routing",
     "Telegram preview renders correctly with both artefacts and paths; "
     "go reply triggers writes; abort or non-go defers to manual; "
     "non-reserved replies route to paused-by-user"),
    ("Vault-write side effects",
     "after go, setup-log contains new entry at derived path; checkpoint "
     "exists at derived path with seven-field frontmatter (source: anvil); "
     "both files render in Obsidian without parse errors"),
    ("Idempotency under re-run",
     "re-running same brief after clean completion → step 9 detects existing "
     "checkpoint, skips writes with log line, no escalation"),
]

# Escalation-source bins for Phase 2 scoring. Matched against the
# `reason` field of escalation-shaped plans + run-log "escalation"
# events. Order matters: more specific patterns first.
_ESCALATION_BINS = {
    "planner-self": (
        "judgment-call", "scope-question", "missing-decision",
        "stage-a-missed-context", "planner escalation",
    ),
    "framework": (
        "planner-validation-failure", "smoke test failed",
        "coder-out-of-scope", "coder-path-reconciliation-failed",
        "coder-failed",
        # Phase 3 Step 6: deploy and e2e escalation reasons
        "deploy-config-missing", "deploy-push-failed", "deploy-pull-failed",
        "deploy-restart-failed", "deploy-health-check-failed",
        "deploy-e2e-failed", "e2e-failed", "e2e-script-not-found",
        # Phase 4 Step 6: vault-write escalation reasons
        "completion-artefacts-draft-failed",
        "checkpoint-write-failed", "vault-write-failed",
    ),
    "genco-initiated": (
        # paused-by-user via non-grammar reply; the run-log "pause"
        # event with a recorded reply text is the signal.
        "pause",
    ),
}


def _bin_escalation(reason: str) -> str:
    """Return the bin name for an escalation reason, or "other"."""
    if not reason:
        return "other"
    rlow = reason.lower()
    for bin_name, patterns in _ESCALATION_BINS.items():
        for p in patterns:
            if p in rlow:
                return bin_name
    return "other"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:
        return {"_harness_read_error": str(e)}


# [planner] token line, with the decision-#13 "%(asctime)s " prefix.
_PLANNER_RE = re.compile(
    r"\[planner\] step=(?P<step>\d+) stage=(?P<stage>\w+) "
    r"model=(?P<model>\S+) input_tokens=(?P<inp>\d+) "
    r"output_tokens=(?P<out>\d+) "
    r"cache_creation_input_tokens=(?P<cc>\d+) "
    r"cache_read_input_tokens=(?P<cr>\d+) "
    r"duration_s=(?P<dur>[\d.]+)"
)
_ANVIL_DECISION_RE = re.compile(r"\[ANVIL_DECISION\].*")


def _parse_planner_lines(log_path: Path) -> list[dict]:
    if not log_path.is_file():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8",
                                   errors="replace").splitlines():
        m = _PLANNER_RE.search(line)
        if not m:
            continue
        ts = line.split(" [planner]", 1)[0].strip()
        d = m.groupdict()
        inp, outp = int(d["inp"]), int(d["out"])
        cc, cr = int(d["cc"]), int(d["cr"])
        cost = (
            inp * RATE_INPUT + outp * RATE_OUTPUT
            + cc * RATE_CACHE_CREATION + cr * RATE_CACHE_READ
        )
        out.append({
            "ts": ts, "step": int(d["step"]), "stage": d["stage"],
            "model": d["model"], "input_tokens": inp, "output_tokens": outp,
            "cache_creation": cc, "cache_read": cr,
            "duration_s": float(d["dur"]), "cost_usd": cost,
        })
    return out


_VAULT_OPS_RE = re.compile(
    r"\[vault_ops\] (?P<kind>wrote|write failed|checkpoint exists, skipped) "
    r"(?P<path>\S+)(?:: (?P<error>.+))?"
)


def _parse_vault_ops_lines(log_path: Path) -> list[dict]:
    """Phase 4 Step 6: parse [vault_ops] log markers from anvil.log.

    Same shape as _parse_planner_lines. Returns one dict per event:
      {ts, kind: wrote|write failed|checkpoint exists, skipped, path, error}
    """
    if not log_path.is_file():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8",
                                    errors="replace").splitlines():
        m = _VAULT_OPS_RE.search(line)
        if not m:
            continue
        ts = line.split(" [vault_ops]", 1)[0].strip()
        d = m.groupdict()
        out.append({
            "ts": ts,
            "kind": d["kind"],
            "path": d["path"],
            "error": d.get("error"),
        })
    return out


def _decision_lines(log_path: Path) -> list[str]:
    if not log_path.is_file():
        return []
    return [
        m.group(0)
        for line in log_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines()
        for m in [_ANVIL_DECISION_RE.search(line)] if m
    ]


def _git_head(repo: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H", "-1"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else "(git read failed)"
    except Exception as e:
        return f"(git error: {e})"


class Capture:
    """Accumulates evidence across polls. Diffs each new state snapshot
    against the previous one and records transitions."""

    def __init__(self, target_repo: Path):
        self.target_repo = target_repo
        self.started_at = _now_iso()
        self.prev = None
        self.status_transitions: list[str] = []
        self.step_transitions: list[str] = []
        self.plans: dict[int, dict] = {}
        self.escalations: list[dict] = []
        self.smokes: dict[int, dict] = {}
        self.actual_commits: dict[int, str] = {}
        self.last_state = None
        # Phase 2 Step 4 additions: Coder output per step, path
        # reconciliations, reply-event count, escalation source bins.
        self.coder_outputs: dict[int, dict] = {}
        self.reconciliations: list[dict] = []
        self.reply_events: list[dict] = []
        self.escalation_bin_counts: dict[str, int] = {
            "planner-self": 0, "framework": 0,
            "genco-initiated": 0, "other": 0,
        }
        # Phase 3 Step 6: deploy-stage outcomes and post-deploy e2e
        self.deploy_outcomes: list[dict] = []
        self.e2e_outcomes: list[dict] = []
        self.vps_head_shas: list[str] = []
        # Phase 4 Step 6: vault-write outcomes from state +
        # [vault_ops] log markers (success, failure, idempotent skip).
        self.vault_writes_outcome: dict | None = None
        self.vault_ops_log_events: list[dict] = []

    def poll(self, state: dict):
        self.last_state = state
        prev = self.prev or {}
        # Phase 3 Step 6: capture deploy outcome when first observed.
        deploy = state.get("deploy")
        prev_deploy = prev.get("deploy")
        if deploy and deploy != prev_deploy:
            self.deploy_outcomes.append({
                "captured_at": _now_iso(),
                "stage": deploy.get("stage"),
                "ok": deploy.get("ok"),
                "vps_head_sha": deploy.get("vps_head_sha"),
                "service_status": deploy.get("service_status"),
                "output_truncated": (deploy.get("output", "")[:300] if deploy.get("output") else ""),
            })
            if deploy.get("vps_head_sha"):
                self.vps_head_shas.append(deploy["vps_head_sha"])
        # Phase 4 Step 6: capture vault_writes_outcome when first observed
        vwo = state.get("vault_writes_outcome")
        prev_vwo = prev.get("vault_writes_outcome")
        if vwo and vwo != prev_vwo:
            self.vault_writes_outcome = vwo
        if state.get("status") != prev.get("status"):
            self.status_transitions.append(
                f"{_now_iso()}  {prev.get('status', '(start)')} -> "
                f"{state.get('status')}"
            )
        pv = {s.get("n"): s for s in prev.get("steps", [])}
        for st in state.get("steps", []):
            n = st.get("n")
            old = pv.get(n, {})
            if st.get("status") != old.get("status"):
                self.step_transitions.append(
                    f"{_now_iso()}  step {n}: "
                    f"{old.get('status', '(none)')} -> {st.get('status')}"
                )
            plan = st.get("plan")
            if plan is not None and old.get("plan") is None:
                self.plans[n] = plan
                if isinstance(plan, dict) and plan.get("escalate") is True:
                    self.escalations.append({
                        "step": n,
                        "reason": plan.get("reason"),
                        "detail": plan.get("detail"),
                        "options": plan.get("options"),
                        "step_number": plan.get("step_number"),
                    })
            # Phase 2 Step 4: capture coder_output as it appears.
            co = st.get("coder_output")
            old_co = old.get("coder_output")
            if co is not None and old_co is None:
                # The Phase 2 Coder returns a dict; Phase 1 manual
                # mode leaves coder_output as None — both correct.
                if isinstance(co, dict):
                    self.coder_outputs[n] = co
                    for rec in co.get("reconciliations", []) or []:
                        self.reconciliations.append({"step": n, **rec})
                else:
                    # Legacy string shape — store as-is for the grader.
                    self.coder_outputs[n] = {"_raw": co}
            if st.get("smoke") is not None and old.get("smoke") is None:
                self.smokes[n] = {
                    "smoke": st.get("smoke"),
                    "smoke_output": st.get("smoke_output"),
                }
                # decision #14/17: state.commit stays null for manual
                # steps; record the target repo's HEAD separately so the
                # grader sees both.
                self.actual_commits[n] = _git_head(self.target_repo)
        self.prev = json.loads(json.dumps(state))  # deep snapshot

    def snapshot_commits(self):
        """For --once / terminal write: record git HEAD for every step
        that has a plan or smoke, labelled as HEAD-at-capture (not a
        per-step attribution — state.commit is the authoritative-but-null
        field; this is the best available cross-check)."""
        head = _git_head(self.target_repo)
        for st in (self.last_state or {}).get("steps", []):
            n = st.get("n")
            if n not in self.actual_commits and (
                st.get("plan") is not None or st.get("smoke") is not None
            ):
                self.actual_commits[n] = head


def _fence(obj) -> str:
    return "```json\n" + json.dumps(obj, indent=2, ensure_ascii=False) + "\n```"


def render(cap: Capture, planner_calls: list[dict],
           decisions: list[str], state_file: Path, log_file: Path,
           run_log: Path | None, reason: str) -> str:
    s = cap.last_state or {}
    run_name = Path(s.get("brief_path", "run")).stem or "run"
    L: list[str] = []
    L += [
        "---",
        "author: anvil-exam-harness",
        f"date: '{datetime.now().strftime('%Y-%m-%d')}'",
        "status: ungraded",
        "exam_type: phase-gate",
        "related_phase_or_version: anvil-phase-1",
        f"captured_at: '{_now_iso()}'",
        f"capture_reason: {reason}",
        "---",
        "",
        f"# ANVIL exam evidence — {run_name} (harness-captured, ungraded)",
        "",
        _VOICE,
        "",
        "## Capture metadata",
        "",
        f"- State file: {state_file}",
        f"- Log file: {log_file}",
        f"- Target repo: {cap.target_repo}",
        f"- Harness start: {cap.started_at}",
        f"- Capture end: {_now_iso()}  ({reason})",
        f"- Final status observed: {s.get('status')}",
        f"- Current step: {s.get('current_step')}",
        f"- Coder mode: {s.get('coder_mode')}",
        f"- Brief path: {s.get('brief_path')}",
        f"- Run log: {run_log if run_log else '(unset)'}",
        f"- schema_version: {s.get('schema_version')}",
        "",
        "## Per-step evidence",
        "",
        "| Step | Name | Status | Plan persisted | Escalation | Smoke | "
        "state.commit | git HEAD (cross-check) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for st in s.get("steps", []):
        n = st.get("n")
        esc = "yes" if any(e["step"] == n for e in cap.escalations) else "no"
        L.append(
            f"| {n} | {st.get('name')} | {st.get('status')} | "
            f"{'yes' if st.get('plan') is not None else 'no'} | {esc} | "
            f"{st.get('smoke') or '—'} | {st.get('commit') or '(none)'} | "
            f"{cap.actual_commits.get(n, '—')} |"
        )
    L += ["",
          "state.commit is null for manual-Coder steps by design "
          "(decision #14/17); the git HEAD column is the harness "
          "cross-check, recorded at smoke-pass or at capture, and is "
          "HEAD-at-capture, not a verified per-step attribution.",
          ""]

    L += ["## Status transitions", ""]
    L += [f"- {t}" for t in cap.status_transitions] or ["- (none observed)"]
    L += ["", "### Step status transitions", ""]
    L += [f"- {t}" for t in cap.step_transitions] or ["- (none observed)"]

    L += ["", "## Token cost", ""]
    if planner_calls:
        L += [
            "| ts | step | stage | in | out | cache_cr | cache_rd | "
            "dur s | cost USD |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        total = 0.0
        for c in planner_calls:
            total += c["cost_usd"]
            L.append(
                f"| {c['ts']} | {c['step']} | {c['stage']} | "
                f"{c['input_tokens']} | {c['output_tokens']} | "
                f"{c['cache_creation']} | {c['cache_read']} | "
                f"{c['duration_s']:.1f} | ${c['cost_usd']:.4f} |"
            )
        L += [
            "",
            f"Cumulative Planner cost: ${total:.4f}  "
            f"(budget ${BUDGET_USD:.0f}, remaining ${BUDGET_USD - total:.2f})",
            f"Calls captured: {len(planner_calls)}. Rates: input "
            f"$15/M, output $75/M, cache-read $1.50/M, cache-creation "
            f"$18.75/M (1.25x input, convention — not spec-named).",
        ]
    else:
        L.append("No [planner] token lines found in the log. If the run "
                 "has made Planner calls, the log handler (decision #13) "
                 "may not have been wired for this invocation.")

    L += ["", "## Raw plans", ""]
    if cap.plans:
        for n in sorted(cap.plans):
            L += [f"### Step {n}", "", _fence(cap.plans[n]), ""]
    else:
        L.append("(no plans persisted yet)")

    L += ["", "## Escalations", ""]
    if cap.escalations:
        for e in cap.escalations:
            L += [
                f"### Step {e['step']} — {e.get('reason')}",
                "",
                f"- reason: {e.get('reason')}",
                f"- detail: {e.get('detail')}",
                f"- options: {e.get('options')}",
                f"- step_number: {e.get('step_number')}",
                "",
            ]
    else:
        L.append("(no escalation-shaped plans captured)")

    L += ["", "## Run log", ""]
    if run_log and Path(run_log).is_file():
        L += ["```", Path(run_log).read_text(encoding="utf-8",
                                             errors="replace").rstrip(), "```"]
    else:
        L.append(f"(run log not found at {run_log})")

    L += ["", "## Grading dimensions (ungraded — grader plugs in here)", ""]
    for i, (name, feeds) in enumerate(DIMENSIONS, 1):
        L += [
            f"### Dimension {i} — {name}",
            "",
            f"Grader: assess against rubric dimension {i} ({name}). "
            f"Evidence: {feeds}.",
            "",
        ]


    # --- Phase 2 Step 4 additions below ---
    # Coder outputs section.
    L += ["", "## Coder outputs", ""]
    if cap.coder_outputs:
        L += [
            "| Step | exit | files | out_of_scope | duration_s | "
            "allow-list / deny-list |",
            "|---|---|---|---|---|---|",
        ]
        for n in sorted(cap.coder_outputs):
            co = cap.coder_outputs[n]
            if "_raw" in co:
                L.append(
                    f"| {n} | (manual) | — | — | — | — |"
                )
                continue
            files = co.get("files_touched", []) or []
            oos = co.get("out_of_scope", []) or []
            dur = co.get("duration_s")
            dur_s = f"{dur:.1f}" if isinstance(dur, (int, float)) else "—"
            tools = co.get("allowed_tools") or co.get("disallowed_tools") or "—"
            L.append(
                f"| {n} | {co.get('exit_code', '—')} | "
                f"{len(files)} ({', '.join(files) or '—'}) | "
                f"{len(oos)} ({', '.join(oos) or '—'}) | "
                f"{dur_s} | {tools} |"
            )
    else:
        L.append("(no coder_output captured — Phase 1 manual-mode runs leave this empty)")

    # Path reconciliations.
    L += ["", "### Path reconciliations", ""]
    if cap.reconciliations:
        L += [
            "| Step | original | resolved | status | reason |",
            "|---|---|---|---|---|",
        ]
        for rec in cap.reconciliations:
            L.append(
                f"| {rec.get('step')} | {rec.get('original', '—')} | "
                f"{rec.get('resolved') or '—'} | "
                f"{rec.get('status', '—')} | {rec.get('reason', '—')} |"
            )
    else:
        L.append("(no path reconciliations recorded)")

    # Escalation rate — bin every captured escalation by source.
    L += ["", "## Escalation rate (Phase 2 metric)", ""]
    bin_counts = {"planner-self": 0, "framework": 0,
                  "genco-initiated": 0, "other": 0}
    for e in cap.escalations:
        bin_counts[_bin_escalation(e.get("reason", ""))] += 1
    total_escalations = sum(bin_counts.values())
    L += [
        f"- planner-self-emitted: {bin_counts['planner-self']}",
        f"- framework-emitted: {bin_counts['framework']}",
        f"- Genco-initiated (non-grammar reply): {bin_counts['genco-initiated']}",
        f"- other: {bin_counts['other']}",
        f"- **total**: {total_escalations}",
    ]

    # Total reply count — counted from run log "coder(manual) reply=",
    # "pause reply=", and escalation→user-decision events. The harness
    # does not see Telegram directly; the run log is the proxy.
    L += ["", "## Total Genco reply count (proxy via run log)", ""]
    reply_count = 0
    if run_log and Path(run_log).is_file():
        try:
            run_log_text = Path(run_log).read_text(
                encoding="utf-8", errors="replace"
            )
            for line in run_log_text.splitlines():
                if "coder(manual)" in line and "reply=" in line:
                    reply_count += 1
                elif "**pause**" in line and "reply=" in line:
                    reply_count += 1
                elif "step-done" in line:
                    # An explicit-confirm step-done is the "go" reply.
                    reply_count += 1
        except Exception as e:
            L.append(f"(run-log read error: {e})")
    L += [
        f"- replies counted from run log: {reply_count}",
        "- Phase 1 manual-Coder baseline is documented in the Phase 1 setup-log entry; "
        "Phase 2 target is < 20% of that baseline.",
    ]

    # Phase 2 grading dimensions.
    L += ["", "## Phase 2 grading dimensions (ungraded — grader plugs in here)", ""]
    # Phase 3 Step 6: Deploy verification section (only when deploy ran)
    if cap.deploy_outcomes:
        lines.append("## Deploy verification (Phase 3)")
        for d in cap.deploy_outcomes:
            lines.append(
                f"- {d['captured_at']}: stage={d['stage']} ok={d['ok']} "
                f"sha={d.get('vps_head_sha') or '-'} status={d.get('service_status') or '-'}"
            )
            if d.get("output_truncated"):
                lines.append(f"  output: {d['output_truncated']!r}")
        lines.append("")

    for i, (name, feeds) in enumerate(PHASE2_DIMENSIONS, 1):
        L += [
            f"### Phase 2 dimension {i} — {name}",
            "",
            f"Grader: assess against Phase 2 rubric dimension {i} ({name}). "
            f"Evidence: {feeds}.",
            "",
        ]
    # --- end Phase 2 Step 4 additions ---

    # --- Phase 4 Step 6 additions ---
    # Vault writes section. Emits when either vault_writes_outcome was
    # captured from state, or [vault_ops] log markers were observed.
    if cap.vault_writes_outcome or cap.vault_ops_log_events:
        L += ["", "## Vault writes (Phase 4)", ""]
        if cap.vault_writes_outcome:
            vwo = cap.vault_writes_outcome
            L += [
                f"- outcome: {'ok' if vwo.get('ok') else 'failed'}",
                f"- setup-log: {vwo.get('setup_log_path', '-')}",
                f"- checkpoint: {vwo.get('checkpoint_path', '-')}",
            ]
            if vwo.get("error"):
                L.append(f"- error: {vwo['error']}")
            L.append("")
        if cap.vault_ops_log_events:
            L += ["### vault_ops log events", ""]
            for ev in cap.vault_ops_log_events:
                err = f" — {ev['error']}" if ev.get("error") else ""
                L.append(f"- {ev['ts']}: {ev['kind']} {ev['path']}{err}")
            L.append("")

    # Phase 4 grading dimensions. Emitted alongside Phase 2/3 dimensions;
    # grader uses whichever matches the phase being graded.
    L += ["", "## Phase 4 grading dimensions (ungraded — grader plugs in here)", ""]
    for i, (name, feeds) in enumerate(PHASE4_DIMENSIONS, 1):
        L += [
            f"### Phase 4 dimension {i} — {name}",
            "",
            f"Grader: assess against Phase 4 rubric dimension {i} ({name}). "
            f"Evidence: {feeds}.",
            "",
        ]
    # --- end Phase 4 Step 6 additions ---

    L += ["## Decisions register", ""]
    if decisions:
        L += [f"- {d}" for d in decisions]
    else:
        L.append("(no [ANVIL_DECISION] lines in the log — the human "
                 "grader fills this from the tracked decisions register)")
    L.append("")
    return "\n".join(L)


def _default_out(state: dict | None) -> Path:
    runs = Path("~/Downloads/anvil/state/runs").expanduser()
    name = "run"
    if state:
        name = Path(state.get("brief_path", "run")).stem or "run"
        rl = state.get("run_log")
        if rl:
            name = Path(rl).stem
    return runs / f"{name}-exam.md"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="exam_harness",
        description="Passive live-run evidence capture for an ANVIL run. "
                    "Watches the state file, writes one ungraded exam "
                    "markdown at a terminal status, on SIGINT, or under "
                    "--once. Does not drive ANVIL and does not grade.",
    )
    p.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    p.add_argument("--log-file", type=Path, default=DEFAULT_LOG)
    p.add_argument("--target-repo", type=Path, default=DEFAULT_TARGET)
    p.add_argument("--out", type=Path, default=None,
                   help="output markdown; default "
                        "state/runs/<run>-exam.md")
    p.add_argument("--once", action="store_true",
                   help="capture the current state once, write, exit "
                        "(no watch loop) — deterministic snapshot")
    p.add_argument("--interval", type=float, default=2.0,
                   help="poll seconds (default 2)")
    p.add_argument("--self-check", action="store_true",
                   help="run against tools/fixtures/probe-state.json "
                        "and assert the report contains all dimension "
                        "sections; exit 0 on pass, non-zero on fail")
    args = p.parse_args(argv)

    if args.self_check:
        return _self_check()

    state_file = args.state_file.expanduser()
    log_file = args.log_file.expanduser()
    target_repo = args.target_repo.expanduser()

    state = _read_state(state_file)
    if state is None:
        print(f"exam_harness: no state file at {state_file}", file=sys.stderr)
        return 1

    out = (args.out.expanduser() if args.out else _default_out(state))
    out.parent.mkdir(parents=True, exist_ok=True)

    def write(reason: str):
        cap.snapshot_commits()
        rl = (cap.last_state or {}).get("run_log")
        rlp = Path(rl).expanduser() if rl else None
        # Phase 4 Step 6: parse [vault_ops] markers and surface to cap
        cap.vault_ops_log_events = _parse_vault_ops_lines(log_file)
        doc = render(
            cap, _parse_planner_lines(log_file), _decision_lines(log_file),
            state_file, log_file, rlp, reason,
        )
        out.write_text(doc, encoding="utf-8")
        print(f"exam_harness: wrote {out} ({reason})")

    cap = Capture(target_repo)
    cap.poll(state)

    if args.once:
        write("once")
        return 0

    if state.get("status") == "done":
        print("exam_harness: status is done at startup — nothing to "
              "observe")
        return 0

    interrupted = {"v": False}

    def _sig(_signum, _frame):
        interrupted["v"] = True
    signal.signal(signal.SIGINT, _sig)

    print(f"exam_harness: watching {state_file} every "
          f"{args.interval}s; out -> {out}")
    try:
        while True:
            time.sleep(args.interval)
            if interrupted["v"]:
                write("sigint")
                return 0
            s = _read_state(state_file)
            if s is None or "_harness_read_error" in (s or {}):
                continue  # transient (atomic rename window); retry
            cap.poll(s)
            if s.get("status") in TERMINAL:
                write(f"terminal:{s.get('status')}")
                return 0
    except KeyboardInterrupt:
        write("sigint")
        return 0



def _self_check() -> int:
    """Self-check: run the harness against a known fixture state and
    assert the report contains every dimension section. Creates the
    fixture at tools/fixtures/probe-state.json if missing. Stdlib-only.
    Returns 0 on pass, 1 on fail."""
    import tempfile
    fixtures_dir = Path(__file__).resolve().parent / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    # Phase 4 Step 6: prefer the Phase 4 fixture if present —
    # exercises the Vault writes section. Falls back to probe-state.json
    # for backward compatibility.
    fixture_p4 = fixtures_dir / "probe-phase4-state.json"
    fixture = fixture_p4 if fixture_p4.is_file() else (fixtures_dir / "probe-state.json")
    if not fixture.is_file():
        fixture.write_text(json.dumps({
            "schema_version": 2,
            "brief_path": "/tmp/self-check-brief.md",
            "started_at": "2026-05-18T00:00:00",
            "status": "done",
            "current_step": 1,
            "coder_mode": "auto",
            "run_log": None,
            "steps": [
                {
                    "n": 1, "name": "fixture step",
                    "status": "done",
                    "commit": "deadbeef",
                    "smoke": "pass",
                    "smoke_output": "ok",
                    "plan": {"step_number": 1, "step_name": "fixture",
                             "approach": "do it", "confidence": "high",
                             "escalation_triggers": []},
                    "coder_output": {
                        "exit_code": 0, "stdout": "done", "stderr": "",
                        "files_touched": ["a.py"], "out_of_scope": [],
                        "reconciliations": [], "duration_s": 12.3,
                        "allowed_tools": ["Edit"],
                    },
                },
            ],
        }, indent=2), encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "self-check-exam.md"
        rc = main([
            "--state-file", str(fixture),
            "--log-file", "/tmp/nonexistent-log",
            "--target-repo", str(Path(__file__).resolve().parent.parent),
            "--out", str(out),
            "--once",
        ])
        if rc != 0 or not out.is_file():
            print(f"self-check: harness exited {rc} or no output", file=sys.stderr)
            return 1
        report = out.read_text(encoding="utf-8")

    expected_sections = [
        "## Per-step evidence",
        "## Status transitions",
        "## Token cost",
        "## Raw plans",
        "## Escalations",
        "## Coder outputs",
        "### Path reconciliations",
        "## Escalation rate (Phase 2 metric)",
        "## Total Genco reply count",
        "## Phase 2 grading dimensions",
        # Phase 4 Step 6: new section asserted when using
        # probe-phase4-state.json fixture.
        "## Phase 4 grading dimensions",
        "## Decisions register",
    ]
    missing = [s for s in expected_sections if s not in report]
    if missing:
        print(f"self-check: missing sections {missing}", file=sys.stderr)
        return 1

    # Verify the Coder fixture rendered into the table.
    # Phase 4 Step 6: Coder fixture assertion only applies when the
    # probe-state.json fixture is in use (which carries the deadbeef
    # coder_output). The Phase 4 fixture exercises vault writes; its
    # check is the presence of the Vault writes section instead.
    if "probe-phase4-state.json" not in str(fixture):
        if "deadbeef" not in report or "12.3" not in report:
            print("self-check: Coder fixture did not render", file=sys.stderr)
            return 1
    else:
        if "## Vault writes (Phase 4)" not in report:
            print("self-check: Vault writes section missing", file=sys.stderr)
            return 1

    print("self-check: ok — all dimension sections present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
