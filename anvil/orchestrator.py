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
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from anvil import git_ops as _git_ops
from anvil.brief import parse_brief, resolve_context_paths, validate_or_reject
from anvil.coder import Coder
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
        coder_mode: str = "manual",   # Phase 0 hardcodes manual
        planner=None,
        telegram=None,
        git=None,
        run_smoke=None,
        coder=None,
    ) -> None:
        self.config = config
        self.coder_mode = coder_mode
        self.planner = planner if planner is not None else Planner(
            api_key=config.anthropic_api_key,
            model=config.planner_model,
            timeout=config.planner_timeout,
            vault_root=config.vault_path,
        )
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
        elif coder_mode == "auto":
            self.coder = self._build_coder()
        else:
            self.coder = None
        self._run_log: Path | None = None
        self._state = None

    def _build_coder(self) -> Coder:
        """Construct a real Coder from config. The system prompt is
        coder-system.md with {VOICE_SPEC} substituted. claude_binary
        defaults to whatever `claude` resolves to on PATH at startup,
        overridable via CLAUDE_BINARY in .env. Coder timeout reuses
        config.coder_timeout (already present since Phase 0).
        """
        prompt_path = Path(__file__).resolve().parent / "prompts" / "coder-system.md"
        prompt_text = prompt_path.read_text(encoding="utf-8")
        prompt_text = prompt_text.replace("{VOICE_SPEC}", self.voice_spec)
        binary = Path(
            getattr(self.config, "claude_binary", None)
            or shutil.which("claude")
            or "claude"
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
        try:
            if self.coder_mode == "auto":
                # Phase 2 Step 9 wires this path through the step loop;
                # no-op here. The auto branch in step 5c does the work.
                pass

            brief = parse_brief(brief_path)
            validate_or_reject(brief)  # raises BriefValidationError on bad brief
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
                self._state = state
                # Reopen the existing run log for append, if known.
                if state.run_log:
                    self._run_log = Path(state.run_log)
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
                    coder_mode="manual",
                )
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

                # 5d smoke
                ok, smoke_out = self._run_smoke(bstep.smoke, brief.target_repo_path)
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

            # 8 wrap (no e2e/deploy: trivial brief declares neither)
            state.finished_at = datetime.now(_UK).isoformat(timespec="seconds")
            state = transition(state, "done")
            self._state = state
            self._log_event("complete", f"status={state.status}")
            self._archive_brief(brief_path, brief, state)
            self.telegram.send(voice.format_completion(brief, state))
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
