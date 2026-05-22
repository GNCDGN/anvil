"""v2 Phase 1 Step 6 — calibration sweep runner.

Orchestration script that runs the five calibration tasks (T1–T5) in
both `mock` and `real` modes, ingests each run into the harness's
DuckDB, and produces a final cost+verdict report.

Brief location (in the vault):
  <VAULT>/01-Projects/code-workspace/anvil/builds/
    2026-05-20-anvil-v2-phase-1-calibration/<task>-<label>/brief.md

CLI:
  python tools/calibration_runner.py [--tasks T1,T2,...] [--modes mock,real]
                                     [--dry-run] [--budget-cap 30.00]

Behaviour:
  - For each (task, mode) pair: clear inbox/, bootstrap target_repo_path,
    copy brief to inbox/, set env vars, run anvil, write mode.txt,
    ingest into DuckDB.
  - Real mode only: pre-check cumulative spend against budget_cap.
  - `--dry-run`: print the plan, assert every brief parses + validates,
    do NOT run any subprocess.

Hardcoded estimate table per notes.md Finding 7 (1.3× margin):
  T1: $1.25  T2: $2.50  T3: $2.50  T4: $1.25  T5: $3.75

The brief→inbox copy, env-dict build, subprocess invocation, mode.txt
write, and harness ingest are each isolated functions so tests can
exercise them without firing the whole sweep.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow `python tools/calibration_runner.py` direct invocation by adding
# the repo root (parent of `tools/`) to sys.path. Tests already run via
# `unittest discover -s tests` from the repo root and don't need this.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Lazy-import to avoid pulling DuckDB into --dry-run / parse-only paths.
# `harness_v2.open_db` is called from `cumulative_real_spend` and `ingest_run`.


DEFAULT_TASKS = ("T1", "T2", "T3", "T4", "T5", "T6")
DEFAULT_MODES = ("mock", "real")

# Task → vault-folder slug. The vault folder is named `<task>-<label>`;
# this map keeps both in one place. The label is also the second hyphen-
# separated segment of the run_id (so the harness's task_id regex sees
# "T1" as task_id and "doc-edit" as task_label).
#
# T0 added v2 Phase 1 Step 7 option 3: verbose-brief baseline whose
# purpose is to give the Planner unambiguous signal for every required
# Plan field so a validation-passing plan reaches the Coder. Used to
# measure the dashboard-vs-DuckDB Coder-subprocess multiplier. Not in
# DEFAULT_TASKS — must be invoked explicitly via --tasks T0.
TASK_LABELS = {
    "T0": "baseline",
    "T1": "doc-edit",
    "T2": "two-step",
    "T3": "out-of-scope",
    "T4": "judgment-escalation",
    "T5": "deploy",
    # v2 Phase 2 Step 4 follow-up: write-new calibration. Exercises the
    # _reconcile_paths fall-through fix (V2P2-4) — a step that creates a
    # strictly-new file must reach the Coder, not escalate at preflight.
    "T6": "write-new",
}

# Per-task auto-reply for AUTO_REPLY_FOR_CALIBRATION:
#   T0, T1, T2, T5: explicit confirms / escalations → "go" (proceed)
#   T3, T4:         trap / judgment-escalation       → "abort"
AUTO_REPLIES = {
    "T0": "go",
    "T1": "go",
    "T2": "go",
    "T3": "abort",
    "T4": "abort",
    "T5": "go",
    # T6 write-new: a clean create, proceed.
    "T6": "go",
}

# Pre-estimated real-mode cost per task (USD, 1.3× safety margin applied
# per notes.md Finding 7). Used for the budget pre-check.
ESTIMATES_USD = {
    "T0": 1.50,  # 1 Stage A + 1 Stage B + 1 Coder; verbose brief slightly
                 # bigger Stage B prompt than T1's $1.25.
    "T1": 1.25,
    "T2": 2.50,
    "T3": 2.50,
    "T4": 1.25,
    "T5": 3.75,
    "T6": 1.25,  # 1 Stage A + 1 Stage B + 1 Coder; T1-sized single-step.
}

ANVIL_REPO = Path(__file__).resolve().parent.parent


# Per-task placeholder seed map. v2 Phase 1 Step 7 triage: `anvil/coder.py`
# `_reconcile_paths` requires every `plan.files_to_touch` entry to exist
# in the target repo at preflight time (or have a single-basename match).
# For calibration tasks that "create" new files the preflight escalates
# before the Coder ever runs — invalidating the framework-cost
# measurement. Bootstrap seeds these placeholders so the Coder sees
# every planned path as an existing file it modifies. T1's baseline
# README.md is the only file the original bootstrap created; this
# extends the pattern to every task's scope.
#
# Per task: list of (relative_path, content) tuples.
SEED_FILES: dict[str, tuple[tuple[str, str], ...]] = {
    "T0": (
        ("README.md", "# T0 baseline target\n\n"),
    ),
    "T1": (
        ("README.md", "# T1 calibration target\n"),
    ),
    "T2": (
        ("a.py", "# a.py placeholder\n"),
        ("b.py", "# b.py placeholder\n"),
    ),
    "T3": (
        ("a.py", "# a.py placeholder\n"),
        # b.py NOT seeded — the trap requires the Coder to create it
        # out-of-scope during step 2 so Layer 2 git-diff catches it.
        # Per-task carve-out: T3 is the one task whose trap behaviour
        # depends on a planned file (b.py) being created mid-run rather
        # than pre-seeded.
    ),
    "T4": (
        ("retention.py", "# retention.py placeholder\n"),
    ),
    "T5": (
        ("version.txt", "v1\n"),
        ("CHANGELOG.md", "# CHANGELOG\n"),
        ("README.md", "# T5 v1 deploy target\n"),
    ),
    # T6 deliberately does NOT seed its planned file (anvil/utils/hello.py)
    # — the whole point is to exercise the write-new fall-through, so the
    # planned path must be ABSENT at preflight. Seed only a baseline
    # README so the bootstrap's initial commit has content (HEAD must
    # resolve for Layer 2 git-diff). This is the inverse of T1–T5, which
    # seed their planned files as placeholders.
    "T6": (
        ("README.md", "# T6 write-new target\n"),
    ),
}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def vault_path() -> Path:
    """Resolve VAULT_PATH from env, default `~/vaults/second-brain`."""
    return Path(
        os.environ.get("VAULT_PATH", "~/vaults/second-brain")
    ).expanduser()


def calibration_root() -> Path:
    """Vault root for the five calibration briefs."""
    return (
        vault_path()
        / "01-Projects" / "code-workspace" / "anvil" / "builds"
        / "2026-05-20-anvil-v2-phase-1-calibration"
    )


def brief_path_for(task: str) -> Path:
    label = TASK_LABELS[task]
    return calibration_root() / f"{task}-{label}" / "brief.md"


def target_repo_path_for(task: str) -> Path:
    # v2 Phase 5 Step 1b (Finding 6): renamed v2-phase-1/targets → calibration/targets.
    # The throwaway target repos are phase-agnostic (T6 was added in v2 Phase 2);
    # the v2-phase-1 prefix was misleading. bootstrap_target_repo recreates the
    # dirs at the new path on the next sweep — the old state/v2-phase-1/targets/
    # dirs are transient (untracked) and can be deleted at leisure.
    return ANVIL_REPO / "state" / "calibration" / "targets" / task


def run_id_for(task: str, mode: str) -> str:
    """v2 Phase 2 Step 1: run_id carries the mode segment so mock and
    real ingests do not share a run_id. Pairs with harness_v2's
    composite (run_id, mode) idempotency key — the run_id itself is
    mode-distinguishing, the composite key is the defensive second
    line. `mode` must be one of `mock`, `real`, or `unknown`."""
    return f"{task}-{TASK_LABELS[task]}-{mode}"


def run_dir_for(task: str, mode: str) -> Path:
    """v2 Phase 2 Step 1: run-dir name carries the mode segment, so
    `shutil.rmtree(run_dir_for(task, mode))` between mock and real for
    the same task only wipes that mode's events.jsonl — the sibling
    mode's data survives on disk."""
    return ANVIL_REPO / "state" / "runs" / run_id_for(task, mode)


# ---------------------------------------------------------------------------
# Target repo bootstrap
# ---------------------------------------------------------------------------

def bootstrap_target_repo(task: str) -> Path:
    """Wipe + re-seed `state/calibration/targets/<task>/` to the
    task-specific baseline. Idempotent in the sense that two calls
    yield the same baseline; NOT idempotent in the sense that local
    changes are preserved — the target repo is calibration-owned, so
    each call is a fresh-baseline reset.

    Why wipe rather than incrementally seed: re-runs (mock-then-real,
    or repeated mock for debugging) would otherwise pick up residue
    from a prior Coder invocation (e.g. b.py left over from T3's
    out-of-scope trap firing). A wipe + re-seed guarantees deterministic
    `_reconcile_paths` + `_git_files_touched` results regardless of
    run history.

    v2 Phase 1 Step 7 triage: seeds files declared in SEED_FILES so
    `_reconcile_paths` (anvil/coder.py:115) finds every planned path
    as an existing file. See the SEED_FILES docstring + v2 Phase 1
    notes.md Step 7 outcome for the v1 design defect being worked
    around (preflight gates write-new on existence).
    """
    p = target_repo_path_for(task)
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)

    # git init + identity.
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(p), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "calibration@anvil.local"],
        cwd=str(p), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "anvil-calibration"],
        cwd=str(p), check=True, capture_output=True,
    )

    # Seed task-specific baseline files.
    seeds = SEED_FILES.get(task, (("README.md", f"# {task} baseline\n"),))
    for rel, content in seeds:
        full = p / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    # Initial commit so HEAD resolves (Layer 2 git-diff needs HEAD).
    subprocess.run(
        ["git", "add", "-A"], cwd=str(p), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", f"{task} baseline"],
        cwd=str(p), check=True, capture_output=True,
    )

    # v2 Phase 1 Step 7 prep: surface GPG-signing / strict-identity hook
    # conflicts BEFORE the sweep launches. `git commit --dry-run` is
    # buggy with `--allow-empty` (returns exit 1 with "nothing to
    # commit"), so the check makes a REAL empty commit and then
    # resets HEAD~1 if it succeeds. Net repo state unchanged.
    # If GPG signing is required and the env can't satisfy it, the
    # commit fails — we surface a RuntimeError with a clear remediation
    # hint, better than silent failure mid-sweep.
    check = subprocess.run(
        ["git", "-C", str(p), "commit", "--allow-empty",
         "-m", "calibration bootstrap dry-run"],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        raise RuntimeError(
            f"Target repo bootstrap blocked by git config at {p}: "
            f"{(check.stderr or check.stdout).strip()}. "
            f"Likely cause: GPG-signing requirement or strict identity hook. "
            f"Either disable signing for this repo "
            f"(git -C {p} config commit.gpgsign false) or unset the "
            f"global hook for the duration of the sweep."
        )
    # Undo the test commit so the repo is exactly as we left it.
    subprocess.run(
        ["git", "-C", str(p), "reset", "--hard", "HEAD~1"],
        check=True, capture_output=True,
    )

    return p


# ---------------------------------------------------------------------------
# Env construction
# ---------------------------------------------------------------------------

def build_env(task: str, mode: str) -> dict[str, str]:
    """Build the env dict for one calibration run.

    Mirrors `os.environ` so the subprocess inherits credentials, then
    overlays the calibration-specific flags. NEVER mutates `os.environ`
    in the parent process — the dict is passed explicitly to
    subprocess.run.
    """
    env = dict(os.environ)
    is_mock = (mode == "mock")
    env["MOCKED_PLANNER"] = "1" if is_mock else "0"
    env["MOCKED_CODER"] = "1" if is_mock else "0"
    # MOCKED_TASK_ID is read by MockedPlanner/MockedCoder when mock-mode
    # is on; harmless when off. Keep set unconditionally for clarity.
    env["MOCKED_TASK_ID"] = task
    env["ANVIL_RUN_ID_OVERRIDE"] = run_id_for(task, mode)
    env["AUTO_REPLY_FOR_CALIBRATION"] = AUTO_REPLIES[task]
    env["CALIBRATION_TELEGRAM_PREFIX"] = "[ANVIL-calibration]"
    # CODER_MODE must be auto for the orchestrator to invoke the Coder.
    env["CODER_MODE"] = "auto"
    # Jitter at zero for deterministic profiling. The calibration_runner
    # could dial these up in the future for human-comparison; default 0.
    env.setdefault("MOCKED_PLANNER_JITTER_MS", "0")
    env.setdefault("MOCKED_CODER_JITTER_MS", "0")
    return env


# ---------------------------------------------------------------------------
# Brief copy
# ---------------------------------------------------------------------------

def stage_brief_into_inbox(task: str) -> Path:
    """Copy the vault brief into `inbox/<task>-<label>.md` so
    `anvil run` (which scans `inbox/*.md`) picks it up. Clears
    `inbox/*.md` first to guarantee single-brief execution.
    """
    inbox = ANVIL_REPO / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for stale in inbox.glob("*.md"):
        stale.unlink()
    src = brief_path_for(task)
    if not src.is_file():
        raise FileNotFoundError(f"calibration brief not found: {src}")
    dst = inbox / f"{task}-{TASK_LABELS[task]}.md"
    shutil.copy2(src, dst)
    return dst


def clear_current_run_state() -> None:
    """Remove `state/current-run.json` so anvil starts a fresh run."""
    for f in (ANVIL_REPO / "state" / "current-run.json",
              ANVIL_REPO / "state" / "current-run.md"):
        if f.is_file():
            f.unlink()


# ---------------------------------------------------------------------------
# Run + ingest
# ---------------------------------------------------------------------------

def run_one(task: str, mode: str, env: dict[str, str]) -> dict:
    """Execute one (task, mode) run. Returns a result dict.

    Steps:
      1. clear_current_run_state
      2. clear the prior run-dir for this task (events.jsonl appends
         in events.py; without this, re-runs accumulate)
      3. stage_brief_into_inbox
      4. bootstrap_target_repo
      5. subprocess: `anvil run` with the calibration env
      6. write `mode.txt` into the run-dir (creating the dir if absent)
      7. (caller separately calls harness ingest)
    """
    clear_current_run_state()
    # Clear any prior events.jsonl for this run_id — events.py appends
    # in O(1) mode, so a re-run of the same task would otherwise mix
    # the new events into the prior file.
    # v2 Phase 2 Step 1: the run-dir is mode-suffixed, so this rmtree
    # only wipes the current mode's dir — the sibling mode's data
    # (e.g. a prior mock run, when this is the real run) survives on
    # disk and remains ingestable.
    prior = run_dir_for(task, mode)
    if prior.is_dir():
        shutil.rmtree(prior, ignore_errors=True)
    stage_brief_into_inbox(task)
    bootstrap_target_repo(task)

    cmd = [
        sys.executable, "-m", "anvil.cli", "run",
    ]
    proc = subprocess.run(
        cmd, cwd=str(ANVIL_REPO), env=env,
        capture_output=True, text=True,
        timeout=int(env.get("CALIBRATION_RUN_TIMEOUT_S", "600")),
    )

    # mode.txt — written even if the run dir is missing (subprocess
    # crashed before any emit), so harness ingest is robust.
    rd = run_dir_for(task, mode)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "mode.txt").write_text(mode + "\n", encoding="utf-8")

    return {
        "task": task,
        "mode": mode,
        "exit_code": proc.returncode,
        "stdout_chars": len(proc.stdout or ""),
        "stderr_chars": len(proc.stderr or ""),
        "stderr_tail": (proc.stderr or "")[-500:],
        "run_dir": str(rd),
    }


def ingest_run(task: str, mode: str) -> dict:
    """Ingest the run-dir into the harness DuckDB. Returns the harness's
    per-run summary dict.

    v2 Phase 2 Step 1: takes `mode` so the per-mode run-dir is targeted
    and the per_run_summary lookup is keyed on the mode-suffixed run_id.
    """
    from tools import harness_v2
    con = harness_v2.open_db(_db_path_for_v2_phase_2())
    try:
        summary = harness_v2.ingest(con, run_dir_for(task, mode))
        # Pull per_run_summary row.
        per_run = harness_v2.query_per_run_summary(
            con, run_id=run_id_for(task, mode),
        )
        summary["per_run"] = per_run[0] if per_run else None
        return summary
    finally:
        con.close()


def cumulative_real_spend() -> float:
    """Sum `total_cost_usd` across `mode='real'` runs in the harness DB."""
    from tools import harness_v2
    con = harness_v2.open_db(_db_path_for_v2_phase_2())
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(total_cost_usd), 0.0) FROM per_run_summary "
            "WHERE mode = 'real'"
        ).fetchone()
        return float(row[0] or 0.0)
    finally:
        con.close()


# v2 Phase 2 Step 1: DuckDB path is settable via a module-level override
# and a CLI flag. Default is `state/v2-phase-2/calibration.duckdb` for
# the v2 Phase 2 sweep — distinct from v2 Phase 1's path so the prior
# calibration data is untouched. Set via `--db-path` or by assigning
# `calibration_runner._DB_PATH_OVERRIDE` directly in tests.
_DB_PATH_OVERRIDE: Path | None = None


def _db_path_for_v2_phase_2() -> Path:
    if _DB_PATH_OVERRIDE is not None:
        return _DB_PATH_OVERRIDE
    return ANVIL_REPO / "state" / "v2-phase-2" / "calibration.duckdb"


# ---------------------------------------------------------------------------
# Budget log
# ---------------------------------------------------------------------------

def budget_log_path() -> Path:
    # v2 Phase 2 Step 1: budget log moves alongside the v2 Phase 2
    # DuckDB so a single state/v2-phase-2/ dir holds the whole sweep's
    # artefacts (DuckDB, budget log, future XLSX export).
    return ANVIL_REPO / "state" / "v2-phase-2" / "budget-log.md"


def append_budget_log(line: str) -> None:
    p = budget_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {line}\n")


# ---------------------------------------------------------------------------
# Top-level sweep
# ---------------------------------------------------------------------------

def parse_brief_only(task: str) -> tuple[bool, str]:
    """Parse + validate one brief. Returns (ok, error_message).

    Used in --dry-run mode to assert every brief is shippable before
    any expensive real-mode runs.
    """
    from anvil.brief import parse_brief, validate_or_reject
    src = brief_path_for(task)
    if not src.is_file():
        return (False, f"brief not found: {src}")
    # Bootstrap target repo so rule 3 (target_repo_path exists + git repo)
    # has something to chew on.
    bootstrap_target_repo(task)
    try:
        brief = parse_brief(src)
        validate_or_reject(brief, vault_root=vault_path())
        return (True, "")
    except Exception as e:  # noqa: BLE001
        return (False, f"{type(e).__name__}: {e}")


def _print_pre_sweep_warning(
    tasks: tuple[str, ...], modes: tuple[str, ...], budget_cap: float,
) -> None:
    """v2 Phase 1 Step 7 prep: pre-flight notice before a live sweep.

    Five-second pause gives Genco time to ctrl-C if the terminal is in
    the wrong directory, env vars are missing, etc. Dry-run skips this.
    """
    print("=" * 70)
    print("CALIBRATION SWEEP — pre-flight notice")
    print("=" * 70)
    print()
    print("Real Telegram messages will land in your chat during this sweep,")
    print("prefixed [ANVIL-calibration]. You do NOT need to reply — the")
    print("AUTO_REPLY_FOR_CALIBRATION env flag short-circuits all wait-for-")
    print("reply sites in the orchestrator.")
    print()
    print("During the sweep, watch:")
    print("  - The terminal output (per-run summary lines + auto-reply telemetry)")
    print("  - The Anthropic billing dashboard (real-mode spend)")
    print()
    print("The Telegram chat can be ignored.")
    print()
    print(f"Tasks: {', '.join(tasks)}  Modes: {', '.join(modes)}")
    print(f"In-app budget cap: ${budget_cap:.2f}")
    print("Provider hard cap (Anthropic dashboard): set manually by user")
    print()
    print("Press Ctrl-C to abort. Sweep begins in 5 seconds...")
    print("=" * 70)
    time.sleep(5)


def sweep(
    tasks: tuple[str, ...] = DEFAULT_TASKS,
    modes: tuple[str, ...] = DEFAULT_MODES,
    *,
    dry_run: bool = False,
    budget_cap: float = 30.00,
) -> int:
    plan: list[tuple[str, str]] = [(t, m) for t in tasks for m in modes]
    if dry_run:
        print("--- calibration_runner --dry-run ---")
        all_ok = True
        for task, mode in plan:
            env = build_env(task, mode)
            label = f"{task}-{TASK_LABELS[task]}"
            est = ESTIMATES_USD[task] if mode == "real" else 0.0
            print(f"  {label:30s} mode={mode:5s} est_usd=${est:.2f}")
            print(f"    env: MOCKED_PLANNER={env['MOCKED_PLANNER']} "
                  f"MOCKED_CODER={env['MOCKED_CODER']} "
                  f"MOCKED_TASK_ID={env['MOCKED_TASK_ID']} "
                  f"ANVIL_RUN_ID_OVERRIDE={env['ANVIL_RUN_ID_OVERRIDE']} "
                  f"AUTO_REPLY={env['AUTO_REPLY_FOR_CALIBRATION']}")
            ok, err = parse_brief_only(task)
            if not ok:
                print(f"    BRIEF PARSE FAILED: {err}")
                all_ok = False
            else:
                print(f"    brief parses + validates: OK")
        print(f"--- dry-run {'PASS' if all_ok else 'FAIL'} ---")
        return 0 if all_ok else 1

    # Real sweep — print the pre-flight notice and pause briefly.
    _print_pre_sweep_warning(tasks, modes, budget_cap)

    results: list[dict] = []
    aborted: list[tuple[str, str, str]] = []
    for task, mode in plan:
        if mode == "real":
            spend = cumulative_real_spend()
            est = ESTIMATES_USD[task]
            if spend + est > budget_cap:
                msg = (
                    f"budget cap would be exceeded: cumulative=${spend:.2f} + "
                    f"estimate=${est:.2f} > cap=${budget_cap:.2f}; "
                    f"aborting {task}-real"
                )
                append_budget_log(msg)
                print(msg)
                aborted.append((task, mode, "budget"))
                continue
        env = build_env(task, mode)
        print(f"[calibration_runner] starting {task} ({mode})")
        result = run_one(task, mode, env)
        # Surface [calibration] telemetry from the subprocess's stderr.
        for line in (result.get("stderr_tail") or "").splitlines():
            if "[calibration]" in line:
                print(f"  {line}")
        print(f"  exit={result['exit_code']} "
              f"stderr_chars={result['stderr_chars']}")
        try:
            ingest = ingest_run(task, mode)
            result["ingest"] = ingest
            per = ingest.get("per_run")
            if per:
                print(f"  cost=${per[4] or 0:.2f} "
                      f"duration_s={per[5] or 0:.1f} "
                      f"escalations={per[8]}")
        except Exception as e:  # noqa: BLE001
            result["ingest_error"] = str(e)
            print(f"  ingest FAILED: {e}")
        results.append(result)

    # Summary
    total_real = sum(
        (r.get("ingest", {}).get("per_run") or [None] * 11)[4] or 0.0
        for r in results if r["mode"] == "real"
    )
    print("\n--- calibration sweep summary ---")
    print(f"runs completed: {len(results)}")
    print(f"runs aborted (budget): {len(aborted)}")
    print(f"total real-mode spend: ${total_real:.2f}")
    print(f"budget cap: ${budget_cap:.2f}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibration_runner",
        description="ANVIL v2 Phase 1 calibration sweep.",
    )
    parser.add_argument("--tasks", default=",".join(DEFAULT_TASKS),
                        help="comma-separated task IDs (default: T1,T2,T3,T4,T5)")
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES),
                        help="comma-separated modes (default: mock,real)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, validate briefs, do not execute")
    parser.add_argument("--budget-cap", type=float, default=30.00,
                        help="hard ceiling on cumulative real-mode spend (USD)")
    parser.add_argument(
        "--db-path", default=None,
        help=(
            "DuckDB path for ingest + budget queries. Default: "
            "<ANVIL_REPO>/state/v2-phase-2/calibration.duckdb"
        ),
    )
    args = parser.parse_args(argv)

    # v2 Phase 2 Step 1: --db-path overrides the default sweep DuckDB
    # path. Threaded through module-level `_DB_PATH_OVERRIDE` so
    # `ingest_run` and `cumulative_real_spend` (called from sweep) both
    # pick it up without further plumbing.
    global _DB_PATH_OVERRIDE
    if args.db_path:
        _DB_PATH_OVERRIDE = Path(args.db_path).expanduser().resolve()

    tasks = tuple(t.strip() for t in args.tasks.split(",") if t.strip())
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    for t in tasks:
        if t not in TASK_LABELS:
            print(f"unknown task: {t}; known: {sorted(TASK_LABELS.keys())}")
            return 2
    for m in modes:
        if m not in DEFAULT_MODES:
            print(f"unknown mode: {m}; known: {DEFAULT_MODES}")
            return 2

    return sweep(tasks, modes, dry_run=args.dry_run, budget_cap=args.budget_cap)


if __name__ == "__main__":
    sys.exit(main())
