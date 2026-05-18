#!/usr/bin/env python3
"""Phase 2 Step 9 follow-up — claude_binary needs a dataclass default.

The Step 9 patch added `claude_binary: str | None` to Config without a
default value, which broke `tests/test_orchestrator.py:131` which builds
Config(...) positionally with the Phase 0/1 set of fields. 5 tests
errored with TypeError: Config.__init__() missing 1 required positional
argument: 'claude_binary'.

Fix: give the field a default of `None`. Same posture as service_name in
the Brief model — optional, None means unset, code paths that need a
value provide their own fallback (Orchestrator._build_coder falls back
to shutil.which("claude") when claude_binary is None).

Idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "anvil" / "config.py"

if not CONFIG.is_file():
    print(f"error: {CONFIG} not found.", file=sys.stderr)
    sys.exit(1)

src = CONFIG.read_text(encoding="utf-8")
orig = src

# Replace `claude_binary: str | None` with `claude_binary: str | None = None`.
old = "    claude_binary: str | None\n"
new = "    claude_binary: str | None = None\n"

if "claude_binary: str | None = None" in src:
    print("[1/1] claude_binary already has a default; skipping.")
elif old in src:
    src = src.replace(old, new, 1)
    print("[1/1] claude_binary now defaults to None.")
else:
    print(
        "error: could not find `claude_binary: str | None` anchor in "
        "config.py. The Step 9 patch may have landed differently than "
        "expected; inspect the dataclass body.",
        file=sys.stderr,
    )
    sys.exit(2)

if src != orig:
    backup = CONFIG.with_suffix(".py.pre-phase-2-step-9b.bak")
    backup.write_text(orig, encoding="utf-8")
    CONFIG.write_text(src, encoding="utf-8")
    print(f"wrote {CONFIG} (backup at {backup})")

print("\nverify with:")
print("  .venv/bin/python -m py_compile anvil/config.py")
print("  .venv/bin/python -m unittest discover tests/ -v")
