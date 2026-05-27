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
from anvil.brief import (
    parse_brief, resolve_context_paths, validate_or_reject, _observe_scheme,
)
from anvil.lint import lint_brief
from anvil.coder import Coder
from anvil import ssh_ops
# v4 Phase 2c Step 1: the observe sub-phase consumes the Phase 2a substrate
# (browser substrate + visibility-session state writer). Module imports (the
# `from anvil import ssh_ops` style) so the mock-patch target is
# `anvil.orchestrator.browser` / `anvil.orchestrator.visibility_session`.
from anvil.integrations import browser, visibility_session
# v4 Phase 3c Step 1: the screen-aware observe sub-phase dispatches the screen://
# (native) and tab:// (extension) schemes to the Phase 3a substrate wrappers
# (module imports so the mock-patch target is `anvil.orchestrator.screen_capture`
# / `anvil.orchestrator.screen_browser`).
from anvil.integrations import screen_capture, screen_browser
# v4 Phase 2c Step 2: the observe-loop's first real consumer of the Phase 1a
# seam (the Haiku-routed observation digest). Module import so the mock-patch
# target is `anvil.orchestrator.routing.call_model_for_subtask`.
from anvil import routing
# v2 Phase 1 Step 6: `anvil.checkpoint` does `from anvil.orchestrator
# import _slug` at module load. Importing checkpoint eagerly here
# triggers the circular load when this module is the entry point
# (e.g. `python -m anvil.cli run`). Defer to inside
# `_draft_and_confirm_artefacts` — the only call site. Tests that
# load checkpoint via a separate test module work because the other
# test pre-loads checkpoint, which then triggers orchestrator's full
# initialization before the cycle hits.
# Recorded in notes.md Step 2 outcome / pre-existing issues; v2 Phase 2
# cleanup target is to extract `_slug` to anvil/util.py and end the
# cycle outright.
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


def _calibration_auto_reply_log(site: str, reply: str, step_idx) -> None:
    """v2 Phase 1 Step 7 prep: telemetry for AUTO_REPLY_FOR_CALIBRATION
    short-circuits. Gated on `CALIBRATION_TELEGRAM_PREFIX` being set so
    the line only appears during calibration sweeps, not in production.

    Surfaces via anvil.log (file handler wired by cli.py:_setup_logging)
    AND via stderr (so calibration_runner's captured-stderr can echo it
    back to the operator's terminal). Direct stderr write rather than a
    logger.StreamHandler so we don't perturb the global anvil.* logger
    config — `[planner]` and `[coder]` lines remain file-only.
    """
    if not os.environ.get("CALIBRATION_TELEGRAM_PREFIX", "").strip():
        return
    step_token = step_idx if step_idx is not None else "run-level"
    line = (
        f"[calibration] auto-replied {reply!r} at {site} (step={step_token})"
    )
    log.info(line)
    import sys as _sys
    print(line, file=_sys.stderr, flush=True)


def _slug(build_name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (build_name or "").lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "build"


# v4 Phase 2c Step 2 (Q-F5): the digest-interpretation system prompt the observe
# sub-phase sends to Haiku via call_model_for_subtask. Plain string (no
# cache_control — the seam's Q-A3). Asks for a Planner-readable digest.
DIGEST_SYSTEM_PROMPT = (
    "You interpret browser observations from a build step. Given DOM/console/"
    "network observations, produce a concise digest — a short paragraph plus a "
    "bullet list of notable findings (console errors, network failures, "
    "anomalies) — that the Planner reads on the next iteration. Be terse; if "
    "nothing is notable, say so in one line."
)


def _build_observation_summary(observations: dict) -> str:
    """Render the observations as a cost-shaped text summary for the Haiku
    digest (Q-F5): console + network entries verbatim, but the DOM as a
    SUMMARY (length only — NOT the full HTML body, which is token-heavy and
    defeats the cost-shaping). Module-level so it is not re-created per step."""
    parts: list[str] = []
    dom = observations.get("dom")
    if dom is not None:
        html = dom.get("html", "") if isinstance(dom, dict) else str(dom)
        parts.append(f"DOM: {len(html)} chars (body omitted from digest input)")
    console = observations.get("console")
    if console is not None:
        entries = console.get("entries", []) if isinstance(console, dict) else []
        parts.append(f"Console: {len(entries)} entries")
        for e in entries:
            parts.append(f"  [{e.get('type')}] {e.get('text')}")
    network = observations.get("network")
    if network is not None:
        entries = network.get("entries", []) if isinstance(network, dict) else []
        parts.append(f"Network: {len(entries)} responses")
        for e in entries:
            parts.append(f"  {e.get('status')} {e.get('url')}")
    return "\n".join(parts) if parts else "(no observations captured)"


# v4 Phase 3c Step 1: the vision-interpretation system prompt the screen-aware
# observe sub-phase sends to Sonnet via call_model_for_subtask (the seam's FIRST
# vision consumer, BAF-1). Plain string (no cache_control — the seam's Q-A3). Asks
# for a Planner-readable digest of the screen frame + accessibility tree. The
# "describe only what is visible; do not assert correctness" clause holds the
# Q-C0 no-equivalence-claim line: this captures + feeds back, it does not grade.
VISION_SYSTEM_PROMPT = (
    "You interpret a screenshot of a build step's running UI, plus an optional "
    "accessibility-tree summary. Produce a concise digest — a short paragraph "
    "plus a bullet list of notable findings (visible errors, broken layout, "
    "unexpected state) — that the Planner reads on the next iteration. Be terse; "
    "if nothing is notable, say so in one line. Describe only what is visible; do "
    "not assert correctness or equivalence to any expected output."
)


def _build_screen_summary(observations: dict) -> str:
    """Render the screen observations as the TEXT half of the Sonnet vision call
    (Phase 3c Step 1) — the frame itself rides as the image content block, so it
    is noted by size only (the pixels are the image, not the text; cost-shaping).
    The accessibility tree is summarized verbatim (role/label per element).
    Module-level so it is not re-created per step (the _build_observation_summary
    precedent)."""
    parts: list[str] = []
    frame = observations.get("frame")
    if frame is not None and isinstance(frame, dict):
        parts.append(
            f"Frame: {frame.get('width')}x{frame.get('height')} "
            "(the screenshot is attached as the image)"
        )
    ax = observations.get("accessibility")
    if ax is not None and isinstance(ax, dict):
        elements = ax.get("elements", [])
        parts.append(f"Accessibility: {len(elements)} top-level elements")
        for e in elements:
            parts.append(f"  [{e.get('role')}] {e.get('label')}")
    return "\n".join(parts) if parts else "(no screen observations captured)"


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

        v3 Phase 1b Step 2: the routing policy is selected by
        `_build_routing_policy` from the `ANVIL_CALIBRATION_DB` env var
        (opt-in). Unset → policy=None → Planner defaults to
        PHASE_1A_PLACEHOLDER (Phase-1a-equivalent). Set → the Stage A shadow
        rule. The policy is passed to both the real and mocked Planner.
        """
        policy = self._build_routing_policy()
        historical_baseline = self._build_historical_baseline()
        if getattr(self.config, "mocked_planner", False):
            from anvil.mocked import MockedPlanner
            return MockedPlanner(
                api_key=self.config.anthropic_api_key,
                model=self.config.planner_model,
                timeout=self.config.planner_timeout,
                vault_root=self.config.vault_path,
                policy=policy,
                historical_baseline=historical_baseline,
            )
        return Planner(
            api_key=self.config.anthropic_api_key,
            model=self.config.planner_model,
            timeout=self.config.planner_timeout,
            vault_root=self.config.vault_path,
            policy=policy,
            historical_baseline=historical_baseline,
        )

    def _build_historical_baseline(self):
        """v3 Phase 1c Step 3 (V3P1C-3): construct the comparator option-(b)
        baseline provider from `ANVIL_HISTORICAL_BASELINE_DB` (opt-in). Unset →
        None → the canary uses the live parallel-Opus baseline (Phase
        1b-equivalent). The provider itself never-raises on a missing/broken
        DB, so a bad path degrades to all-parallel rather than failing."""
        import os
        from anvil.planner import HistoricalBaselineProvider
        db = os.environ.get("ANVIL_HISTORICAL_BASELINE_DB", "").strip()
        if not db:
            return None
        return HistoricalBaselineProvider(db)

    def _build_routing_policy(self):
        """Select the routing policy from the opt-in calibration env vars.

        v3 Phase 1b Step 2 + 3 — three-way selection:
        - `ANVIL_CALIBRATION_DB` unset → None (Planner defaults to
          PHASE_1A_PLACEHOLDER; a default sweep is byte-identical to Phase 1a).
        - set, and the current task (`ANVIL_CURRENT_TASK`, set per-subprocess
          by the calibration runner) is in the `ANVIL_CANARY_TASKS` allowlist
          → PHASE_1B_STAGE_A_CANARY (the canary ACTS — the API runs the cheap
          model on empty-context Stage A).
        - set, but the task is not allowlisted → PHASE_1B_STAGE_A_SHADOW
          (route_candidate diverges, the API still runs Opus).

        v3 Phase 2d (bootstrap mechanism — Step 1, option (ii)): the recipe for
        producing an all-Opus Stage A baseline corpus is `ANVIL_CALIBRATION_DB`
        set + `ANVIL_CANARY_TASKS=""` (empty allowlist). With an empty allowlist
        no task matches, so EVERY task takes the shadow branch → Opus Stage A
        with `selected_paths` recorded. No new env var — the shadow branch
        already runs Opus; the empty allowlist is the only knob. The live
        bootstrap sweep and the `ANVIL_HISTORICAL_BASELINE_DB` repoint are
        deferred to Phase 2d2 (on T1-T6's uniformly empty-context corpus the
        Opus baseline is `[]`, indistinguishable from the existing canary
        sweep; the spend is deferred until corpus extension makes it carry
        information). Phase 2d2 also writes the rich-context derivation:
        `RoutingCalibration.from_db` currently derives the empty-context gate
        only (it reads `paths_returned`, not `selected_paths`). See the Phase 2d
        brief and the v3 planning context (Revision L).

        `RoutingCalibration.from_db` is never-raise (a missing/broken DB →
        empty corpus → degraded predicate), so a misconfigured path can never
        block the build — it just recommends Opus everywhere.
        """
        cal_db = os.environ.get("ANVIL_CALIBRATION_DB", "").strip()
        if not cal_db:
            return None
        from anvil.calibration import RoutingCalibration
        from anvil.policy import (
            PHASE_1B_STAGE_A_CANARY,
            PHASE_1B_STAGE_A_SHADOW,
            RoutingPolicy,
        )
        calibration = RoutingCalibration.from_db(cal_db).policy
        canary_tasks = {
            t.strip()
            for t in os.environ.get("ANVIL_CANARY_TASKS", "").split(",")
            if t.strip()
        }
        current_task = os.environ.get("ANVIL_CURRENT_TASK", "").strip()
        if current_task and current_task in canary_tasks:
            return RoutingPolicy(PHASE_1B_STAGE_A_CANARY, calibration=calibration)
        return RoutingPolicy(PHASE_1B_STAGE_A_SHADOW, calibration=calibration)

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

    # ---- v5 Phase 1c: the mode-guard reverse channel (Mac -> VPS SSH) ----
    def _mode_guard_mark_active(self, run_id: str, brief_path) -> None:
        """SSH-write running_builds=active on the VPS at build start (Q-C1 —
        ssh_ops.ssh_run, NOT Telegram; the 409 holds). Best-effort + never-
        raises: gated on config.mode_guard + a configured vps_host, off for
        sweeps/local runs. A failed SSH-write logs + continues (the monitor's
        fail-closed staleness covers a stale active row; the build is never
        blocked by the mode-guard's own bookkeeping)."""
        if not getattr(self.config, "mode_guard", False) or not self.config.vps_host:
            return
        try:
            import shlex
            path = shlex.quote(self.config.vps_monitor_path)
            cmd = (
                f"cd {path} && PYTHONPATH={path} "
                f"ANVIL_OPS_DB_PATH={shlex.quote(self.config.vps_ops_db)} "
                f"/usr/bin/python3 -m anvil.monitor.running_builds mark-active "
                f"{shlex.quote(run_id)} {shlex.quote(str(brief_path))}"
            )
            ok, out = ssh_ops.ssh_run(self.config.vps_host, self.config.vps_user, cmd, timeout=20)
            self._mode_guard_run_id = run_id  # remember for the completion write
            if not ok:
                log.warning("[mode-guard] mark-active SSH-write failed: %s", (out or "").strip()[:200])
        except Exception as e:  # noqa: BLE001 — never break a build
            log.warning("[mode-guard] mark-active error: %s", e)

    def _mode_guard_mark_complete(self) -> None:
        """SSH-write running_builds=completed on the VPS at build end (the
        finally). Best-effort + never-raises; only fires if a mark-active landed
        this run."""
        run_id = getattr(self, "_mode_guard_run_id", None)
        if not run_id or not getattr(self.config, "mode_guard", False) or not self.config.vps_host:
            return
        try:
            import shlex
            path = shlex.quote(self.config.vps_monitor_path)
            cmd = (
                f"cd {path} && PYTHONPATH={path} "
                f"ANVIL_OPS_DB_PATH={shlex.quote(self.config.vps_ops_db)} "
                f"/usr/bin/python3 -m anvil.monitor.running_builds mark-complete "
                f"{shlex.quote(run_id)}"
            )
            ok, out = ssh_ops.ssh_run(self.config.vps_host, self.config.vps_user, cmd, timeout=20)
            if not ok:
                log.warning("[mode-guard] mark-complete SSH-write failed: %s", (out or "").strip()[:200])
        except Exception as e:  # noqa: BLE001 — never break a build
            log.warning("[mode-guard] mark-complete error: %s", e)
        finally:
            self._mode_guard_run_id = None

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
                f"{voice._prefix()} Resuming {Path(st.brief_path).name}, step "
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

            # v5 Phase 1c: mark the build active on the VPS (the mode-guard
            # reverse channel) so the monitor defers concurrent triggers.
            # Best-effort, opt-in (config.mode_guard), never blocks the build.
            self._mode_guard_mark_active(run_id, brief_path)

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

            # v3 Phase 1a Step 2: advisory brief lint. Runs after
            # resolve_context_paths (so context_paths_count reflects the
            # resolved paths) and before the step loop. Pure advisory —
            # never mutates the brief, never gates execution; lint_brief
            # owns its own never-raise contract, so no wrap here. Stashed
            # on state.lint_result via each branch's transition() write
            # below (re-linting on resume is intentional — it back-fills
            # legacy state files that predate the field).
            lint_result = lint_brief(brief)
            # v3 Phase 1a Step 3 (V3P1A-3): stash the lint result on the
            # Planner so _call_anthropic's _policy_routing can merge the lint
            # structured_features into the policy's decision_basis (lint wins
            # on collision). Instance-attribute stash mirrors the V3P0-2 /
            # V3P0-6 precedent; the wrapper reads it via getattr (None when
            # unset → lint features simply absent from the merge).
            self.planner._current_lint_result = lint_result

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
                state = transition(
                    state, "running", pending_action=None,
                    lint_result=lint_result,
                )
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
                                   run_log=str(self._run_log),
                                   lint_result=lint_result)
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

                # 5d.5 observe (v4 Phase 2c Step 1 — the orchestrator's first
                # build-loop observe sub-phase; inserted after smoke / before
                # commit per the Phase 2 design Q9 placement). Mechanical here:
                # launch the Phase 2a browser substrate, capture the brief-
                # declared surfaces, persist via visibility_session. CAPTURE-ONLY
                # (Q-F7): fires only when the step declares observe:; every
                # browser/write failure logs and continues to commit — it NEVER
                # fails the step. Per-step BrowserSession lifecycle, closed in
                # try/finally so a mid-capture error still tears down (Q-F6).
                # Step 2 completes the loop: capture → Haiku digest (the Phase 1a
                # seam's first real consumer) → single write (digest-first, BAF-1)
                # → emit observe.captured (the first v4 VALID_KINDS bump, 52). The
                # default path (no observe:) is byte-identical: this whole block
                # short-circuits when observe is None.
                if bstep.observe is not None:
                    observe_target = bstep.observe.get("target")
                    # Order-preserving dedup of the declared surfaces (Phase 2b
                    # carry-forward 2: the schema permits duplicates; the observe-
                    # loop dedups at consumption so a surface isn't double-captured
                    # in one observation window).
                    _seen: set = set()
                    surfaces = []
                    for _s in (bstep.observe.get("surfaces") or []):
                        if _s not in _seen:
                            _seen.add(_s)
                            surfaces.append(_s)
                    # v4 Phase 3c Step 1: dispatch the observe sub-phase by the
                    # target's scheme (Q-C2 / BAF-3). https:// → the Phase 2c
                    # browser path (BrowserSession + Haiku + observe.captured,
                    # byte-identical, extracted unchanged to the helper below);
                    # screen:// / tab:// → the screen-aware path (screen substrate
                    # + Sonnet vision + screen.captured). A clean branch, not a
                    # rewrite — the browser path is unchanged.
                    scheme = _observe_scheme(observe_target)
                    if scheme == "browser":
                        self._observe_browser_subphase(
                            state, idx, bstep, observe_target, surfaces
                        )
                    else:  # screen / tab
                        self._observe_screen_subphase(
                            state, idx, bstep, observe_target, surfaces, scheme
                        )

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
                    # v2 Phase 1 Step 6: AUTO_REPLY_FOR_CALIBRATION
                    # short-circuits the explicit-confirm wait too.
                    # The send leg above ran for real; only the wait
                    # is bypassed. Keeps the framework profile honest
                    # (real send legs, mocked user wait).
                    auto = os.environ.get("AUTO_REPLY_FOR_CALIBRATION", "").strip()
                    if auto:
                        text = auto.lower()
                        _calibration_auto_reply_log(
                            "step-completion-wait", text, idx,
                        )
                    else:
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
            # v5 Phase 1c: clear the VPS running_builds row (mode-guard) on
            # every exit path. Best-effort, never-raises (no-op if no
            # mark-active landed this run).
            self._mode_guard_mark_complete()

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
            f"{voice._prefix()} Step {plan.step_number} — execute in Claude Code, then "
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

    # ---- observe sub-phase (v4 Phase 2c browser / Phase 3c screen) ----
    def _observe_browser_subphase(self, state, idx, bstep, observe_target, surfaces):
        """v4 Phase 2c observe sub-phase, browser-class (https://). Launch the
        Phase 2a browser substrate, capture the declared DOM/console/network
        surfaces, route to a Haiku digest (the seam's first real consumer),
        single-write via visibility_session, emit observe.captured. CAPTURE-ONLY
        (Q-F7): every failure logs and continues — never fails the step. Per-step
        BrowserSession lifecycle, closed in try/finally (Q-F6). Extracted
        unchanged from the Phase 2c inline block at Phase 3c Step 1 (the scheme
        dispatch); the browser path is byte-identical to Phase 2c."""
        observations = {"dom": None, "console": None, "network": None}
        _capture = {
            "dom": "snapshot_dom",
            "console": "capture_console",
            "network": "capture_network",
        }
        sess = browser.BrowserSession()
        try:
            launched = sess.launch(headless=True)
            if launched.get("ok"):
                nav = sess.navigate(observe_target)
                if nav.get("ok"):
                    for surface in surfaces:
                        method = _capture.get(surface)
                        if method is None:
                            continue  # schema (rule 17) validated it
                        res = getattr(sess, method)()
                        if res.get("ok"):
                            observations[surface] = res.get("result")
                        else:
                            self._log_event(
                                "observe",
                                f"step {bstep.number}: capture "
                                f"{surface} failed: {res.get('error')}",
                            )
                else:
                    self._log_event(
                        "observe",
                        f"step {bstep.number}: navigate "
                        f"{observe_target!r} failed: {nav.get('error')}",
                    )
            else:
                self._log_event(
                    "observe",
                    f"step {bstep.number}: browser launch failed: "
                    f"{launched.get('error')}",
                )
        finally:
            try:
                sess.close()
            except Exception:  # noqa: BLE001 — defensive; never-raises
                pass
        # Route to a Haiku digest BEFORE the single write (BAF-1 digest-first-
        # single-write). Capture-only: a seam failure (the error sentinel) OR an
        # empty/whitespace response (Q-F4-F1) both yield digest=None + continue.
        digest = None
        if any(observations.get(s) is not None
               for s in ("dom", "console", "network")):
            raw_digest = routing.call_model_for_subtask(
                "haiku",
                DIGEST_SYSTEM_PROMPT,
                _build_observation_summary(observations),
            )
            if raw_digest.startswith("[call_model_for_subtask error:"):
                self._log_event(
                    "observe",
                    f"step {bstep.number}: digest seam error "
                    f"(digest=None): {raw_digest}",
                )
            elif not raw_digest.strip():
                self._log_event(
                    "observe",
                    f"step {bstep.number}: digest empty — Haiku "
                    "returned no text content (digest=None; Q-F4-F1)",
                )
            else:
                digest = raw_digest
        written = visibility_session.write_session(
            state.run_id, idx, observe_target or "", observations,
            digest=digest,
        )
        record_path = ""
        if written.get("ok"):
            record_path = (written.get("result") or {}).get("path", "")
        else:
            self._log_event(
                "observe",
                f"step {bstep.number}: visibility_session write "
                f"failed: {written.get('error')}",
            )
        # Emit observe.captured (Q-F2/Q-F3) — derived counts + the record path
        # + digest size + ok; NO blobs on the row.
        _console_errs = 0
        _net_fails = 0
        if observations.get("console"):
            _console_errs = sum(
                1 for e in observations["console"].get("entries", [])
                if e.get("type") == "error"
            )
        if observations.get("network"):
            _net_fails = sum(
                1 for e in observations["network"].get("entries", [])
                if (e.get("status") or 0) >= 400
            )
        _events.emit(
            "observe.captured",
            {
                "step_idx": idx,
                "target": observe_target or "",
                "surfaces": surfaces,
                "record_path": record_path,
                "console_error_count": _console_errs,
                "network_failure_count": _net_fails,
                "digest_chars": len(digest) if digest else 0,
                "ok": bool(written.get("ok")),
            },
            step_idx=idx,
        )
        self._log_event(
            "observe",
            f"step {bstep.number}: captured {surfaces} → "
            f"{record_path or '(write failed)'}; "
            f"digest_chars={len(digest) if digest else 0}",
        )

    def _observe_screen_subphase(
        self, state, idx, bstep, observe_target, surfaces, scheme
    ):
        """v4 Phase 3c Step 1 observe sub-phase, screen-class (screen:// native /
        tab:// extension). Launch the Phase 3a screen substrate, capture the
        declared frame/accessibility surfaces, route the frame to a Sonnet VISION
        digest (the seam's first vision consumer, BAF-1), single-write via
        visibility_session (frame is binary — write_bytes), emit screen.captured
        (mode=build). CAPTURE-ONLY (Q-F7): every failure logs and continues —
        never fails the step. Per-step session lifecycle, torn down in finally
        (Q-F6). screen:// → ScreenCaptureSession (snapshot_frame +
        query_accessibility); tab:// → BrowserExtensionSession (capture_tab;
        the accessibility tree is a native-only surface, skipped on tab://)."""
        observations = {"frame": None, "accessibility": None}
        if scheme == "tab":
            sess = screen_browser.BrowserExtensionSession()
            _open, _close = sess.connect_extension, sess.disconnect
            _frame = sess.capture_tab
            _accessibility = None  # the extension surface has no AX tree
        else:  # screen
            sess = screen_capture.ScreenCaptureSession()
            _open, _close = sess.start_capture, sess.stop_capture
            _frame = sess.snapshot_frame
            _accessibility = sess.query_accessibility
        try:
            opened = _open()
            if opened.get("ok"):
                if "frame" in surfaces:
                    res = _frame()
                    if res.get("ok"):
                        observations["frame"] = res.get("result")
                    else:
                        self._log_event(
                            "observe",
                            f"step {bstep.number}: capture frame failed: "
                            f"{res.get('error')}",
                        )
                if "accessibility" in surfaces:
                    if _accessibility is None:
                        self._log_event(
                            "observe",
                            f"step {bstep.number}: accessibility surface "
                            f"unsupported on {scheme} scheme (skipped)",
                        )
                    else:
                        res = _accessibility()
                        if res.get("ok"):
                            observations["accessibility"] = res.get("result")
                        else:
                            self._log_event(
                                "observe",
                                f"step {bstep.number}: capture accessibility "
                                f"failed: {res.get('error')}",
                            )
            else:
                self._log_event(
                    "observe",
                    f"step {bstep.number}: screen substrate ({scheme}) open "
                    f"failed: {opened.get('error')}",
                )
        finally:
            try:
                _close()
            except Exception:  # noqa: BLE001 — defensive; never-raises
                pass
        # Route the frame to a Sonnet VISION digest (the seam's first vision
        # consumer). digest=None on no frame / seam error / empty. An
        # accessibility-only capture (no frame) still digests via Sonnet, text-
        # only (image absent) — the AX tree is the signal.
        digest = None
        vision_used = False
        frame_blob = observations.get("frame")
        ax_blob = observations.get("accessibility")
        if frame_blob is not None and frame_blob.get("frame_png"):
            vision_used = True
            raw_digest = routing.call_model_for_subtask(
                "sonnet",
                VISION_SYSTEM_PROMPT,
                _build_screen_summary(observations),
                image=frame_blob["frame_png"],
            )
            if raw_digest.startswith("[call_model_for_subtask error:"):
                self._log_event(
                    "observe",
                    f"step {bstep.number}: vision digest seam error "
                    f"(digest=None): {raw_digest}",
                )
            elif not raw_digest.strip():
                self._log_event(
                    "observe",
                    f"step {bstep.number}: vision digest empty — Sonnet "
                    "returned no text content (digest=None)",
                )
            else:
                digest = raw_digest
        elif ax_blob is not None:
            raw_digest = routing.call_model_for_subtask(
                "sonnet",
                VISION_SYSTEM_PROMPT,
                _build_screen_summary(observations),
            )
            if (not raw_digest.startswith("[call_model_for_subtask error:")
                    and raw_digest.strip()):
                digest = raw_digest
        written = visibility_session.write_session(
            state.run_id, idx, observe_target or "", observations,
            digest=digest,
        )
        record_path = ""
        if written.get("ok"):
            record_path = (written.get("result") or {}).get("path", "")
        else:
            self._log_event(
                "observe",
                f"step {bstep.number}: visibility_session write failed: "
                f"{written.get('error')}",
            )
        # Emit screen.captured (Q-C7; mode=build) — derived counts + the record
        # path + digest size + ok; NO blobs on the row. VALID_KINDS stays 53.
        _ax_count = (
            len(ax_blob.get("elements", []))
            if isinstance(ax_blob, dict) else 0
        )
        _events.emit(
            "screen.captured",
            {
                "mode": "build",
                "step_idx": idx,
                "target": observe_target or "",
                "surfaces": surfaces,
                "record_path": record_path,
                "accessibility_element_count": _ax_count,
                "vision_used": vision_used,
                "frame_count": 1 if frame_blob is not None else 0,
                "digest_chars": len(digest) if digest else 0,
                "ok": bool(written.get("ok")),
            },
            step_idx=idx,
        )
        self._log_event(
            "observe",
            f"step {bstep.number}: screen-captured {surfaces} ({scheme}) → "
            f"{record_path or '(write failed)'}; vision_used={vision_used}; "
            f"digest_chars={len(digest) if digest else 0}",
        )

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
        # v2 Phase 1 Step 6: AUTO_REPLY_FOR_CALIBRATION short-circuits
        # the wait — calibration runs don't enter the long-poll seam,
        # so telegram.poll.* events do NOT fire. The send leg in
        # `_escalate` above already ran for real, so escalation.raised
        # is present and (still) followed by escalation.resolved here.
        auto = os.environ.get("AUTO_REPLY_FOR_CALIBRATION", "").strip()
        if auto:
            text = auto.lower()
            _step_idx_for_log = (
                (state.current_step - 1) if isinstance(state.current_step, int)
                else None
            )
            _calibration_auto_reply_log(
                "_await_user_decision", text, _step_idx_for_log,
            )
        else:
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
        # v2 Phase 1 Step 6: lazy import — see the top-of-module
        # comment for the circular-load reasoning.
        from anvil import checkpoint as _checkpoint
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
        # v2 Phase 1 Step 7 prep: defensive AUTO_REPLY_FOR_CALIBRATION
        # short-circuit at the artefact-preview wait. Calibration runs
        # normally hit the soft-skip path higher up (no setup-log at
        # the ANVIL repo root), but any future shape that reaches here
        # would have hung the sweep without this guard.
        auto = os.environ.get("AUTO_REPLY_FOR_CALIBRATION", "").strip()
        if auto:
            text = auto.lower()
            _calibration_auto_reply_log("artefact-preview-wait", text, None)
        else:
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
