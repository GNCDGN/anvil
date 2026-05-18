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

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

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


def _reconcile_paths(plan_files: list[str], repo: Path) -> tuple[list[str], list[dict]]:
    """For each path in plan_files, check existence at repo/path. If
    missing, look for a single-basename match. Returns (resolved_paths,
    reconciliations) where resolved_paths is the path list as the Coder
    will use (originals where existing, resolved where reconciled), and
    reconciliations is the structured record per design Part 4 layer 3.

    Reconciliation dict shape:
      {original: str, resolved: str | None, status: 'resolved' | 'failed',
       reason: str}
    """
    resolved: list[str] = []
    reconciliations: list[dict] = []
    for p in plan_files:
        if (repo / p).exists():
            resolved.append(p)
            continue
        match = _basename_match(repo, p)
        if match is None:
            reconciliations.append({
                "original": p,
                "resolved": None,
                "status": "failed",
                "reason": "no single basename match in repo",
            })
            # Still include the original — the failed reconciliation is
            # what the escalation block surfaces; the caller decides.
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

    def execute_step(self, plan, brief) -> dict:
        repo = Path(brief.target_repo_path)

        # 1. Pre-flight: path reconciliation.
        plan_files = list(getattr(plan, "files_to_touch", []) or [])
        resolved_paths, reconciliations = _reconcile_paths(plan_files, repo)
        failed = [r for r in reconciliations if r["status"] == "failed"]
        if failed:
            detail_lines = [
                f"- {r['original']}: {r['reason']}" for r in failed
            ]
            return {
                "escalate": True,
                "reason": "coder-path-reconciliation-failed",
                "detail": (
                    "Could not reconcile the following plan paths against "
                    f"target_repo_path ({repo}):\n" + "\n".join(detail_lines)
                ),
                "step_number": getattr(plan, "step_number", None),
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

        # 4. Invoke and time.
        start = time.monotonic()
        stdout = ""
        stderr = ""
        exit_code = -1
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode
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

        # 5. Layer 2 — post-hoc scope verification via git.
        files_touched = _git_files_touched(repo)
        # Compute out_of_scope against the RESOLVED path set. If the
        # plan said reporter/x.py and we reconciled to x.py, x.py is
        # in scope, reporter/x.py is not — match accordingly.
        in_scope = set(resolved_paths) | set(plan_files)
        out_of_scope = [f for f in files_touched if f not in in_scope]

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
