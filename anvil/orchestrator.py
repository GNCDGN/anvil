"""Orchestrator state machine (implementation-notes Component 1).

Phase 0: manual-Coder mode ONLY. `coder_mode == "auto"` (the Phase 2+ path)
raises NotImplementedError — nothing more (per Step 8 note 3).

NO LOCK FILE. The lock-file mechanism is dead; coordination is dedicated-bot
/ independent update streams. This orchestrator never writes `~/.anvil-active`
(or anything like it). The only file under `state/` besides the state files
and the run log is `telegram-down.marker`, and that is written by
`TelegramClient.send` ONLY on send failure — never by a clean run.

Component-1 ↔ Component-4 reconciliation (flagged in the Step 8 report):
Component 1 step 5e has the orchestrator commit via `git_ops.commit_step`;
Component 4 manual mode has Genco reply `done <hash>` having committed
himself. Phase 0 resolves this so the run-log footer stays consistent: in
manual mode Genco does the file edits in Claude Code and replies `done`
(optionally `done <hash>` — the hash is logged but informational); the
orchestrator owns the commit via `git_ops.commit_step` so the canonical
Component 7 message + run-log footer are always correct. `skip` / `abort`
also handled.

Never-raise contract: `run()` / `handle_brief()` catch Exception → log →
best-effort escalate → return non-zero. KeyboardInterrupt is caught at the
top, persists state as `paused-mid-execution`, exits cleanly.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from anvil import events as _events
from anvil import git_ops as _git_ops
from anvil.brief import parse_brief, resolve_context_paths, validate_or_reject
from anvil.coder import Coder
from anvil import ssh_ops
from anvil import checkpoint as _checkpoint  # Phase 4 Step 5
from anvil import vault_ops as _vault_ops  # Phase 4 Step 5
from anvil.errors import AnvilError
from anvil.planner import Plan, Planner
from anvil.state import (
    PendingAction,
    State,
    init_state,
    state_dir,
    transition,
    write_state,
)
from anvil import voice

log = logging.getLogger("anvil.orchestrator")
_UK = ZoneInfo("Europe/London")


def _slug(build_name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (build_name or "").lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "build"


class Orchestrator:
    def __init__(
        self,
        config,
        *,
        coder_mode: str | None = None,   # Phase 4 Step 1: None → resolve from config.coder_mode
        planner=None,
        telegram=None,
        git=None,
        run_smoke=None,
        coder=None,
    ) -> None:
        self.config = config
        # Phase 4 Step 1 Layer 2 fix (Step 0 Finding 1): explicit kwarg
        # wins for test injection; otherwise config.coder_mode (read from
        # CODER_MODE env) determines the mode. The Phase 0/1 default of
        # 'manual' is gone — production now honours .env.
        self.coder_mode = coder_mode if coder_mode is not None else getattr(config, "coder_mode", "manual")
        # v2 Phase 1 Step 5: extracted _build_planner() so the
        # mocked-vs-real switch (Config.mocked_planner) lives in one
        # method, mirroring the existing _build_coder seam.
        self.planner = planner if planner is not None else self._build_planner()
        self._telegram = telegram          # may be None until needed
        self.git = git if git is not None else _git_ops
        self._run_smoke = run_smoke or self._default_run_smoke
        # decision #1 closed: zero-arg load_voice_spec() (VAULT_PATH env is
        # the source of truth); the Phase 0 vault_root shim is removed.
        self.voice_spec = voice.load_voice_spec()
        # Phase 2 Step 9: lazy Coder construction. Only built when
        # auto-mode is requested AND no coder was injected. Manual mode
        # leaves self.coder = None and never reads it.
        if coder is not None:
            self.coder = coder
        elif self.coder_mode == "auto":
            self.coder = self._build_coder()
        else:
            self.coder = None
        self._run_log: Path | None = None
        self._state = None

    def _build_planner(self) -> Planner:
        """Construct the Planner instance (real or mocked subclass).

        v2 Phase 1 Step 5: if `config.mocked_planner` is set
        (`MOCKED_PLANNER=1`), return a `MockedPlanner` subclass that
        substitutes `_call_anthropic` with fixture-driven responses and
        synthesised api_end emits. Otherwise return the production
        Planner. Lazy import of `anvil.mocked` avoids the circular
        import path (mocked imports Planner; orchestrator imports
        mocked only via this method).
        """
        if getattr(self.config, "mocked_planner", False):
            from anvil.mocked import MockedPlanner
            return MockedPlanner(
                api_key=self.config.anthropic_api_key,
                model=self.config.planner_model,
                timeout=self.config.planner_timeout,
                vault_root=self.config.vault_path,
            )
        return Planner(
            api_key=self.config.anthropic_api_key,
            model=self.config.planner_model,
            timeout=self.config.planner_timeout,
            vault_root=self.config.vault_path,
        )

    def _build_coder(self) -> Coder:
        """Construct a real Coder from config. The system prompt is
        coder-system.md with {VOICE_SPEC} substituted. claude_binary
        defaults to whatever `claude` resolves to on PATH at startup,
        overridable via CLAUDE_BINARY in .env. Coder timeout reuses
        config.coder_timeout (already present since Phase 0).

        v2 Phase 1 Step 5: switches to MockedCoder when
        `config.mocked_coder` is set. The mocked Coder honours the
        same constructor surface (claude_binary, timeout, system_prompt)
        and overrides `_real_run` only — preflight + Layer 2 scope
        verify still run on real disk against real git.
        """
        prompt_path = Path(__file__).resolve().parent / "prompts" / "coder-system.md"
        prompt_text = prompt_path.read_text(encoding="utf-8")
        prompt_text = prompt_text.replace("{VOICE_SPEC}", self.voice_spec)
        binary = Path(
            getattr(self.config, "claude_binary", None)
            or shutil.which("claude")
            or "claude"
        )
        if getattr(self.config, "mocked_coder", False):
            from anvil.mocked import MockedCoder
            return MockedCoder(
                claude_binary=binary,
                timeout=self.config.coder_timeout,
                system_prompt=prompt_text,
            )
        return Coder(
            claude_binary=binary,
            timeout=self.config.coder_timeout,
            system_prompt=prompt_text,
        )

    # ---- telegram (lazy so unit tests can inject a mock) ----
    @property
    def telegram(self):
        if self._telegram is None:
            from anvil.telegram import TelegramClient
            self._telegram = TelegramClient(
                self.config.telegram_bot_token, self.config.telegram_chat_id
            )
        return self._telegram

    # ---- run log ----
    def _open_run_log(self, brief, started_at: str) -> Path:
        runs = state_dir() / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(_UK).strftime("%Y-%m-%d-%H%M")
        path = runs / f"{stamp}-{_slug(brief.build_name)}.md"
        path.write_text(
            f"# ANVIL run log — {brief.build_name}\n\n"
            f"Started: {started_at}\nBrief: {brief.build_name}\n\n",
            encoding="utf-8",
        )
        self._run_log = path
        return path

    def _log_event(self, kind: str, detail: str) -> None:
        if self._run_log is None:
            return
        ts = datetime.now(_UK).strftime("%H:%M:%S")
        with self._run_log.open("a", encoding="utf-8") as f:
            f.write(f"- [{ts}] **{kind}** — {detail}\n")

    def _default_run_smoke(self, cmd: str, cwd: Path):
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=str(cwd),
                capture_output=True, text=True, timeout=120,
            )
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as e:  # noqa: BLE001
            return False, f"smoke runner error: {e}"

    # ---- public API ----
    def run(self, brief_path: Path) -> int:
        from anvil.telegram import (
            install_interrupt_handler,
            restore_interrupt_handler,
        )
        install_interrupt_handler()
        try:
            return self.handle_brief(Path(brief_path))
        except KeyboardInterrupt:
            log.warning("KeyboardInterrupt — persisting state, exiting")
            try:
                st = self._state
                transition(st, "paused-mid-execution")
            except Exception:  # noqa: BLE001
                pass
            return 2
        except Exception as e:  # noqa: BLE001 — never-raise contract
            log.error(f"fatal in run(): {e}", exc_info=True)
            return 2
        finally:
            restore_interrupt_handler()

    def resume(self) -> int:
        from anvil.state import read_state
        from anvil.telegram import (
            install_interrupt_handler,
            restore_interrupt_handler,
        )
        st = read_state()
        if st is None or st.status in ("done", "failed", "aborted"):
            log.info("nothing to resume")
            return 0
        install_interrupt_handler()
        try:
            self.telegram.send(
                f"[ANVIL] Resuming {Path(st.brief_path).name}, step "
                f"{st.current_step} ({st.status}). Reply 'resume' or 'abort'."
            )
            reply = self.telegram.wait_for_reply(timeout=None)
            if reply is None or reply.text.strip().lower() != "resume":
                transition(st, "aborted")
                return 1
            return self.handle_brief(Path(st.brief_path), resumed_state=st)
        except KeyboardInterrupt:
            log.warning("KeyboardInterrupt in resume() — persisting state")
            try:
                transition(self._state or st, "paused-mid-execution")
            except Exception:  # noqa: BLE001
                pass
            return 2
        except Exception as e:  # noqa: BLE001 — never-raise contract
            log.error(f"fatal in resume(): {e}", exc_info=True)
            return 2
        finally:
            restore_interrupt_handler()

    def handle_brief(
        self, brief_path: Path, *, resumed_state: State | None = None,
    ) -> int:
        # v2 Phase 1 Step 2 (notes.md Finding 1 constraint 4): the
        # existing try/except gains a `finally:` clause at the end of
        # the function body to guarantee events.end_run() fires on
        # every exit path — success, early return, exception. The
        # finally on a try/except/finally runs after any except's
        # return or re-raise. end_run() is a no-op when begin_run()
        # never fired (e.g. parse_brief raised), so the wrapper
        # handles partial-initialisation cases without special-casing.
        try:
            if self.coder_mode == "auto":
                # Phase 2 Step 9 wires this path through the step loop;
                # no-op here. The auto branch in step 5c does the work.
                pass

            # Parse brief first — build_name seeds the run_id slug.
            brief = parse_brief(brief_path)

            # v2 Phase 1 Step 2: determine run_id and call begin_run
            # BEFORE the brief.parsed/brief.validated emits so they
            # land in the right run's events.jsonl (not unknown-run/).
            #
            # v2 Phase 1 Step 5 (Step 4 outcome finding 1):
            # ANVIL_RUN_ID_OVERRIDE env wins outright when set — the
            # calibration_runner (Step 6) injects the task-prefixed
            # shape (`T1-doc-edit`) so the harness's regex matches.
            # Resume keeps using the persisted state.run_id (which the
            # calibration_runner set on the original run).
            override = os.environ.get("ANVIL_RUN_ID_OVERRIDE", "").strip()
            if resumed_state is not None and resumed_state.run_id:
                run_id = resumed_state.run_id
            elif resumed_state is not None:
                # Legacy v1 state: no run_id persisted. Mint a fresh one
                # with a -resumed suffix and log the un-instrumented case.
                run_id = override or (
                    f"{datetime.now(_UK).strftime('%Y-%m-%d-%H%M')}"
                    f"-{_slug(brief.build_name)}-resumed"
                )
                log.info(
                    "[events] resume of un-instrumented run; new run_id="
                    f"{run_id}"
                )
            else:
                # Fresh run: env override wins; else same shape as the
                # _run_log filename slug.
                run_id = override or (
                    f"{datetime.now(_UK).strftime('%Y-%m-%d-%H%M')}"
                    f"-{_slug(brief.build_name)}"
                )
            _events.begin_run(run_id)

            _events.emit(
                "brief.parsed",
                {
                    "brief_name": brief.build_name,
                    "brief_path": str(brief_path),
                    "step_count": len(brief.steps),
                },
            )
            validate_or_reject(brief)  # raises BriefValidationError on bad brief
            _events.emit(
                "brief.validated",
                {"brief_name": brief.build_name, "step_count": len(brief.steps)},
            )
            # Finding 3 / decision #9: parse_brief leaves context_paths=[]
            # (only context_links populated). Stage A's vault index needs
            # the resolved paths, so resolve them before the step loop.
            # Unresolved links raise BriefValidationError (an AnvilError),
            # caught below — a brief defect surfaces, not a silent blind run.
            brief = resolve_context_paths(brief, self.config.vault_path)

            if resumed_state is not None:
                # Decision #15 fix (Phase 2 Step 2): on resume, reuse the
                # loaded state instead of clobbering it with init_state.
                # _plan_step's reuse-guard depends on state.steps[i].plan
                # being populated; init_state always sets plan=None and so
                # silently invalidated the guard on the resume path before.
                state = resumed_state
                state.run_id = run_id  # ensure set (covers legacy v1 case)
                self._state = state
                # Reopen the existing run log for append, if known.
                if state.run_log:
                    self._run_log = Path(state.run_log)
                _events.emit(
                    "run.resume",
                    {
                        "run_id": run_id,
                        "from_step": state.current_step,
                    },
                )
                self._log_event(
                    "resume", f"resumed at step {state.current_step}"
                )
                # The brief is already in active/ from the original run; do
                # not re-move it. transition() back to "running" so the
                # loop's status checks see a runnable state.
                state = transition(state, "running", pending_action=None)
                self._state = state
            else:
                started_at = datetime.now(_UK).isoformat(timespec="seconds")
                state = init_state(
                    brief, started_at, brief_path=str(brief_path),
                    coder_mode=self.coder_mode,
                )
                state.run_id = run_id
                self._state = state

                self._open_run_log(brief, started_at)
                state = transition(state, "running",
                                   run_log=str(self._run_log))
                self._state = state
                self._log_event(
                    "start", f"{len(brief.steps)} steps; manual mode"
                )

                self._move_brief(brief_path)

            for idx, bstep in enumerate(brief.steps):
                # Decision #15 fix (Phase 2 Step 2): skip steps already
                # marked done from a prior session. Without this, resume
                # re-executes completed steps with their persisted plans —
                # which is worse than re-planning. The reuse-guard alone
                # is not enough; we must not enter the step body at all.
                if state.steps[idx].status == "done":
                    continue
                state.steps[idx].status = "running"
                state.current_step = bstep.number
                state = transition(state, "running")
                self._state = state

                # v2 Phase 1 Step 2: emit step.start after the done-skip
                # guard so resumed builds don't re-emit step.start for
                # already-completed steps.
                _events.emit(
                    "step.start",
                    {
                        "step_idx": idx,
                        "step_number": bstep.number,
                        "step_name": bstep.name,
                    },
                    step_idx=idx,
                )

                result = self._plan_step(brief, state, idx)

                # Plan | escalation-dict split (design Part 4 / brief Step 6).
                # Detect escalation BEFORE any .step_name access.
                if isinstance(result, dict) and result.get("escalate"):
                    self._escalate(
                        state,
                        result.get("reason", "planner escalation"),
                        result.get("detail", ""),
                        result.get("options"),
                    )
                    if not self._await_user_decision(state):
                        return 1
                    # User chose to proceed past the escalation: the step
                    # cannot be executed without a plan, so skip it (the
                    # decision to continue is the decision not to run it).
                    state.steps[idx].status = "done"
                    state.steps[idx].commit = None
                    state = transition(state, "running")
                    self._state = state
                    continue

                plan = result
                self._log_event(
                    "planner", f"step {bstep.number}: {plan.step_name}"
                )

                # 5c Coder execution — branch on coder_mode.
                # Phase 2 Step 9: auto-mode invokes anvil.coder.Coder;
                # manual-mode is the Phase 0/1 flow, unchanged.
                if self.coder_mode == "auto":
                    coder_output = self.coder.execute_step(plan, brief)
                    state.steps[idx].coder_output = coder_output
                    self._state = state
                    write_state(state)
                    self._log_event(
                        "coder(auto)",
                        f"exit={coder_output.get('exit_code')} "
                        f"files={len(coder_output.get('files_touched') or [])} "
                        f"oos={len(coder_output.get('out_of_scope') or [])} "
                        f"dur={coder_output.get('duration_s', 0):.1f}s",
                    )
                    # Route post-Coder escalations.
                    if coder_output.get("escalate") is True:
                        self._escalate(
                            state,
                            coder_output.get("reason", "coder escalation"),
                            coder_output.get("detail", ""),
                            ("go", "abort"),
                        )
                        if not self._await_user_decision(state):
                            return 1
                        # User said go past the reconciliation failure.
                        # Skip the step (cannot execute without resolved
                        # paths); same posture as Planner escalation.
                        state.steps[idx].status = "done"
                        state.steps[idx].commit = None
                        state = transition(state, "running")
                        self._state = state
                        continue
                    if coder_output.get("out_of_scope"):
                        self._escalate(
                            state, "coder-out-of-scope",
                            "Files touched outside plan scope: "
                            + ", ".join(coder_output["out_of_scope"]),
                            ("go", "abort"),
                        )
                        if not self._await_user_decision(state):
                            return 1
                    if coder_output.get("exit_code", 0) != 0:
                        self._escalate(
                            state, "coder-failed",
                            (coder_output.get("stderr") or "")[:1500]
                            or "Coder exited non-zero with no stderr.",
                            ("go", "abort"),
                        )
                        if not self._await_user_decision(state):
                            return 1
                else:
                    # 5c manual-Coder execution (Phase 0/1 flow).
                    outcome = self._manual_step(plan)
                    self._log_event("coder(manual)", f"reply={outcome}")
                    if outcome == "abort":
                        state = transition(state, "aborted")
                        return 1
                    if outcome == "skip":
                        state.steps[idx].status = "done"
                        state.steps[idx].commit = None
                        state = transition(state, "running")
                        continue

                # 5d smoke — v2 Phase 1 Step 2 emit pair wraps the
                # injected indirection (notes.md Finding 1 constraint
                # 3: instrumentation lands at the call site, not
                # inside _default_run_smoke, preserving the test seam).
                _events.emit(
                    "smoke.start",
                    {"step_idx": idx, "command": bstep.smoke},
                    step_idx=idx,
                )
                _smoke_t0 = time.monotonic()
                ok, smoke_out = self._run_smoke(bstep.smoke, brief.target_repo_path)
                _events.emit(
                    "smoke.end",
                    {
                        "step_idx": idx,
                        "command": bstep.smoke,
                        "duration_ms": int((time.monotonic() - _smoke_t0) * 1000),
                        "ok": bool(ok),
                        "output_chars": len(smoke_out or ""),
                    },
                    step_idx=idx,
                )
                state.steps[idx].smoke = "pass" if ok else "fail"
                state.steps[idx].smoke_output = smoke_out[:1000]
                self._log_event("smoke", f"step {bstep.number}: "
                                f"{'pass' if ok else 'FAIL'}")
                if not ok:
                    self._escalate(
                        state, "smoke test failed",
                        smoke_out, ("go", "abort"),
                    )
                    if not self._await_user_decision(state):
                        return 1

                # 5e commit (orchestrator owns it so the footer is canonical)
                commit_hash = self.git.commit_step(
                    brief.target_repo_path, plan, idx,
                    brief_name=brief.build_name,
                    commit_message_hint=bstep.commit_message_hint,
                    run_log_filename=Path(self._run_log).name,
                )
                # Phase 2 Step 9 (decisions #14/17): if commit_step
                # was a no-op (manual mode: Genco committed in his own
                # Claude Code session, ANVIL's `git add -A` found
                # nothing), fall back to head_hash so the state
                # records the attribution that exists in the git log.
                # Design Part 3 §"Manual mode preserved": "The state
                # still records the head commit hash via
                # `git rev-parse HEAD` so attribution holds either way."
                state.steps[idx].commit = (
                    commit_hash
                    or self.git.head_hash(brief.target_repo_path)
                )
                state.steps[idx].status = "done"
                state = transition(state, "running")
                self._state = state
                self._log_event("commit", commit_hash or "(no-op: commit null)")

                # 5f confirm
                if bstep.confirm == "explicit":
                    msg = voice.format_step_completion(
                        state, plan, commit_hash, state.steps[idx].smoke
                    )
                    self.telegram.send(msg)
                    pa = PendingAction(
                        type="step_confirmation",
                        sent_at=datetime.now(_UK).isoformat(timespec="seconds"),
                        expected_reply="go",
                    )
                    state = transition(state, "waiting", pending_action=pa)
                    self._state = state
                    reply = self.telegram.wait_for_reply(timeout=None)
                    text = (reply.text.strip().lower() if reply else "")
                    if text == "go":
                        state = transition(state, "running", pending_action=None)
                        self._state = state
                    else:
                        self._log_event("pause", f"reply={text!r}")
                        state = transition(state, "paused-by-user")
                        self._state = state
                        return 1
                # confirm == "auto" → fall through, no Telegram round-trip
                self._log_event("step-done", f"step {bstep.number}")
                # v2 Phase 1 Step 2: emit step.end at the natural
                # fall-through. Steps that returned early via escalation
                # or pause do not emit step.end — the step did not
                # complete and the events.jsonl reflects that asymmetry.
                _events.emit(
                    "step.end",
                    {
                        "step_idx": idx,
                        "status": state.steps[idx].status,
                        "commit": state.steps[idx].commit,
                    },
                    step_idx=idx,
                )

            # ---------------------------------------------------------------
            # 6: end-to-end test (Phase 3 Step 5)
            # ---------------------------------------------------------------
            # Detect Mac-resident vs VPS-resident. For VPS-resident e2e the
            # ordering flips to post-deploy (design 2.7 (ii)): pre-deploy e2e
            # against a VPS-resident script measures the prior deploy, which
            # is irrelevant to the new commits being deployed.
            e2e_runs_on = None
            if brief.end_to_end_test and state.status == "running":
                e2e_runs_on = self._detect_e2e_location(brief)
                self._e2e_runs_on = e2e_runs_on  # cache for post-deploy branch
                if e2e_runs_on == "not-found":
                    self._escalate(
                        state, "e2e-script-not-found",
                        f"{brief.end_to_end_test.script} not at Mac or VPS path",
                        options=("abort",),
                    )
                    return 1
                if e2e_runs_on == "mac":
                    # Pre-deploy gate ordering (master design Part 6 nominal)
                    e2e_ok, e2e_out = self._run_e2e_mac(brief)
                    if not e2e_ok:
                        self._escalate(
                            state, "e2e-failed", e2e_out, options=("go", "abort"),
                        )
                        if state.status == "aborted":
                            return 1
                # vps-resident: defer e2e to post-deploy below

            # ---------------------------------------------------------------
            # 7: deploy (Phase 3 Step 5)
            # ---------------------------------------------------------------
            if brief.vps_deploy == "yes" and state.status == "running":
                # Pre-check config
                if self.config.vps_host is None:
                    self._escalate(
                        state, "deploy-config-missing",
                        "VPS_HOST not set in .env; required for vps_deploy: yes briefs",
                        options=("abort",),
                    )
                    return 1

                deploy_result = ssh_ops.deploy(brief, self.config)
                state.deploy = deploy_result
                self._state = state
                from anvil.state import write_state as _write_state
                _write_state(state)
                self._log_event("deploy", f"stage={deploy_result['stage']} ok={deploy_result['ok']}")

                if not deploy_result["ok"]:
                    stage = deploy_result["stage"]
                    reason = f"deploy-{stage}-failed"
                    self._escalate(
                        state, reason, deploy_result["output"],
                        options=("go", "abort"),
                    )
                    if state.status == "aborted":
                        return 1
                    # "go" past a deploy escalation: full deploy retry from scratch.
                    # The orchestrator does not silently retry — the escalation
                    # required Genco confirmation. Re-enter handle_brief with the
                    # current state so the deploy block runs fresh.
                    return self.handle_brief(brief_path, resumed_state=state)

            # Post-deploy e2e for VPS-resident case
            if (brief.end_to_end_test and state.status == "running"
                    and brief.vps_deploy == "yes"
                    and getattr(self, "_e2e_runs_on", None) == "vps"):
                e2e_ok, e2e_out = self._run_e2e_vps(brief)
                if not e2e_ok:
                    self._escalate(
                        state, "deploy-e2e-failed", e2e_out, options=("go", "abort"),
                    )
                    if state.status == "aborted":
                        return 1

            # 8 wrap
            state.finished_at = datetime.now(_UK).isoformat(timespec="seconds")
            state = transition(state, "done")
            self._state = state
            self._log_event("complete", f"status={state.status}")
            self._archive_brief(brief_path, brief, state)
            self.telegram.send(voice.format_completion(brief, state))
            # Phase 4 Step 5: step 9 — draft + confirm + write artefacts.
            # Wrapped in try/except for never-raise contract; on any
            # unexpected exception, log and continue (build is done).
            try:
                self._draft_and_confirm_artefacts(brief, brief_path, state)
            except Exception as e:  # noqa: BLE001 — never-raise
                log.error(
                    f"unexpected in _draft_and_confirm_artefacts: {e}",
                    exc_info=True,
                )
            return 0

        except NotImplementedError:
            # Deliberate "not built in Phase 0" signal — observable, not a
            # runtime failure the never-raise contract is meant to absorb.
            raise
        except AnvilError as e:
            log.error(f"AnvilError in handle_brief: {e}")
            try:
                self._log_event("error", str(e)[:300])
            except Exception:  # noqa: BLE001
                pass
            return 1
        except Exception as e:  # noqa: BLE001 — never-raise
            log.error(f"unexpected in handle_brief: {e}", exc_info=True)
            return 2
        finally:
            # v2 Phase 1 Step 2: events.end_run() fires on every exit
            # path. No-op if begin_run never ran. Wrapped in try/except
            # so an events-layer failure cannot replace the real return
            # value with a raise.
            try:
                _events.end_run()
            except Exception:  # noqa: BLE001 — never-raise
                pass

    # ---- e2e + deploy (Phase 3 Step 5) ----
    def _detect_e2e_location(self, brief) -> str:
        """Return 'mac' | 'vps' | 'not-found' for brief.end_to_end_test.script.

        Convention-based heuristic (no new brief field):
        - vps_deploy: yes AND script lives under eval/ AND vps_target_path set:
          classify VPS-resident (Phase 3 exit test shape — post-deploy smoke).
        - Else if script exists at target_repo_path/script: Mac-resident.
        - Else if vps_deploy: yes: best-effort VPS probe (test -e on VPS path).
        - Else not-found.

        The eval/-path convention is narrow enough to not surprise Mac-side
        builds and broad enough to cover the Phase 3 exit-test smoke. If a
        future brief needs a different convention, a runs_on field is the
        upgrade path.
        """
        script = brief.end_to_end_test.script
        if (brief.vps_deploy == "yes"
                and brief.vps_target_path
                and script.startswith("eval/")):
            return "vps"
        mac_path = Path(brief.target_repo_path) / script
        if mac_path.exists():
            return "mac"
        if brief.vps_deploy == "yes" and brief.vps_target_path and self.config.vps_host:
            # Best-effort VPS probe; if SSH fails or path missing, treat as not-found
            probe_cmd = f"test -e {brief.vps_target_path}/{script}"
            ok, _ = ssh_ops.ssh_run(
                self.config.vps_host, self.config.vps_user, probe_cmd, timeout=15,
            )
            if ok:
                return "vps"
        return "not-found"

    def _run_e2e_mac(self, brief) -> tuple[bool, str]:
        """Run a Mac-resident e2e script. Returns (ok, output). Never raises."""
        import subprocess
        script_path = Path(brief.target_repo_path) / brief.end_to_end_test.script
        try:
            r = subprocess.run(
                [str(script_path)],
                cwd=str(brief.target_repo_path),
                capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired as e:
            return (False, f"TimeoutExpired(600s): {e!r}")
        except Exception as e:  # noqa: BLE001
            return (False, repr(e))
        output = (r.stdout or "") + (r.stderr or "")
        expected = brief.end_to_end_test.expected_exit
        return (r.returncode == expected, output)

    def _run_e2e_vps(self, brief) -> tuple[bool, str]:
        """Run a VPS-resident e2e script via SSH. Returns (ok, output). Never raises."""
        cmd = f"cd {brief.vps_target_path} && bash {brief.end_to_end_test.script}"
        ok, output = ssh_ops.ssh_run(
            self.config.vps_host, self.config.vps_user, cmd, timeout=600,
        )
        return (ok, output)

    # ---- planning (resume-reuse guard + persist) ----
    def _plan_step(self, brief, state, idx: int):
        """Returns Plan | escalation-dict. Resume-reuse: if
        state.steps[idx].plan is already set, reconstruct from it without
        calling the Planner (escalation dict passes through). Step 7
        landed the schema (StepState.plan, schema_version=2), so the
        reuse path is live on resume; a legacy v1 state loads with
        .plan=None on every step and falls through to the Planner.

        On a fresh plan, persist immediately via the atomic write_state
        contract (the brief's `_write_state()` shorthand) so a crash
        between planning and execution does not lose the plan.
        result.model_dump() not result.dict() — pydantic v2, decision #5.
        """
        existing = state.steps[idx].plan
        if existing is not None:
            if existing.get("escalate"):
                log.info(
                    f"[planner] reusing persisted escalation, step {idx + 1}"
                )
                return existing
            log.info(f"[planner] reusing persisted plan, step {idx + 1}")
            return Plan(**existing)
        result = self.planner.plan_step(brief, state, idx)
        state.steps[idx].plan = (
            result.model_dump() if isinstance(result, Plan) else result
        )
        write_state(state)
        self._state = state
        return result

    # ---- manual coder ----
    def _manual_step(self, plan) -> str:
        """Send the plan to Genco; parse reply: 'done'/'done <hash>' →
        'done', 'skip' → 'skip', 'abort' → 'abort'. Anything else → 'abort'
        (safe default — don't proceed on an unrecognised manual reply)."""
        self.telegram.send(
            f"[ANVIL] Step {plan.step_number} — execute in Claude Code, then "
            f"reply 'done' (or 'skip' / 'abort').\n"
            f"Plan: {plan.approach[:300]}\n"
            f"Files: {', '.join(plan.files_to_touch)}\n"
            f"Smoke: {plan.smoke_test}"
        )
        reply = self.telegram.wait_for_reply(timeout=None)
        text = (reply.text.strip().lower() if reply else "")
        if text.startswith("done"):
            return "done"
        if text == "skip":
            return "skip"
        return "abort"

    # ---- escalation ----
    # Decision #19 (Phase 2 Step 3): the `options` argument is now a
    # tuple of literal command tokens — ('go', 'abort') in the common
    # case, ('abort',) when proceeding past the escalation makes no
    # sense (e.g. planner-validation-failure). Planner-self-emitted
    # `options` (a list of descriptive prose like 'amend brief to widen
    # scope') are rendered as numbered context; the *grammar* the user
    # replies with stays ('go', 'abort'). Source-compatible: a legacy
    # string-shaped options arg still works (rendered as-is, grammar
    # falls back to ('go', 'abort')) but emits a one-time warning.
    def _escalate(self, state, reason, detail, options=("go", "abort")) -> None:
        # Phase 4 Step 5: tick escalation_count for the wrap-time
        # outcome suffix decision in checkpoint.derive_checkpoint_path.
        # The or-0 guard handles legacy v1 state (escalation_count
        # absent, defaulted by pydantic to 0 but defensive anyway).
        state.escalation_count = (state.escalation_count or 0) + 1
        write_state(state)
        prose_lines: list[str] = []
        if isinstance(options, (list, tuple)) and options and all(
            isinstance(o, str) for o in options
        ):
            # If every element looks like a single short token, treat as
            # the grammar tuple. Otherwise treat as descriptive prose.
            grammar = tuple(o.strip().lower() for o in options)
            looks_like_tokens = all(
                len(o) <= 16 and " " not in o for o in grammar
            )
            if looks_like_tokens:
                display = " / ".join(grammar)
            else:
                # Descriptive prose options from the Planner. Render as
                # numbered list; grammar is the standard go/abort pair.
                prose_lines = [
                    f"  {i + 1}. {opt}" for i, opt in enumerate(options)
                ]
                grammar = ("go", "abort")
                display = "go / abort"
        elif isinstance(options, str):
            # Legacy: a single string. Honour the contract but warn so
            # remaining call sites get migrated.
            log.warning(
                "_escalate received legacy string options=%r; "
                "call sites should pass a tuple of literal tokens.",
                options,
            )
            grammar = ("go", "abort")
            display = options
        else:
            grammar = ("go", "abort")
            display = "go / abort"

        if prose_lines:
            detail_with_options = (
                f"{detail}\n\nPlanner suggests:\n"
                + "\n".join(prose_lines)
                + "\n\nReply: " + display
            )
        else:
            detail_with_options = detail

        # v2 Phase 1 Step 2: emit escalation.raised before the telegram
        # send and stash the monotonic baseline so the paired
        # escalation.resolved in _await_user_decision can compute the
        # round-trip latency_ms_user.
        _step_idx_evt = (
            (state.current_step - 1) if isinstance(state.current_step, int)
            else None
        )
        _events.emit(
            "escalation.raised",
            {
                "step_idx": _step_idx_evt,
                "reason": reason,
                "detail": (detail or "")[:500],
                "options": list(grammar),
            },
            step_idx=_step_idx_evt,
        )
        self._pending_escalation_sent_at = time.monotonic()

        self.telegram.send(
            voice.format_escalation(state, reason, detail_with_options, display)
        )
        self._log_event("escalation", reason)
        # Remembered for the immediately-following _await_user_decision.
        self._pending_options = grammar

    def _await_user_decision(self, state) -> bool:
        """Return True to proceed, False if the user aborts/pauses.

        Decision #19 (Phase 2 Step 3): the accepted-token set is now
        whatever the most recent _escalate stored on self._pending_options.
        The previous hardcoded ('go', 'continue', 'proceed') set is retired;
        the user-facing options line now lists literal command tokens
        that match the grammar exactly. The grammar always includes
        'abort' as the abort path.
        """
        options = getattr(self, "_pending_options", ("go", "abort"))
        reply = self.telegram.wait_for_reply(timeout=None)
        text = (reply.text.strip().lower() if reply else "")
        # v2 Phase 1 Step 2: emit escalation.resolved with the
        # wall-clock latency_ms_user (paired against the monotonic
        # baseline stashed by _escalate). Cleared after read.
        sent_at = getattr(self, "_pending_escalation_sent_at", None)
        latency_ms = (
            int((time.monotonic() - sent_at) * 1000) if sent_at is not None
            else None
        )
        _step_idx_evt = (
            (state.current_step - 1) if isinstance(state.current_step, int)
            else None
        )
        _events.emit(
            "escalation.resolved",
            {
                "step_idx": _step_idx_evt,
                "reply": text,
                "latency_ms_user": latency_ms,
            },
            step_idx=_step_idx_evt,
        )
        self._pending_escalation_sent_at = None
        # An empty/missing reply is treated as paused-by-user, same as
        # any other non-matching reply.
        if text and text != "abort" and text in options:
            return True
        transition(state, "aborted" if text == "abort" else "paused-by-user")
        return False

    # ---- brief movement ----
    def _move_brief(self, brief_path: Path) -> None:
        """Move inbox/<brief> → active/<brief> and, crucially, update
        state.brief_path to the new location and persist it. Without this,
        resume() would re-parse the now-vacated inbox path and fail
        (caught by Step 10 pre-flight: resume-broken-by-move)."""
        try:
            if brief_path.parent.name == "inbox":
                active = brief_path.parent.parent / "active"
                active.mkdir(parents=True, exist_ok=True)
                new_path = active / brief_path.name
                shutil.move(str(brief_path), str(new_path))
                if self._state is not None:
                    self._state.brief_path = str(new_path)
                    write_state(self._state)
                self._log_event("brief", f"moved to active/{brief_path.name}; "
                                f"state.brief_path → {new_path}")
        except Exception as e:  # noqa: BLE001 — non-fatal
            log.warning(f"move_brief skipped ({e})")

    def _archive_brief(self, brief_path: Path, brief, state) -> None:
        try:
            src = brief_path
            active = state_dir().parent / "active" / brief_path.name
            if active.exists():
                src = active
            if not src.exists():
                return
            month = datetime.now(_UK).strftime("%Y-%m")
            dest = state_dir().parent / "archive" / month
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(
                str(src),
                str(dest / f"{brief_path.stem}-{state.status}.md"),
            )
        except Exception as e:  # noqa: BLE001 — non-fatal
            log.warning(f"archive_brief skipped ({e})")

    # ---- artefact drafting (Phase 4 Step 5) ----
    def _draft_and_confirm_artefacts(
        self, brief, brief_path: Path, state
    ) -> None:
        """Step 9: draft setup-log entry + checkpoint via Planner,
        Telegram-preview them, write on go, defer-to-manual on abort.

        Never raises (caller still wraps for belt-and-braces). All
        failure paths route through _escalate with (go, abort) or
        (abort,) grammar. setup-log-path-not-found is abort-only
        (config can\'t be fixed mid-run); all other failures offer
        go to mark the build done with paperwork deferred.
        """
        # 1. Derive setup-log path; soft-skip if missing.
        # Phase 4 Step 5b: a missing setup-log is a pre-flight condition
        # (brief in non-standard location, or test fixture), not an active
        # failure. Log + return rather than escalate — matches the
        # proportionality of the existing idempotent-skip pattern
        # (checkpoint exists → log + skip). Active failures (draft-failed,
        # write-failed) stay as escalations.
        setup_log_path = _vault_ops.derive_setup_log_path(brief_path)
        if not setup_log_path.is_file():
            log.info(
                f"[checkpoint] setup-log path does not exist; "
                f"skipping artefact writes: {setup_log_path}"
            )
            self._log_event(
                "artefacts",
                f"skipped (setup-log not at {setup_log_path})",
            )
            return

        # 2. Derive checkpoint path; skip writes idempotently if it exists
        checkpoint_path = _checkpoint.derive_checkpoint_path(
            brief, state, self.config.vault_path,
        )
        if checkpoint_path.exists():
            log.info(
                f"[checkpoint] exists; skipping artefact writes: "
                f"{checkpoint_path}"
            )
            self._log_event(
                "artefacts", f"skipped (checkpoint exists: {checkpoint_path.name})"
            )
            return

        # 3. Call Planner; route escalation on draft failure
        draft, err = _checkpoint.draft_and_preview(
            brief, state, self.planner,
        )
        if draft is None:
            self._escalate(
                state, "completion-artefacts-draft-failed", err or "draft failed",
                options=("go", "abort"),
            )
            proceed = self._await_user_decision(state)
            if not proceed:
                # User chose abort; state already aborted by _await
                return
            # User chose go: skip writes, mark deferred-to-manual
            self._log_event("artefacts", "draft failed; deferred to manual")
            return

        # 4. Telegram preview + go/abort gate (plain wait, not escalation)
        preview = voice.format_artefact_preview(
            draft, setup_log_path, checkpoint_path,
        )
        self.telegram.send(preview)
        pa = PendingAction(
            type="artefact_confirmation",
            sent_at=datetime.now(_UK).isoformat(timespec="seconds"),
            expected_reply="go",
        )
        state.pending_action = pa
        write_state(state)
        reply = self.telegram.wait_for_reply(timeout=None)
        text = (reply.text.strip().lower() if reply else "")
        state.pending_action = None
        write_state(state)

        if text != "go":
            # Abort or any non-go reply → defer to manual
            self._log_event("artefacts", f"deferred to manual (reply={text!r})")
            return

        # 5. Execute writes; route escalation on failure
        frontmatter = _checkpoint.compose_checkpoint_frontmatter(
            brief, state,
            git_commit=self.git.head_hash(brief.target_repo_path),
        )
        ok, write_err = _checkpoint.execute_writes(
            draft, setup_log_path, checkpoint_path, frontmatter,
        )
        if not ok:
            # Discriminate checkpoint-write vs generic vault-write failure
            reason = (
                "checkpoint-write-failed"
                if "checkpoint" in (write_err or "").lower()
                else "vault-write-failed"
            )
            self._escalate(
                state, reason, write_err or "vault write failed",
                options=("go", "abort"),
            )
            self._await_user_decision(state)
            return

        # 6. Happy path — both writes succeeded
        # Phase 4 Step 6: persist outcome to state so format_completion
        # can render the Vault writes block and the harness Capture
        # parser can read it from anvil.log markers.
        state.vault_writes_outcome = {
            "setup_log_path": str(setup_log_path),
            "checkpoint_path": str(checkpoint_path),
            "ok": True,
            "error": None,
        }
        write_state(state)
        self._log_event(
            "artefacts",
            f"wrote {setup_log_path.name} + {checkpoint_path.name}",
        )
