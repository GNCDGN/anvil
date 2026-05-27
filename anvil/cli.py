"""ANVIL command-line entry point.

Phase 0 Step 9: `run` / `resume` are wired to the Orchestrator.

- `status` requires no Config and never crashes on missing state (Step 2
  contract preserved). It does not import Orchestrator/Config.
- `run` / `resume` load Config first; a ConfigError is printed cleanly and
  exits 1 (never a traceback). Config/Orchestrator are imported lazily
  inside those commands so `--help`/`status` stay light and Config-free.
- `run`: refuse to start if a non-terminal current-run.json exists
  ("build already in progress" — design's one-build-at-a-time rule); else
  take the first brief alphabetically from inbox/; empty inbox → exit 0.
- `resume`: delegate to Orchestrator.resume().
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from anvil import __version__

_TERMINAL = {"done", "failed", "aborted"}


def _setup_logging() -> Path:
    """Wire a file handler so the [planner] token-usage lines (and every
    anvil.* log) are persisted to anvil.log. Nothing else configures a
    handler anywhere; without this every log.info() no-ops at the
    handler-less root logger (decision #13). Attaches to the `anvil`
    parent logger (children propagate up), not root, so the test
    suite's assertLogs and any third-party logging are untouched.
    Idempotent: a second call does not stack a duplicate handler on the
    same file (keeps the manual probe and repeat invocations safe).
    Format is `%(asctime)s %(message)s` — the message already carries
    its own `[planner]` prefix, so no redundant logger-name bracket;
    the Step 10 `grep "\\[planner\\]"` still matches.
    """
    log_path = _anvil_root() / "anvil.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    anvil_log = logging.getLogger("anvil")
    anvil_log.setLevel(logging.INFO)
    for h in anvil_log.handlers:
        if isinstance(h, logging.FileHandler) and (
            Path(getattr(h, "baseFilename", "")).resolve()
            == log_path.resolve()
        ):
            return log_path
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    anvil_log.addHandler(handler)
    return log_path


def _anvil_root() -> Path:
    default_root = Path(__file__).resolve().parent.parent
    return Path(
        os.path.expanduser(os.environ.get("ANVIL_ROOT", str(default_root)))
    ).resolve()


def _state_md_path() -> Path:
    return _anvil_root() / "state" / "current-run.md"


def _load_config_or_exit():
    """Returns Config, or prints the ConfigError and raises SystemExit(1)."""
    from anvil.config import Config, ConfigError
    try:
        return Config.load()
    except ConfigError as e:
        print(f"anvil: configuration error\n{e}", file=sys.stderr)
        raise SystemExit(1)


def cmd_status(args: argparse.Namespace) -> int:
    p = _state_md_path()
    if p.is_file():
        print(p.read_text())
    else:
        print("no current run")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _setup_logging()
    config = _load_config_or_exit()

    # One build at a time: refuse if a non-terminal run is live.
    from anvil.state import read_state
    st = read_state()
    if st is not None and st.status not in _TERMINAL:
        print(
            "build already in progress — check state/current-run.md "
            f"(status: {st.status}). Use `anvil resume` to continue it."
        )
        return 1

    inbox = config.anvil_root / "inbox"
    briefs = sorted(inbox.glob("*.md")) if inbox.is_dir() else []
    if not briefs:
        print("no briefs in inbox")
        return 0

    from anvil.orchestrator import Orchestrator
    return Orchestrator(config, coder_mode=config.coder_mode).run(briefs[0])


def cmd_resume(args: argparse.Namespace) -> int:
    _setup_logging()
    config = _load_config_or_exit()
    from anvil.orchestrator import Orchestrator
    return Orchestrator(config, coder_mode=config.coder_mode).resume()


def cmd_copilot(args: argparse.Namespace) -> int:
    """v4 Phase 3c Step 2: `anvil copilot start <target> [--autonomous]` — the
    co-pilot session path (DC7). Loads Config (for the Telegram channel), then
    runs a bounded capture-interpret-guide session via copilot_runner. The
    --autonomous flag grants the actuation opt-in at start (default-off, DC8)."""
    if getattr(args, "copilot_command", None) != "start":
        print("usage: anvil copilot start <target> [--autonomous]",
              file=sys.stderr)
        return 1
    _setup_logging()
    config = _load_config_or_exit()
    from anvil import copilot_runner
    from anvil.telegram import TelegramClient
    telegram = TelegramClient(config.telegram_bot_token, config.telegram_chat_id)
    summary = copilot_runner.run(
        args.target,
        autonomous=args.autonomous,
        max_captures=args.max_captures,
        telegram=telegram,
    )
    print(
        f"co-pilot session {summary['session_id']} ({summary['scheme']}): "
        f"{summary['captures']} captures, {summary['frames']} frames, "
        f"autonomous_granted={summary['autonomous_granted']}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anvil", description="Autonomous build orchestrator"
    )
    parser.add_argument(
        "--version", action="version", version=f"anvil {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run", help="Process the next brief in inbox/ (first alphabetically)"
    )
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser(
        "resume", help="Resume an interrupted run"
    )
    p_resume.set_defaults(func=cmd_resume)

    p_status = sub.add_parser(
        "status", help="Print the current run state"
    )
    p_status.set_defaults(func=cmd_status)

    # v4 Phase 3c Step 2: the co-pilot session entry (DC7). `anvil copilot start
    # <target> [--autonomous] [--max-captures N]`.
    p_copilot = sub.add_parser(
        "copilot", help="Run a screen-aware co-pilot session"
    )
    p_copilot.set_defaults(func=cmd_copilot)
    copilot_sub = p_copilot.add_subparsers(dest="copilot_command", required=True)
    p_cp_start = copilot_sub.add_parser(
        "start", help="Start a co-pilot session against a target (e.g. screen://main)"
    )
    p_cp_start.add_argument("target", help="observe target, e.g. screen://main")
    p_cp_start.add_argument(
        "--autonomous", action="store_true",
        help="grant the actuation opt-in at start (default-off, DC8)",
    )
    p_cp_start.add_argument(
        "--max-captures", dest="max_captures", type=int, default=5,
        help="bound the capture-interpret-guide loop (default 5)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as e:  # _load_config_or_exit uses SystemExit(1)
        return int(e.code) if e.code is not None else 0


if __name__ == "__main__":
    sys.exit(main())
