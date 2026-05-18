#!/usr/bin/env python3
"""Phase 2 Step 9 — wire Coder into Orchestrator.handle_brief; close
decisions #14 and #17 via head_hash fallback at the commit phase.

Six semantic changes:

  1. anvil/orchestrator.py imports Coder + loads the coder-system.md
     prompt with voice substitution (mirrors Phase 1 Planner system-
     prompt loading at __init__).
  2. Orchestrator.__init__ gains a `coder=None` kwarg. When None and
     coder_mode == "auto", a real Coder is constructed from config.
     Tests inject FakeCoder via the kwarg, same posture as FakePlanner.
  3. The auto-mode NotImplementedError at line ~190 is replaced with
     a no-op (the auto-mode flow is now wired through step 5c-auto).
  4. Step 5c gains an auto-mode branch: if coder_mode == "auto", call
     self.coder.execute_step(plan, brief), store the result on
     state.steps[idx].coder_output, route escalations (escalation block
     -> _escalate; out_of_scope -> coder-out-of-scope; exit_code != 0
     -> coder-failed). The manual branch is unchanged.
  5. Step 5e commit gains the head_hash fallback per design Part 3:
     state.commit = commit_hash or head_hash(target_repo_path). Closes
     decisions #14/17 — manual-mode runs now record Genco's commit
     attribution via git rev-parse HEAD when ANVIL's commit_step was a
     no-op.
  6. anvil/config.py gains CLAUDE_BINARY (default shutil.which("claude")).

Idempotent. Aborts non-zero if any anchor is missing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORCH = ROOT / "anvil" / "orchestrator.py"
CONFIG = ROOT / "anvil" / "config.py"

for f in (ORCH, CONFIG):
    if not f.is_file():
        print(f"error: {f} not found.", file=sys.stderr)
        sys.exit(1)


# ===========================================================================
# Part 1 — anvil/orchestrator.py
# ===========================================================================

src = ORCH.read_text(encoding="utf-8")
orig = src

# ---------------------------------------------------------------------------
# Edit 1: import Coder. Add to the existing anvil-package imports.
# ---------------------------------------------------------------------------
import_old = (
    "from anvil import git_ops as _git_ops\n"
    "from anvil.brief import parse_brief, resolve_context_paths, validate_or_reject\n"
    "from anvil.errors import AnvilError\n"
    "from anvil.planner import Plan, Planner\n"
)
import_new = (
    "from anvil import git_ops as _git_ops\n"
    "from anvil.brief import parse_brief, resolve_context_paths, validate_or_reject\n"
    "from anvil.coder import Coder\n"
    "from anvil.errors import AnvilError\n"
    "from anvil.planner import Plan, Planner\n"
)

if "from anvil.coder import Coder" in src:
    print("[1/6] Coder import already present; skipping.")
elif import_old in src:
    src = src.replace(import_old, import_new, 1)
    print("[1/6] Coder import added.")
else:
    print("error: could not find anvil-package import block.",
          file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Edit 2: Orchestrator.__init__ gains a coder= kwarg and lazy construction.
# Add it after the run_smoke kwarg.
# ---------------------------------------------------------------------------
init_old = (
    "        coder_mode: str = \"manual\",   # Phase 0 hardcodes manual\n"
    "        planner=None,\n"
    "        telegram=None,\n"
    "        git=None,\n"
    "        run_smoke=None,\n"
    "    ) -> None:\n"
    "        self.config = config\n"
    "        self.coder_mode = coder_mode\n"
    "        self.planner = planner if planner is not None else Planner(\n"
    "            api_key=config.anthropic_api_key,\n"
    "            model=config.planner_model,\n"
    "            timeout=config.planner_timeout,\n"
    "            vault_root=config.vault_path,\n"
    "        )\n"
    "        self._telegram = telegram          # may be None until needed\n"
    "        self.git = git if git is not None else _git_ops\n"
    "        self._run_smoke = run_smoke or self._default_run_smoke\n"
    "        # decision #1 closed: zero-arg load_voice_spec() (VAULT_PATH env is\n"
    "        # the source of truth); the Phase 0 vault_root shim is removed.\n"
    "        self.voice_spec = voice.load_voice_spec()\n"
    "        self._run_log: Path | None = None\n"
    "        self._state = None\n"
)
init_new = (
    "        coder_mode: str = \"manual\",   # Phase 0 hardcodes manual\n"
    "        planner=None,\n"
    "        telegram=None,\n"
    "        git=None,\n"
    "        run_smoke=None,\n"
    "        coder=None,\n"
    "    ) -> None:\n"
    "        self.config = config\n"
    "        self.coder_mode = coder_mode\n"
    "        self.planner = planner if planner is not None else Planner(\n"
    "            api_key=config.anthropic_api_key,\n"
    "            model=config.planner_model,\n"
    "            timeout=config.planner_timeout,\n"
    "            vault_root=config.vault_path,\n"
    "        )\n"
    "        self._telegram = telegram          # may be None until needed\n"
    "        self.git = git if git is not None else _git_ops\n"
    "        self._run_smoke = run_smoke or self._default_run_smoke\n"
    "        # decision #1 closed: zero-arg load_voice_spec() (VAULT_PATH env is\n"
    "        # the source of truth); the Phase 0 vault_root shim is removed.\n"
    "        self.voice_spec = voice.load_voice_spec()\n"
    "        # Phase 2 Step 9: lazy Coder construction. Only built when\n"
    "        # auto-mode is requested AND no coder was injected. Manual mode\n"
    "        # leaves self.coder = None and never reads it.\n"
    "        if coder is not None:\n"
    "            self.coder = coder\n"
    "        elif coder_mode == \"auto\":\n"
    "            self.coder = self._build_coder()\n"
    "        else:\n"
    "            self.coder = None\n"
    "        self._run_log: Path | None = None\n"
    "        self._state = None\n"
    "\n"
    "    def _build_coder(self) -> Coder:\n"
    "        \"\"\"Construct a real Coder from config. The system prompt is\n"
    "        coder-system.md with {VOICE_SPEC} substituted. claude_binary\n"
    "        defaults to whatever `claude` resolves to on PATH at startup,\n"
    "        overridable via CLAUDE_BINARY in .env. Coder timeout reuses\n"
    "        config.coder_timeout (already present since Phase 0).\n"
    "        \"\"\"\n"
    "        prompt_path = Path(__file__).resolve().parent / \"prompts\" / \"coder-system.md\"\n"
    "        prompt_text = prompt_path.read_text(encoding=\"utf-8\")\n"
    "        prompt_text = prompt_text.replace(\"{VOICE_SPEC}\", self.voice_spec)\n"
    "        binary = Path(\n"
    "            getattr(self.config, \"claude_binary\", None)\n"
    "            or shutil.which(\"claude\")\n"
    "            or \"claude\"\n"
    "        )\n"
    "        return Coder(\n"
    "            claude_binary=binary,\n"
    "            timeout=self.config.coder_timeout,\n"
    "            system_prompt=prompt_text,\n"
    "        )\n"
)

if "def _build_coder(self)" in src:
    print("[2/6] Orchestrator.__init__ coder kwarg already wired; skipping.")
elif init_old in src:
    src = src.replace(init_old, init_new, 1)
    print("[2/6] Orchestrator.__init__ gained coder= kwarg + _build_coder.")
else:
    print("error: could not find Orchestrator.__init__ anchor.",
          file=sys.stderr)
    sys.exit(3)


# ---------------------------------------------------------------------------
# Edit 3: replace the auto-mode NotImplementedError with a no-op so the
# auto branch in step 5c can be reached.
# ---------------------------------------------------------------------------
not_impl_old = (
    "            if self.coder_mode == \"auto\":\n"
    "                # Phase 2 work\n"
    "                raise NotImplementedError(\n"
    "                    \"auto coder_mode is Phase 2 work; Phase 0 is manual only\"\n"
    "                )\n"
)
# Loose pattern in case the comment text differs
not_impl_pattern_lines = [
    "            if self.coder_mode == \"auto\":",
    "                raise NotImplementedError(",
]

not_impl_new = (
    "            if self.coder_mode == \"auto\":\n"
    "                # Phase 2 Step 9 wires this path through the step loop;\n"
    "                # no-op here. The auto branch in step 5c does the work.\n"
    "                pass\n"
)

if "Phase 2 Step 9 wires this path through the step loop" in src:
    print("[3/6] auto-mode NotImplementedError already replaced; skipping.")
elif not_impl_old in src:
    src = src.replace(not_impl_old, not_impl_new, 1)
    print("[3/6] auto-mode NotImplementedError replaced with no-op.")
else:
    # Fallback: do a multi-line replace by anchor pair
    if all(line in src for line in not_impl_pattern_lines):
        # Find the block and replace from "if" line to the next blank/dedent
        idx_if = src.index(not_impl_pattern_lines[0])
        # Walk forward to find the closing ")"
        idx_close = src.index(")", src.index("NotImplementedError(", idx_if))
        # Find end of line containing the close paren
        end_of_block = src.index("\n", idx_close) + 1
        src = src[:idx_if] + not_impl_new.lstrip("\n") + src[end_of_block:]
        print("[3/6] auto-mode NotImplementedError replaced via fallback.")
    else:
        print("error: could not find auto-mode NotImplementedError block.",
              file=sys.stderr)
        sys.exit(4)


# ---------------------------------------------------------------------------
# Edit 4: step 5c — add the auto-mode branch around the existing manual call.
# ---------------------------------------------------------------------------
step5c_old = (
    "                # 5c manual-Coder execution\n"
    "                outcome = self._manual_step(plan)\n"
    "                self._log_event(\"coder(manual)\", f\"reply={outcome}\")\n"
    "                if outcome == \"abort\":\n"
    "                    state = transition(state, \"aborted\")\n"
    "                    return 1\n"
    "                if outcome == \"skip\":\n"
    "                    state.steps[idx].status = \"done\"\n"
    "                    state.steps[idx].commit = None\n"
    "                    state = transition(state, \"running\")\n"
    "                    continue\n"
)
step5c_new = (
    "                # 5c Coder execution — branch on coder_mode.\n"
    "                # Phase 2 Step 9: auto-mode invokes anvil.coder.Coder;\n"
    "                # manual-mode is the Phase 0/1 flow, unchanged.\n"
    "                if self.coder_mode == \"auto\":\n"
    "                    coder_output = self.coder.execute_step(plan, brief)\n"
    "                    state.steps[idx].coder_output = coder_output\n"
    "                    self._state = state\n"
    "                    write_state(state)\n"
    "                    self._log_event(\n"
    "                        \"coder(auto)\",\n"
    "                        f\"exit={coder_output.get('exit_code')} \"\n"
    "                        f\"files={len(coder_output.get('files_touched') or [])} \"\n"
    "                        f\"oos={len(coder_output.get('out_of_scope') or [])} \"\n"
    "                        f\"dur={coder_output.get('duration_s', 0):.1f}s\",\n"
    "                    )\n"
    "                    # Route post-Coder escalations.\n"
    "                    if coder_output.get(\"escalate\") is True:\n"
    "                        self._escalate(\n"
    "                            state,\n"
    "                            coder_output.get(\"reason\", \"coder escalation\"),\n"
    "                            coder_output.get(\"detail\", \"\"),\n"
    "                            (\"go\", \"abort\"),\n"
    "                        )\n"
    "                        if not self._await_user_decision(state):\n"
    "                            return 1\n"
    "                        # User said go past the reconciliation failure.\n"
    "                        # Skip the step (cannot execute without resolved\n"
    "                        # paths); same posture as Planner escalation.\n"
    "                        state.steps[idx].status = \"done\"\n"
    "                        state.steps[idx].commit = None\n"
    "                        state = transition(state, \"running\")\n"
    "                        self._state = state\n"
    "                        continue\n"
    "                    if coder_output.get(\"out_of_scope\"):\n"
    "                        self._escalate(\n"
    "                            state, \"coder-out-of-scope\",\n"
    "                            \"Files touched outside plan scope: \"\n"
    "                            + \", \".join(coder_output[\"out_of_scope\"]),\n"
    "                            (\"go\", \"abort\"),\n"
    "                        )\n"
    "                        if not self._await_user_decision(state):\n"
    "                            return 1\n"
    "                    if coder_output.get(\"exit_code\", 0) != 0:\n"
    "                        self._escalate(\n"
    "                            state, \"coder-failed\",\n"
    "                            (coder_output.get(\"stderr\") or \"\")[:1500]\n"
    "                            or \"Coder exited non-zero with no stderr.\",\n"
    "                            (\"go\", \"abort\"),\n"
    "                        )\n"
    "                        if not self._await_user_decision(state):\n"
    "                            return 1\n"
    "                else:\n"
    "                    # 5c manual-Coder execution (Phase 0/1 flow).\n"
    "                    outcome = self._manual_step(plan)\n"
    "                    self._log_event(\"coder(manual)\", f\"reply={outcome}\")\n"
    "                    if outcome == \"abort\":\n"
    "                        state = transition(state, \"aborted\")\n"
    "                        return 1\n"
    "                    if outcome == \"skip\":\n"
    "                        state.steps[idx].status = \"done\"\n"
    "                        state.steps[idx].commit = None\n"
    "                        state = transition(state, \"running\")\n"
    "                        continue\n"
)

if "# Phase 2 Step 9: auto-mode invokes anvil.coder.Coder" in src:
    print("[4/6] step 5c auto-mode branch already wired; skipping.")
elif step5c_old in src:
    src = src.replace(step5c_old, step5c_new, 1)
    print("[4/6] step 5c gained auto-mode branch.")
else:
    print("error: could not find step 5c manual-step anchor.",
          file=sys.stderr)
    sys.exit(5)


# ---------------------------------------------------------------------------
# Edit 5: step 5e — add the head_hash fallback for state.commit. Closes
# decisions #14 and #17.
# ---------------------------------------------------------------------------
step5e_old = (
    "                commit_hash = self.git.commit_step(\n"
    "                    brief.target_repo_path, plan, idx,\n"
    "                    brief_name=brief.build_name,\n"
    "                    commit_message_hint=bstep.commit_message_hint,\n"
    "                    run_log_filename=Path(self._run_log).name,\n"
    "                )\n"
    "                state.steps[idx].commit = commit_hash or None\n"
)
step5e_new = (
    "                commit_hash = self.git.commit_step(\n"
    "                    brief.target_repo_path, plan, idx,\n"
    "                    brief_name=brief.build_name,\n"
    "                    commit_message_hint=bstep.commit_message_hint,\n"
    "                    run_log_filename=Path(self._run_log).name,\n"
    "                )\n"
    "                # Phase 2 Step 9 (decisions #14/17): if commit_step\n"
    "                # was a no-op (manual mode: Genco committed in his own\n"
    "                # Claude Code session, ANVIL's `git add -A` found\n"
    "                # nothing), fall back to head_hash so the state\n"
    "                # records the attribution that exists in the git log.\n"
    "                # Design Part 3 §\"Manual mode preserved\": \"The state\n"
    "                # still records the head commit hash via\n"
    "                # `git rev-parse HEAD` so attribution holds either way.\"\n"
    "                state.steps[idx].commit = (\n"
    "                    commit_hash\n"
    "                    or self.git.head_hash(brief.target_repo_path)\n"
    "                )\n"
)

if "Phase 2 Step 9 (decisions #14/17)" in src:
    print("[5/6] step 5e head_hash fallback already wired; skipping.")
elif step5e_old in src:
    src = src.replace(step5e_old, step5e_new, 1)
    print("[5/6] step 5e gained head_hash fallback — decisions #14/17 close.")
else:
    print("error: could not find step 5e commit anchor.", file=sys.stderr)
    sys.exit(6)


if src != orig:
    backup = ORCH.with_suffix(".py.pre-phase-2-step-9.bak")
    backup.write_text(orig, encoding="utf-8")
    ORCH.write_text(src, encoding="utf-8")
    print(f"wrote {ORCH} (backup at {backup})")


# ===========================================================================
# Part 2 — anvil/config.py: add CLAUDE_BINARY
# ===========================================================================

src = CONFIG.read_text(encoding="utf-8")
orig = src

# Add `claude_binary: str | None` to the dataclass and load it from env.
dataclass_old = (
    "    planner_model: str\n"
    "    planner_timeout: int\n"
    "    coder_timeout: int\n"
)
dataclass_new = (
    "    planner_model: str\n"
    "    planner_timeout: int\n"
    "    coder_timeout: int\n"
    "    claude_binary: str | None\n"
)
if "claude_binary: str | None" in src:
    print("[6a/6] Config.claude_binary already present; skipping.")
elif dataclass_old in src:
    src = src.replace(dataclass_old, dataclass_new, 1)
    print("[6a/6] Config gained claude_binary field.")
else:
    print(
        "error: could not find Config dataclass body anchor.",
        file=sys.stderr,
    )
    sys.exit(7)

# Load from env. Optional — if unset, Orchestrator._build_coder falls back
# to shutil.which("claude"). Empty string normalised to None.
env_old = (
    "        coder_timeout = int_env(\"CODER_TIMEOUT_SECONDS\", 600)\n"
)
env_new = (
    "        coder_timeout = int_env(\"CODER_TIMEOUT_SECONDS\", 600)\n"
    "        claude_binary = os.environ.get(\"CLAUDE_BINARY\", \"\").strip() or None\n"
)
if "claude_binary = os.environ.get" in src:
    print("[6b/6] config env-load of CLAUDE_BINARY already present; skipping.")
elif env_old in src:
    src = src.replace(env_old, env_new, 1)
    print("[6b/6] config now reads CLAUDE_BINARY from env.")
else:
    print("error: could not find coder_timeout env-load anchor.",
          file=sys.stderr)
    sys.exit(8)

# Return statement — add claude_binary to the kwargs.
return_old = (
    "            planner_model=planner_model,\n"
    "            planner_timeout=planner_timeout,\n"
    "            coder_timeout=coder_timeout,\n"
    "        )\n"
)
return_new = (
    "            planner_model=planner_model,\n"
    "            planner_timeout=planner_timeout,\n"
    "            coder_timeout=coder_timeout,\n"
    "            claude_binary=claude_binary,\n"
    "        )\n"
)
if "claude_binary=claude_binary," in src:
    print("[6c/6] Config return already passes claude_binary; skipping.")
elif return_old in src:
    src = src.replace(return_old, return_new, 1)
    print("[6c/6] Config return now includes claude_binary.")
else:
    print(
        "error: could not find Config.load return-statement anchor.",
        file=sys.stderr,
    )
    sys.exit(9)


if src != orig:
    backup = CONFIG.with_suffix(".py.pre-phase-2-step-9.bak")
    backup.write_text(orig, encoding="utf-8")
    CONFIG.write_text(src, encoding="utf-8")
    print(f"wrote {CONFIG} (backup at {backup})")

print("\nverify with:")
print("  .venv/bin/python -m py_compile anvil/orchestrator.py anvil/config.py")
print("  .venv/bin/python -m unittest discover tests/ -v")
