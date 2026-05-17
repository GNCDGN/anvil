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
import os
import sys
from pathlib import Path

from anvil import __version__

_TERMINAL = {"done", "failed", "aborted"}


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
    return Orchestrator(config).run(briefs[0])


def cmd_resume(args: argparse.Namespace) -> int:
    config = _load_config_or_exit()
    from anvil.orchestrator import Orchestrator
    return Orchestrator(config).resume()


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
