"""Coder agent — Claude Code subprocess wrapper (implementation-notes Component 4).

Phase 2 Step 8 ships the real Coder, replacing the Phase 0/1
`coder_mode == "manual"` fallback. The Coder wraps `claude --print`
with scope discipline at two layers:

  Layer 1 — deny-list construction. The Step 1 probe finding
  (decision P2-1, soft prompt for Edit) means --allowedTools is not a
  hard gate. We invert: from the plan's operations, compute a
  *deny-set* of tool names that are NOT implied and pass via
  --disallowedTools. Combined with --tools "" when operations is
  empty. Layer 1 is best-effort first-line; Layer 2 carries correctness.

  Layer 2 — post-hoc git diff verification. After claude --print exits,
  run `git diff --name-only HEAD` + `git ls-files --others
  --exclude-standard` against target_repo_path, compute the set of
  files touched, and flag any outside plan.files_to_touch as
  out_of_scope. The orchestrator escalates on a non-empty out_of_scope
  list. This is the load-bearing correctness layer.

Path reconciliation (decision #18 layer 3): before invoking the
subprocess, for each path in plan.files_to_touch check existence at
target_repo_path / path. If a path doesn't exist, walk the repo for a
single-basename-match using the same heuristic as brief._basename_match
(but kept independent here so coder.py doesn't import from brief). If
exactly one match exists, record the reconciliation and use the
resolved path in the prompt. If zero or multiple matches, return an
escalation block (coder-path-reconciliation-failed).

The Coder's output contract is a single dict with eight keys
(design Part 9):
  stdout, stderr, exit_code, files_touched, out_of_scope,
  reconciliations, duration_s, allowed_tools.

The `ok` boolean is derived by consumers as
`exit_code == 0 and not out_of_scope`. Not stored on the dict.

Never raises. On subprocess timeout the dict carries exit_code=-1 and
a stderr line describing the timeout. The orchestrator escalates on
non-zero exit through the existing smoke-fail-adjacent path.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from anvil import events as _events

log = logging.getLogger("anvil.coder")

# Tools the model uses to read or search the repo. Always permitted —
# without these the model can't even look at files to plan its edits.
_READ_TOOLS = ("Read", "Glob", "Grep")

# Tools that mutate the filesystem. Permitted only when the plan
# declares `write`. Specifically denied otherwise because the Step 1
# probe found --allowedTools is a soft prompt for Edit and Write — the
# deny-list is what actually enforces.
_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# Shell. Permitted only when `shell` is in operations. Smoke-test and
# commit are explicitly NOT mapped to Bash — the orchestrator owns
# both per design Part 3 / Q2 / Q6.
_SHELL_TOOLS = ("Bash",)

# Excluded from repo walks during path reconciliation.
_WALK_EXCLUDED = frozenset(
    {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
)


def _basename_match(repo: Path, target: str) -> str | None:
    """Walk `repo` for a single file whose basename matches `target`'s.
    Deterministic (sorted walk). Returns the repo-relative path of the
    single match, or None if zero or multiple matches exist.

    Independent of brief._basename_match by design — coder.py should
    not import from brief.py since their semantics may drift in later
    phases; the algorithm is small enough to duplicate.
    """
    base = Path(target).name
    hits: list[str] = []
    try:
        for p in sorted(repo.rglob(base)):
            if any(seg in _WALK_EXCLUDED for seg in p.parts):
                continue
            if not p.is_file():
                continue
            try:
                hits.append(str(p.relative_to(repo)))
            except ValueError:
                continue
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def _reconcile_paths(
    plan_files: list[str], repo: Path, operations: list[str]
) -> tuple[list[str], list[dict]]:
    """For each path in plan_files, check existence at repo/path. If
    missing, look for a single-basename match. Returns (resolved_paths,
    reconciliations) where resolved_paths is the path list as the Coder
    will use (originals where existing, resolved where reconciled), and
    reconciliations is the structured record per design Part 4 layer 3.

    Reconciliation dict shape:
      {original: str, resolved: str | None,
       status: 'resolved' | 'new-file' | 'failed', reason: str}

    v2 Phase 2 Step 4 (V2P2-4): write-new fall-through. A path that
    resolves to no existing file AND has no single-basename match used
    to be a uniform 'failed' → preflight escalation. That made it
    impossible to plan a step that creates a new file. Now, when the
    plan declares the `write` operation, an unresolved path is recorded
    as 'new-file' rather than 'failed': the Coder subprocess will
    create it. Without `write` in operations, the prior 'failed'
    behaviour holds (an unresolved read/edit target is still a real
    error worth escalating).
    """
    resolved: list[str] = []
    reconciliations: list[dict] = []
    can_write = "write" in (operations or [])
    for p in plan_files:
        if (repo / p).exists():
            resolved.append(p)
            continue
        match = _basename_match(repo, p)
        if match is None:
            if can_write:
                # Write-new carve-out: the step is allowed to create
                # files, so an unresolved path is a new file, not a
                # reconciliation failure. The Coder creates it.
                reconciliations.append({
                    "original": p,
                    "resolved": None,
                    "status": "new-file",
                    "reason": "write operation declared; treating as new file",
                })
            else:
                reconciliations.append({
                    "original": p,
                    "resolved": None,
                    "status": "failed",
                    "reason": "no single basename match in repo",
                })
            # Either way, the original path is what the Coder uses
            # (create-new or surface-the-failure); the caller's failure
            # filter decides which reconciliation statuses escalate.
            resolved.append(p)
        else:
            reconciliations.append({
                "original": p,
                "resolved": match,
                "status": "resolved",
                "reason": "single basename match in repo",
            })
            resolved.append(match)
    return resolved, reconciliations


def _operations_to_denylist(operations: list[str]) -> tuple[list[str], list[str]]:
    """From a plan's operations list, compute (allow_list, deny_list).

    Decision P2-1: --allowedTools is a soft prompt for Edit, so we lead
    with the deny-list. The allow-list is reported back in coder_output
    for the harness; the deny-list is what the subprocess actually uses.

    Mapping:
      read         → Read, Glob, Grep allowed
      write        → Read tools + Edit/Write tools allowed
      smoke-test   → SILENTLY DROPPED (orchestrator owns smokes)
      commit       → SILENTLY DROPPED (orchestrator owns commits)
      shell        → Bash allowed (broad; rare)
    """
    ops = set(operations or [])
    allow: set[str] = set()
    if "read" in ops or "write" in ops:
        allow.update(_READ_TOOLS)
    if "write" in ops:
        allow.update(_WRITE_TOOLS)
    if "shell" in ops:
        allow.update(_SHELL_TOOLS)
    # smoke-test / commit deliberately omitted — see docstring.

    # Deny-set: every known mutating tool not in allow.
    known_dangerous = set(_WRITE_TOOLS) | set(_SHELL_TOOLS)
    deny = sorted(known_dangerous - allow)
    return sorted(allow), deny


def _git_files_touched(repo: Path) -> list[str]:
    """Combined output of `git diff --name-only HEAD` and `git ls-files
    --others --exclude-standard`. Names paths the Coder's subprocess
    actually changed or created. Failure → empty list (the orchestrator
    will still see the Coder's exit code).
    """
    out: list[str] = []
    for args in (
        ("diff", "--name-only", "HEAD"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                out.extend(
                    ln.strip() for ln in r.stdout.splitlines() if ln.strip()
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    # Dedupe preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


def _assemble_prompt(plan, reconciliations: list[dict], system_prompt: str) -> str:
    """Assemble the user-side prompt the Coder hands to claude --print.

    The system prompt is the voice-substituted coder-system.md from
    Step 5; it carries the two discipline rules and the [anvil-coder]
    block convention. Both system and user content are passed to
    claude --print as one combined input via stdin (per Step 1 probe
    finding: positional prompt is eaten by variadic flags).

    Reconciliations are surfaced inline so the model knows the path
    rewrite happened — without this, the model would still see
    plan.files_to_touch with its original (missing) paths and might
    refuse the edit.
    """
    plan_dict = (
        plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
    )
    parts = [system_prompt.rstrip(), "", "## Plan", ""]
    parts.append(_format_plan(plan_dict))
    if reconciliations:
        parts.append("")
        parts.append("## Path reconciliations")
        parts.append("")
        parts.append(
            "Before invoking you the orchestrator reconciled the plan's "
            "paths against target_repo_path. Use the resolved paths below "
            "rather than the originals; report a `[anvil-coder]` block at "
            "the end naming any reconciliation you applied."
        )
        for r in reconciliations:
            if r["status"] == "resolved":
                parts.append(
                    f"- `{r['original']}` -> `{r['resolved']}` "
                    f"({r['reason']})"
                )
            else:
                parts.append(
                    f"- `{r['original']}` -> UNRESOLVED ({r['reason']})"
                )
    parts.append("")
    parts.append("## Instruction")
    parts.append("")
    parts.append(
        "Execute the plan: make the edits within the declared scope, "
        "then stop. Do not run smoke tests; do not commit. The "
        "orchestrator owns both. Report observations not captured by "
        "your edits as `[anvil-coder]` blocks at the end of your output."
    )
    return "\n".join(parts)


def _format_plan(plan_dict: dict) -> str:
    """Compact human-readable rendering of the plan dict for the prompt.
    Avoids dumping the full JSON; the Planner already drew the contract.
    """
    keys_in_order = [
        ("step_name", "Step name"),
        ("approach", "Approach"),
        ("files_to_touch", "Files to touch"),
        ("operations", "Operations"),
        ("expected_outcome", "Expected outcome"),
        ("escalation_triggers", "Escalation triggers"),
    ]
    out: list[str] = []
    for key, label in keys_in_order:
        v = plan_dict.get(key)
        if v is None or v == [] or v == "":
            continue
        if isinstance(v, list):
            out.append(f"**{label}:**")
            for item in v:
                out.append(f"- {item}")
        else:
            out.append(f"**{label}:** {v}")
    return "\n".join(out)


def _derive_coder_model(env: dict | None) -> str | None:
    """Derive the Coder model from the CLI envelope's `modelUsage` dict.

    v3 Phase 2b Step 1 (V3P0-1 fix): the `claude --output-format json` envelope
    has no top-level `model` key — the model is the *key* of `modelUsage`. So
    the Phase 0 `env.get("model")` always returned None, and route_actual was
    "unknown" on every Coder call since Phase 0. This derives it correctly.

    Returns the max-`costUSD` key (the model that did the most billable work in
    a multi-model session — Q-B1 locked semantics; a single-model envelope has
    one key, so it returns that key). Returns None for empty/missing
    `modelUsage` and for `env is None`. Defensive on malformed entries: a
    missing `costUSD` is treated as 0.0 (deterministic). Ties resolve to the
    first max key by Python's stable `max` (insertion order) — stable but
    implementation-defined; no Phase 2a/2b corpus exercises a multi-model
    session, so the multi-key path is unit-tested synthetically only.

    The None return is distinguished from envelope-absent at the caller: `env
    is None` means there was no JSON envelope to parse (MockedCoder's
    plain-text path, or a subprocess that died before producing stdout) →
    route_actual="no-envelope"; `env` present but `modelUsage` empty/missing →
    route_actual="unknown", a real-mode diagnostic signal post-Phase-2b. See
    Finding M (Step 0 notes).
    """
    if env is None:
        return None
    model_usage = env.get("modelUsage", {})
    if not model_usage:
        return None
    return max(model_usage.keys(), key=lambda m: model_usage[m].get("costUSD", 0.0))


class Coder:
    """Wraps `claude --print` as the Phase 2 Coder agent.

    Usage:
      coder = Coder(
          claude_binary=Path("/usr/local/bin/claude"),
          timeout=600,
          system_prompt=load_voice_substituted_coder_system_md(),
      )
      coder_output = coder.execute_step(plan, brief)

    coder_output is the eight-key dict from design Part 9, OR an
    escalation block dict (escalate: True, reason, detail,
    step_number, reconciliations) when path reconciliation fails
    pre-execution.
    """

    def __init__(
        self,
        claude_binary: Path,
        timeout: int,
        system_prompt: str,
    ) -> None:
        self.claude_binary = Path(claude_binary)
        self.timeout = int(timeout)
        self.system_prompt = system_prompt

    def _real_run(self, cmd, prompt, target_repo_path):
        """Invoke the Claude subprocess. Extracted from execute_step
        (v2 Phase 1 Step 5) so MockedCoder can override the call site
        without touching the surrounding event-emit + timing + scope-
        verify shell. The signature mirrors what the caller needs:
        the constructed argv (`cmd`), the stdin payload (`prompt`),
        and the cwd (`target_repo_path`). Returns a
        `subprocess.CompletedProcess` — either real (this implementation)
        or fabricated (MockedCoder's override).
        """
        return subprocess.run(
            cmd,
            cwd=target_repo_path,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

    def execute_step(self, plan, brief) -> dict:
        repo = Path(brief.target_repo_path)
        step_number = getattr(plan, "step_number", None)
        # step_idx is 0-based; plan.step_number is 1-based.
        step_idx_evt = (step_number - 1) if isinstance(step_number, int) else None
        # v2 Phase 1 Step 5: stash step_idx on self so a MockedCoder
        # subclass's _real_run can read it without changing the method
        # signature. Defensive default (None) keeps execute_step callable
        # from anywhere (tests don't set this).
        self._current_step_idx = step_idx_evt

        # v2 Phase 1 Step 2: preflight emit.
        _events.emit(
            "coder.preflight.start",
            {"step_idx": step_idx_evt, "step_number": step_number},
            step_idx=step_idx_evt,
        )

        # 1. Pre-flight: path reconciliation.
        plan_files = list(getattr(plan, "files_to_touch", []) or [])
        # v2 Phase 2 Step 4 (V2P2-4): pass the plan's operations so
        # _reconcile_paths can fall through to 'new-file' (rather than
        # 'failed') for an unresolved path when the step declares write.
        plan_operations = list(getattr(plan, "operations", []) or [])
        resolved_paths, reconciliations = _reconcile_paths(
            plan_files, repo, plan_operations
        )
        _events.emit(
            "coder.preflight.reconciled",
            {
                "step_idx": step_idx_evt,
                "reconciliations": reconciliations,
            },
            step_idx=step_idx_evt,
        )
        # Only 'failed' reconciliations escalate. 'new-file' (write
        # carve-out) and 'resolved' (basename match) are not failures.
        failed = [r for r in reconciliations if r["status"] == "failed"]
        if failed:
            detail_lines = [
                f"- {r['original']}: {r['reason']}" for r in failed
            ]
            detail = (
                "Could not reconcile the following plan paths against "
                f"target_repo_path ({repo}):\n" + "\n".join(detail_lines)
            )
            _events.emit(
                "coder.preflight.escalate",
                {
                    "step_idx": step_idx_evt,
                    "reason": "coder-path-reconciliation-failed",
                    "detail": detail[:500],
                },
                step_idx=step_idx_evt,
            )
            return {
                "escalate": True,
                "reason": "coder-path-reconciliation-failed",
                "detail": detail,
                "step_number": step_number,
                "reconciliations": reconciliations,
            }

        # 2. Build the prompt and the deny-list / allow-list pair.
        prompt = _assemble_prompt(plan, reconciliations, self.system_prompt)
        operations = list(getattr(plan, "operations", []) or [])
        allow_list, deny_list = _operations_to_denylist(operations)

        # 3. Construct the subprocess argv. claude --print --permission-mode
        # dontAsk so non-interactive runs don't stall. Prompt via stdin so
        # variadic --disallowedTools / --allowedTools don't eat the positional.
        cmd: list[str] = [
            str(self.claude_binary),
            "--print",
            # v2 Phase 5 Step 1a: JSON output exposes the subprocess's token
            # usage + total_cost_usd so the Coder can be cost-instrumented
            # (previously off-the-books). The model text moves to the
            # envelope's `result` field; `execute_step` extracts it back into
            # `stdout` so every downstream consumer is unchanged. Defensive:
            # mock mode + error paths produce non-JSON stdout → fall back.
            "--output-format", "json",
            "--permission-mode", "dontAsk",
        ]
        if allow_list:
            cmd.extend(["--allowedTools", ",".join(allow_list)])
        if deny_list:
            cmd.extend(["--disallowedTools", ",".join(deny_list)])

        log.info(
            "[coder] step=%s op=%s allow=%s deny=%s",
            getattr(plan, "step_number", "?"),
            ",".join(operations) or "(none)",
            ",".join(allow_list) or "(empty)",
            ",".join(deny_list) or "(empty)",
        )

        _events.emit(
            "coder.subprocess.start",
            {
                "step_idx": step_idx_evt,
                "allowed_tools": allow_list,
                "disallowed_tools": deny_list,
                "prompt_chars": len(prompt),
                "claude_binary": str(self.claude_binary),
            },
            step_idx=step_idx_evt,
        )

        # 4. Invoke and time.
        # v2 Phase 1 Step 5: subprocess.run extracted to self._real_run
        # so MockedCoder can override the call without touching the
        # surrounding event-emit + timing + scope-verify shell.
        start = time.monotonic()
        stdout = ""
        stderr = ""
        exit_code = -1
        # v2 Phase 5 Step 1a: Coder cost instrumentation. Populated from the
        # `--output-format json` envelope when present; stays None on the
        # fallback paths (mock mode, error cases, a claude binary without
        # JSON support) so mock-mode runs correctly record no Coder cost.
        coder_usage = None
        coder_total_cost_usd = None
        # v3 Phase 2b Step 2: additional envelope fields (Q-B2 — all on
        # coder.subprocess.end.data, no new kind). Defaults are mock-safe: on
        # the env-is-None path (mock / dead subprocess) they record as
        # {}/None/[] (honest "no envelope to read"), populated from the envelope
        # only in the result branch below. usage.iterations stays inside
        # coder_usage (Q-B4) — surfaced by the coder_envelope view, not re-emitted.
        coder_model_usage = {}
        coder_num_turns = None
        coder_is_error = None
        coder_stop_reason = None
        coder_subtype = None
        coder_permission_denials = []
        # v3 Phase 0 Step 1 (V3P0-1) + Phase 2b Step 1 (V3P0-1 fix): the actual
        # model the claude -p subprocess ran. Phase 0 read the (nonexistent)
        # top-level `model` key → always None; Phase 2b derives it from the
        # envelope's `modelUsage` (the model is the KEY — see
        # _derive_coder_model). `env` is initialised to None here so the emit
        # site can reference it on the exception paths (timeout / binary-not-
        # found) where the inner parse never runs — env None there →
        # route_actual="no-envelope" (structural), distinct from "unknown"
        # (envelope present, model not derivable). See Finding M.
        env = None
        coder_model = None
        try:
            proc = self._real_run(cmd, prompt, str(repo))
            raw_stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode
            # Defensive parse: extract the model text from the JSON envelope's
            # `result` field (so downstream stdout-consumers see the model text
            # exactly as before) plus the usage + reported cost. Any non-JSON
            # output (MockedCoder text, error output) falls back to raw text.
            try:
                env = json.loads(raw_stdout)
            except (json.JSONDecodeError, TypeError):
                env = None
            if isinstance(env, dict) and "result" in env:
                stdout = env.get("result") or ""
                coder_usage = env.get("usage")
                coder_total_cost_usd = env.get("total_cost_usd")
                coder_model = _derive_coder_model(env)
                # v3 Phase 2b Step 2: defensive .get — the envelope is
                # CLI-version-dependent (captured on CLI 2.1.150); a future
                # version omitting a field falls back to the mock-safe default.
                coder_model_usage = env.get("modelUsage", {}) or {}
                coder_num_turns = env.get("num_turns")
                coder_is_error = env.get("is_error", False)
                coder_stop_reason = env.get("stop_reason")
                coder_subtype = env.get("subtype")
                coder_permission_denials = env.get("permission_denials", []) or []
            else:
                stdout = raw_stdout
        except subprocess.TimeoutExpired as e:
            stdout = (
                e.stdout.decode("utf-8", "replace")
                if isinstance(e.stdout, bytes)
                else (e.stdout or "")
            )
            stderr = (
                e.stderr.decode("utf-8", "replace")
                if isinstance(e.stderr, bytes)
                else (e.stderr or "")
            )
            stderr = (
                stderr
                + f"\n[coder-timeout] subprocess killed after {self.timeout}s"
            )
            exit_code = -1
        except FileNotFoundError:
            stderr = (
                f"[coder-error] claude binary not found at "
                f"{self.claude_binary}"
            )
            exit_code = -2
        except Exception as e:  # noqa: BLE001 — never-raise contract
            stderr = f"[coder-error] {type(e).__name__}: {e}"
            exit_code = -3
        duration_s = time.monotonic() - start

        # v3 Phase 2b Step 1 (V3P0-1 fix + Finding M): three-way route_actual,
        # distinguishing structural envelope-absence from diagnostic failure:
        #   env is None             → "no-envelope" (no JSON envelope to parse:
        #                              MockedCoder's plain text, or a subprocess
        #                              that died before producing stdout)
        #   env present, model None → "unknown" (envelope parsed but model not
        #                              derivable — a real-mode diagnostic signal)
        #   else                    → the derived model (V3P0-1 retired, real path)
        if env is None:
            coder_route_actual = "no-envelope"
        elif coder_model is None:
            coder_route_actual = "unknown"
        else:
            coder_route_actual = coder_model

        _events.emit(
            "coder.subprocess.end",
            {
                "step_idx": step_idx_evt,
                "exit_code": exit_code,
                "duration_ms": int(duration_s * 1000),
                "stdout_chars": len(stdout),
                "stderr_chars": len(stderr),
                # v2 Phase 5 Step 1a: Coder cost instrumentation. None on the
                # fallback paths (mock mode / errors). total_cost_usd is the
                # CLI's reported figure (the Coder runs a cheaper model than
                # the Planner's Opus, so the operations view sources Coder
                # cost from this, NOT its Opus token formula — Step 0 Q2).
                # The token fields are surfaced at top level (the operations
                # view's input/output/cache columns + cache_hit_rate read them
                # there) so Coder rows get full token observability; the cost
                # still comes from total_cost_usd via the view's coder CASE.
                "usage": coder_usage,
                "total_cost_usd": coder_total_cost_usd,
                "input_tokens": (coder_usage or {}).get("input_tokens"),
                "output_tokens": (coder_usage or {}).get("output_tokens"),
                "cache_creation_input_tokens":
                    (coder_usage or {}).get("cache_creation_input_tokens"),
                "cache_read_input_tokens":
                    (coder_usage or {}).get("cache_read_input_tokens"),
                # v3 Phase 2b Step 2: five additional envelope fields (Q-B2 —
                # all on .data, no new kind). Q-B3: model_usage carries per-model
                # costUSD as ADDITIONAL attribution; total_cost_usd above stays
                # the operations-view cost-CASE source (V2P5-1 preserved). All
                # null/empty on the mock/no-envelope path.
                "model_usage": coder_model_usage,
                "num_turns": coder_num_turns,
                "is_error": coder_is_error,
                "stop_reason": coder_stop_reason,
                "subtype": coder_subtype,
                "permission_denials": coder_permission_denials,
                # v3 Phase 0 Step 1 (V3P0-1) + Phase 2b Step 1 (fix): routing
                # observability for the Coder. route_actual is the model the
                # CLI ran, derived from the envelope's modelUsage key (Q-B1
                # max-costUSD); "no-envelope" when there's no JSON envelope
                # (mock path / dead subprocess), "unknown" when the envelope
                # parsed but the model wasn't derivable (real-mode diagnostic).
                # observed prompt size is the sum of the three usage token
                # lines; context size is the count of plan paths the step targets.
                **_events.routing_observability(
                    stage="coder",
                    step_idx=step_idx_evt,
                    observed_prompt_token_count=(
                        ((coder_usage or {}).get("input_tokens") or 0)
                        + ((coder_usage or {}).get("cache_creation_input_tokens") or 0)
                        + ((coder_usage or {}).get("cache_read_input_tokens") or 0)
                    ),
                    context_paths_count=len(plan_files),
                    route_actual=coder_route_actual,
                ),
            },
            step_idx=step_idx_evt,
        )

        # 5. Layer 2 — post-hoc scope verification via git.
        files_touched = _git_files_touched(repo)
        # Compute out_of_scope against the RESOLVED path set. If the
        # plan said reporter/x.py and we reconciled to x.py, x.py is
        # in scope, reporter/x.py is not — match accordingly.
        in_scope = set(resolved_paths) | set(plan_files)
        out_of_scope = [f for f in files_touched if f not in in_scope]

        _events.emit(
            "coder.scope_verify",
            {
                "step_idx": step_idx_evt,
                "files_touched": files_touched,
                "files_touched_count": len(files_touched),
                "out_of_scope": out_of_scope,
                "out_of_scope_count": len(out_of_scope),
            },
            step_idx=step_idx_evt,
        )

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "files_touched": files_touched,
            "out_of_scope": out_of_scope,
            "reconciliations": reconciliations,
            "duration_s": duration_s,
            "allowed_tools": allow_list,
            # Reported for the harness; consumers don't typically use this.
            "disallowed_tools": deny_list,
        }


def parse_anvil_coder_blocks(text: str) -> list[str]:
    """Extract `[anvil-coder]` factual blocks from a Coder's stdout per
    the Step 5 prompt convention. A block starts at a line beginning
    with `[anvil-coder]` and runs to the next blank line or end-of-
    string. Empty input → empty list. The Step 9 orchestrator wiring
    may surface these as structured observations.
    """
    if not text:
        return []
    blocks: list[str] = []
    buf: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.startswith("[anvil-coder]"):
            if in_block and buf:
                blocks.append("\n".join(buf))
                buf = []
            in_block = True
            buf.append(line)
        elif in_block:
            if not line.strip():
                if buf:
                    blocks.append("\n".join(buf))
                    buf = []
                in_block = False
            else:
                buf.append(line)
    if in_block and buf:
        blocks.append("\n".join(buf))
    return blocks
