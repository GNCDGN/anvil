#!/usr/bin/env python3
"""Phase 2 Step 9 follow-up (9d) — plumb CODER_MODE from env to Orchestrator.

Wiring gap: cli.py's cmd_run and cmd_resume both construct
`Orchestrator(config).run(...)` without passing coder_mode, so the
__init__ default ("manual") wins regardless of what the user puts in
.env. Step 10's live-run needs `coder_mode="auto"` to fire the new
auto-mode path.

Fix:
  1. anvil/config.py: add `coder_mode: str = "manual"` to the Config
     dataclass, read CODER_MODE from env (default "manual", validated to
     one of the allowed values).
  2. anvil/cli.py: cmd_run and cmd_resume pass coder_mode=config.coder_mode
     through to Orchestrator(...).

Idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "anvil" / "config.py"
CLI = ROOT / "anvil" / "cli.py"

for f in (CONFIG, CLI):
    if not f.is_file():
        print(f"error: {f} not found.", file=sys.stderr)
        sys.exit(1)


# ===========================================================================
# Part 1 — anvil/config.py
# ===========================================================================

src = CONFIG.read_text(encoding="utf-8")
orig = src

# Edit 1a: add coder_mode field after claude_binary, with a default of
# "manual". Both fields are at the tail of the dataclass; both have
# defaults, so the no-default-after-default rule is satisfied.
field_old = "    claude_binary: str | None = None\n"
field_new = (
    "    claude_binary: str | None = None\n"
    "    coder_mode: str = \"manual\"\n"
)
if "    coder_mode: str =" in src:
    print("[1/4] Config.coder_mode already present; skipping.")
elif field_old in src:
    src = src.replace(field_old, field_new, 1)
    print("[1/4] Config gained coder_mode field.")
else:
    print(
        "error: could not find claude_binary anchor in config.py.",
        file=sys.stderr,
    )
    sys.exit(2)

# Edit 1b: read CODER_MODE from env, validate, store in a local.
env_old = (
    "        claude_binary = os.environ.get(\"CLAUDE_BINARY\", \"\").strip() or None\n"
)
env_new = (
    "        claude_binary = os.environ.get(\"CLAUDE_BINARY\", \"\").strip() or None\n"
    "        coder_mode = os.environ.get(\"CODER_MODE\", \"manual\").strip() or \"manual\"\n"
    "        if coder_mode not in (\"manual\", \"auto\"):\n"
    "            problems.append(\n"
    "                f\"CODER_MODE (must be 'manual' or 'auto', got {coder_mode!r})\"\n"
    "            )\n"
    "            coder_mode = \"manual\"\n"
)
if "coder_mode = os.environ.get(\"CODER_MODE\"" in src:
    print("[2/4] config env-load of CODER_MODE already present; skipping.")
elif env_old in src:
    src = src.replace(env_old, env_new, 1)
    print("[2/4] config now reads CODER_MODE from env with validation.")
else:
    print(
        "error: could not find claude_binary env-load anchor.",
        file=sys.stderr,
    )
    sys.exit(3)

# Edit 1c: pass coder_mode in the Config(...) return statement.
return_old = (
    "            claude_binary=claude_binary,\n"
    "        )\n"
)
return_new = (
    "            claude_binary=claude_binary,\n"
    "            coder_mode=coder_mode,\n"
    "        )\n"
)
if "coder_mode=coder_mode," in src:
    print("[3/4] Config return already passes coder_mode; skipping.")
elif return_old in src:
    src = src.replace(return_old, return_new, 1)
    print("[3/4] Config return now includes coder_mode.")
else:
    print(
        "error: could not find Config return-statement anchor.",
        file=sys.stderr,
    )
    sys.exit(4)

if src != orig:
    backup = CONFIG.with_suffix(".py.pre-phase-2-step-9d.bak")
    backup.write_text(orig, encoding="utf-8")
    CONFIG.write_text(src, encoding="utf-8")
    print(f"wrote {CONFIG} (backup at {backup})")


# ===========================================================================
# Part 2 — anvil/cli.py
# ===========================================================================

src = CLI.read_text(encoding="utf-8")
orig = src

# Edit 2: cmd_run and cmd_resume both construct Orchestrator without
# coder_mode. Pass it through from config.

cmd_run_old = (
    "    from anvil.orchestrator import Orchestrator\n"
    "    return Orchestrator(config).run(briefs[0])\n"
)
cmd_run_new = (
    "    from anvil.orchestrator import Orchestrator\n"
    "    return Orchestrator(config, coder_mode=config.coder_mode).run(briefs[0])\n"
)
if "Orchestrator(config, coder_mode=config.coder_mode).run" in src:
    print("[4a/4] cmd_run already plumbs coder_mode; skipping.")
elif cmd_run_old in src:
    src = src.replace(cmd_run_old, cmd_run_new, 1)
    print("[4a/4] cmd_run now plumbs coder_mode from config.")
else:
    print(
        "error: could not find cmd_run Orchestrator-construction anchor.",
        file=sys.stderr,
    )
    sys.exit(5)

cmd_resume_old = (
    "    from anvil.orchestrator import Orchestrator\n"
    "    return Orchestrator(config).resume()\n"
)
cmd_resume_new = (
    "    from anvil.orchestrator import Orchestrator\n"
    "    return Orchestrator(config, coder_mode=config.coder_mode).resume()\n"
)
if "Orchestrator(config, coder_mode=config.coder_mode).resume" in src:
    print("[4b/4] cmd_resume already plumbs coder_mode; skipping.")
elif cmd_resume_old in src:
    src = src.replace(cmd_resume_old, cmd_resume_new, 1)
    print("[4b/4] cmd_resume now plumbs coder_mode from config.")
else:
    print(
        "error: could not find cmd_resume Orchestrator-construction anchor.",
        file=sys.stderr,
    )
    sys.exit(6)

if src != orig:
    backup = CLI.with_suffix(".py.pre-phase-2-step-9d.bak")
    backup.write_text(orig, encoding="utf-8")
    CLI.write_text(src, encoding="utf-8")
    print(f"wrote {CLI} (backup at {backup})")

print("\nverify with:")
print("  .venv/bin/python -m py_compile anvil/config.py anvil/cli.py")
print("  .venv/bin/python -m unittest discover tests/ -v")
print("\nthen add CODER_MODE=auto to .env before the live run.")
