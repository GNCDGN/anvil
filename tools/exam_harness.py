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

    def poll(self, state: dict):
        self.last_state = state
        prev = self.prev or {}
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
    args = p.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
