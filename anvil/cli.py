"""ANVIL command-line entry point.

Phase 0 Step 2: argparse scaffold with `run`, `status`, `resume`. `run` and
`resume` are stubs until the orchestrator is built (Step 8) and wired here
(Step 9). `status` reads state/current-run.md if present, else prints
"no current run" — it must not crash on missing state, and must not require
a complete .env (status works even before Config is satisfiable).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from anvil import __version__


def _state_md_path() -> Path:
    """Resolve state/current-run.md from anvil_root without a full Config
    load — status must work even when .env is incomplete."""
    default_root = Path(__file__).resolve().parent.parent
    root = Path(
        os.path.expanduser(os.environ.get("ANVIL_ROOT", str(default_root)))
    ).resolve()
    return root / "state" / "current-run.md"


def cmd_run(args: argparse.Namespace) -> int:
    print(
        "anvil run: not implemented — orchestrator core lands in Step 8 and "
        "run/resume are wired here in Step 9 (see Phase 0 brief)."
    )
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    print(
        "anvil resume: not implemented — orchestrator core lands in Step 8 "
        "and run/resume are wired here in Step 9 (see Phase 0 brief)."
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    p = _state_md_path()
    if p.is_file():
        print(p.read_text())
    else:
        print("no current run")
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
        "run", help="Process the next brief in inbox/ (Phase 0: stub)"
    )
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser(
        "resume", help="Resume an interrupted run (Phase 0: stub)"
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
