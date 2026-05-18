#!/usr/bin/env python3
"""Phase 2 Step 9 — update tests/test_orchestrator.py:264 obsolete assertion.

Phase 0 wrote `test_auto_coder_mode_raises_not_implemented` to lock in
the "coder_mode == 'auto' raises" contract. Phase 2 Step 9 removed that
NotImplementedError because auto-mode is now real. The test now fails
because the expected exception isn't raised.

Replace it with the inverse assertion: auto-mode constructs cleanly,
self.coder is a real Coder, no NotImplementedError. The full happy-path
coverage lives in tests/test_orchestrator_coder_integration.py — this
test guards the construction step alone, mirroring the original test's
narrow scope (Phase 0 was asserting Phase 0 behaviour; Phase 2 asserts
Phase 2 behaviour).

Idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTFILE = ROOT / "tests" / "test_orchestrator.py"

if not TESTFILE.is_file():
    print(f"error: {TESTFILE} not found.", file=sys.stderr)
    sys.exit(1)

src = TESTFILE.read_text(encoding="utf-8")
orig = src

old = (
    "    def test_auto_coder_mode_raises_not_implemented(self) -> None:\n"
    "        orch = self._orch([])\n"
    "        orch.coder_mode = \"auto\"\n"
    "        with self.assertRaises(NotImplementedError):\n"
    "            orch.handle_brief(self.brief_path)\n"
)
new = (
    "    def test_auto_coder_mode_constructs_without_raising(self) -> None:\n"
    "        # Phase 2 Step 9 replaces Phase 0's NotImplementedError\n"
    "        # assertion: auto-mode is now wired (decisions P2-8 + P2-9),\n"
    "        # so constructing an Orchestrator with coder_mode='auto'\n"
    "        # should succeed and populate self.coder. Full integration\n"
    "        # coverage lives at\n"
    "        # tests/test_orchestrator_coder_integration.py — this test\n"
    "        # guards the construction step alone, matching the original\n"
    "        # test's narrow scope (Phase 0 asserted Phase 0 behaviour;\n"
    "        # Phase 2 asserts Phase 2 behaviour).\n"
    "        from unittest import mock\n"
    "        from anvil.coder import Coder\n"
    "        from anvil.orchestrator import Orchestrator\n"
    "        orch = Orchestrator(\n"
    "            self.cfg,\n"
    "            coder_mode=\"auto\",\n"
    "            planner=mock.Mock(),\n"
    "            telegram=mock.Mock(),\n"
    "            git=mock.Mock(),\n"
    "            run_smoke=mock.Mock(),\n"
    "        )\n"
    "        self.assertIsInstance(orch.coder, Coder)\n"
    "        self.assertEqual(orch.coder_mode, \"auto\")\n"
)

if "def test_auto_coder_mode_constructs_without_raising" in src:
    print("[1/1] test already migrated; skipping.")
elif old in src:
    src = src.replace(old, new, 1)
    print("[1/1] obsolete NotImplementedError assertion replaced with inverse.")
else:
    print(
        "error: could not find the obsolete test anchor at "
        "test_orchestrator.py:264. Inspect the file by hand.",
        file=sys.stderr,
    )
    sys.exit(2)

if src != orig:
    backup = TESTFILE.with_suffix(".py.pre-phase-2-step-9c.bak")
    backup.write_text(orig, encoding="utf-8")
    TESTFILE.write_text(src, encoding="utf-8")
    print(f"wrote {TESTFILE} (backup at {backup})")

print("\nverify with:")
print("  .venv/bin/python -m unittest discover tests/ -v")
