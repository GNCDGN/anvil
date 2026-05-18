#!/usr/bin/env python3
"""Phase 2 Step 6 — brief parse-time path warnings + Stage A
disk-reconciliation-note attachment.

Closes the first two layers of Phase 1 decision #18:
  Layer 1 — anvil/brief.py: a new parse-time computation that records
    scope.files entries that don't exist at target_repo_path (and aren't
    write-targets of any step) as warnings on brief.parse_warnings.
    Warnings emit to stderr + the anvil logger; they do NOT reject.
  Layer 2 — anvil/planner.py: _assemble_stage_a_prompt appends a
    [disk-reconciliation-note] block when brief.parse_warnings contains
    entries for the current step, so Stage B can flag the reconciliation
    in escalation_triggers.

Layer 3 (Coder runtime reconciliation) lands at Step 8 with the Coder.

Idempotent. Aborts non-zero if expected anchors are missing.

Design deviation worth recording: the brief said this was "a new
validation pass". The warning doesn't reject and validate_or_reject's
contract is rejection-via-raise on an accumulated error list — stuffing
non-rejecting signals in there would be wrong-shaped. The warnings
therefore live as a parse-time computation (parse_brief_raw → Brief.
parse_warnings), not a validation rule. Same outcome, cleaner shape.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRIEF = ROOT / "anvil" / "brief.py"
PLANNER = ROOT / "anvil" / "planner.py"

for f in (BRIEF, PLANNER):
    if not f.is_file():
        print(f"error: {f} not found. Run from ~/Downloads/anvil/.",
              file=sys.stderr)
        sys.exit(1)


# ===========================================================================
# Part 1 — patch anvil/brief.py
# ===========================================================================

src = BRIEF.read_text(encoding="utf-8")
orig = src

# ---------------------------------------------------------------------------
# Edit 1: extend the Brief model with parse_warnings.
# ---------------------------------------------------------------------------
brief_model_old = (
    "class Brief(BaseModel):\n"
    "    brief_version: int\n"
    "    project: str\n"
    "    build_name: str\n"
    "    target_repo: str\n"
    "    target_repo_path: Path\n"
    "    vps_deploy: Literal[\"yes\", \"no\"]\n"
    "    service_name: str | None = None\n"
    "    goal: str = \"\"\n"
    "    context_links: list[str] = []\n"
    "    context_paths: list[Path] = []\n"
    "    steps: list[Step] = []\n"
    "    end_to_end_test: EndToEndTest | None = None\n"
)
brief_model_new = (
    "class Brief(BaseModel):\n"
    "    brief_version: int\n"
    "    project: str\n"
    "    build_name: str\n"
    "    target_repo: str\n"
    "    target_repo_path: Path\n"
    "    vps_deploy: Literal[\"yes\", \"no\"]\n"
    "    service_name: str | None = None\n"
    "    goal: str = \"\"\n"
    "    context_links: list[str] = []\n"
    "    context_paths: list[Path] = []\n"
    "    steps: list[Step] = []\n"
    "    end_to_end_test: EndToEndTest | None = None\n"
    "    # Phase 2 Step 6 (decision #18 layer 1): scope.files paths that\n"
    "    # don't exist at target_repo_path AND aren't write-targets of any\n"
    "    # step land here as warnings (not validation errors). Each entry:\n"
    "    # {'kind': 'path-not-found', 'step_number': int, 'path': str,\n"
    "    #  'closest_match': str | None}.\n"
    "    parse_warnings: list[dict] = []\n"
)

if "parse_warnings: list[dict]" in src:
    print("[1/5] Brief.parse_warnings already present; skipping.")
elif brief_model_old in src:
    src = src.replace(brief_model_old, brief_model_new, 1)
    print("[1/5] Brief gained parse_warnings field.")
else:
    print("error: could not find Brief model anchor in brief.py.",
          file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Edit 2: add _compute_parse_warnings + wire it into parse_brief_raw.
# Insert helper just before parse_brief_raw.
# ---------------------------------------------------------------------------
helper_anchor = "def parse_brief_raw(path: Path) -> tuple[Brief, dict]:"

helper_block = (
    'def _basename_match(repo: Path, target: str) -> str | None:\n'
    '    """Walk repo (excluding .git/__pycache__/.venv/node_modules) for a\n'
    '    file with the same basename as `target`. Return the relative path\n'
    '    of the single match, or None if zero or multiple matches exist.\n'
    '    Deterministic: walks in sorted order so the same input always\n'
    '    produces the same answer."""\n'
    '    base = Path(target).name\n'
    '    excluded = {".git", "__pycache__", ".venv", "node_modules"}\n'
    '    hits: list[str] = []\n'
    '    try:\n'
    '        for p in sorted(repo.rglob(base)):\n'
    '            if any(seg in excluded for seg in p.parts):\n'
    '                continue\n'
    '            if not p.is_file():\n'
    '                continue\n'
    '            try:\n'
    '                hits.append(str(p.relative_to(repo)))\n'
    '            except ValueError:\n'
    '                continue\n'
    '    except OSError:\n'
    '        return None\n'
    '    return hits[0] if len(hits) == 1 else None\n'
    '\n'
    '\n'
    'def _compute_parse_warnings(brief: Brief) -> list[dict]:\n'
    '    """Phase 2 Step 6 (decision #18): for each step\'s scope.files,\n'
    '    warn if the path doesn\'t exist at target_repo_path AND isn\'t a\n'
    '    write-target of any step. Files the build creates would otherwise\n'
    '    be falsely warned about.\n'
    '\n'
    '    Returns a list of warning dicts; the caller assigns them to\n'
    '    brief.parse_warnings AND emits the human-readable line via\n'
    '    _emit_parse_warnings.\n'
    '\n'
    '    target_repo_path is checked for existence; if it doesn\'t exist\n'
    '    yet (validation will catch that separately), the warning pass is\n'
    '    skipped entirely — no false-positive flood of "everything missing".\n'
    '    """\n'
    '    repo = brief.target_repo_path\n'
    '    if not repo.is_dir():\n'
    '        return []\n'
    '    write_targets: set[str] = set()\n'
    '    for step in brief.steps:\n'
    '        if "write" in step.scope_operations:\n'
    '            write_targets.update(step.scope_files)\n'
    '    warnings: list[dict] = []\n'
    '    for step in brief.steps:\n'
    '        for sf in step.scope_files:\n'
    '            if (repo / sf).exists():\n'
    '                continue\n'
    '            if sf in write_targets:\n'
    '                continue\n'
    '            warnings.append({\n'
    '                "kind": "path-not-found",\n'
    '                "step_number": step.number,\n'
    '                "path": sf,\n'
    '                "closest_match": _basename_match(repo, sf),\n'
    '            })\n'
    '    return warnings\n'
    '\n'
    '\n'
    'def _emit_parse_warnings(warnings: list[dict]) -> None:\n'
    '    """Emit each warning to stderr and to the anvil logger. Stderr\n'
    '    line shape matches the brief\'s spec:\n'
    '      [brief-warning] step N: scope.files entry \'X\' does not exist\n'
    '      at target_repo_path; closest match: \'Y\'. Continuing; the Coder\n'
    '      will reconcile at execute time."""\n'
    '    if not warnings:\n'
    '        return\n'
    '    import logging\n'
    '    log = logging.getLogger("anvil.brief")\n'
    '    for w in warnings:\n'
    '        cm = w.get("closest_match")\n'
    '        cm_text = f"\'{cm}\'" if cm else "(none)"\n'
    '        line = (\n'
    '            f"[brief-warning] step {w[\'step_number\']}: scope.files "\n'
    '            f"entry \'{w[\'path\']}\' does not exist at target_repo_path; "\n'
    '            f"closest match: {cm_text}. Continuing; the Coder will "\n'
    '            "reconcile at execute time."\n'
    '        )\n'
    '        print(line, file=sys.stderr)\n'
    '        log.warning(line)\n'
    '\n'
    '\n'
)

if "def _compute_parse_warnings" in src:
    print("[2/5] _compute_parse_warnings already present; skipping.")
elif helper_anchor in src:
    # Add `import sys` to the top-level imports if not already present.
    if "\nimport sys" not in src and "import sys\n" not in src:
        import_anchor = "import re\nimport subprocess\n"
        src = src.replace(
            import_anchor, "import re\nimport subprocess\nimport sys\n", 1,
        )
    src = src.replace(helper_anchor, helper_block + helper_anchor, 1)
    print("[2/5] _compute_parse_warnings + _emit_parse_warnings + _basename_match added.")
else:
    print("error: could not find parse_brief_raw anchor in brief.py.",
          file=sys.stderr)
    sys.exit(3)


# ---------------------------------------------------------------------------
# Edit 3: wire _compute_parse_warnings into parse_brief_raw (after the
# Brief() construction, before returning the tuple).
# ---------------------------------------------------------------------------
parse_old = (
    "        end_to_end_test=_parse_e2e(sections.get(\"end-to-end test\")),\n"
    "    )\n"
    "    return brief, (fm if isinstance(fm, dict) else {})\n"
)
parse_new = (
    "        end_to_end_test=_parse_e2e(sections.get(\"end-to-end test\")),\n"
    "    )\n"
    "    # Phase 2 Step 6 (decision #18 layer 1): compute parse-time path\n"
    "    # warnings and attach them to the Brief. Emit each to stderr +\n"
    "    # logger so the build session sees them before Stage A runs.\n"
    "    warnings = _compute_parse_warnings(brief)\n"
    "    if warnings:\n"
    "        brief = brief.model_copy(update={\"parse_warnings\": warnings})\n"
    "        _emit_parse_warnings(warnings)\n"
    "    return brief, (fm if isinstance(fm, dict) else {})\n"
)

if "Phase 2 Step 6 (decision #18 layer 1): compute parse-time path" in src:
    print("[3/5] parse_brief_raw already calls _compute_parse_warnings; skipping.")
elif parse_old in src:
    src = src.replace(parse_old, parse_new, 1)
    print("[3/5] parse_brief_raw now attaches parse_warnings.")
else:
    print("error: could not find parse_brief_raw return anchor.",
          file=sys.stderr)
    sys.exit(4)


if src != orig:
    backup = BRIEF.with_suffix(".py.pre-phase-2-step-6.bak")
    backup.write_text(orig, encoding="utf-8")
    BRIEF.write_text(src, encoding="utf-8")
    print(f"wrote {BRIEF} (backup at {backup})")
else:
    print("brief.py already at patched state.")


# ===========================================================================
# Part 2 — patch anvil/planner.py: append disk-reconciliation-note block
# ===========================================================================

src = PLANNER.read_text(encoding="utf-8")
orig = src

# Wrap the existing return at the bottom of _assemble_stage_a_prompt so the
# block is appended AFTER all substitutions but BEFORE the function returns.
planner_old = (
    "    out = template\n"
    "    for token, value in subs:\n"
    "        out = out.replace(token, value)\n"
    "    return out\n"
)
planner_new = (
    "    out = template\n"
    "    for token, value in subs:\n"
    "        out = out.replace(token, value)\n"
    "\n"
    "    # Phase 2 Step 6 (decision #18 layer 2): append a\n"
    "    # [disk-reconciliation-note] block when the brief carries\n"
    "    # parse_warnings for THIS step. Stage B sees this through the\n"
    "    # Stage A->B handoff and may surface the reconciliation in\n"
    "    # escalation_triggers. The Stage A template itself is not\n"
    "    # modified; the block is appended at assembly time, same posture\n"
    "    # as Phase 1's {PRIOR_STEP_BLOCK} rendering. The block is omitted\n"
    "    # entirely when no warning applies to the current step.\n"
    "    relevant = [\n"
    "        w for w in getattr(brief, \"parse_warnings\", []) or []\n"
    "        if w.get(\"step_number\") == step.number\n"
    "    ]\n"
    "    if relevant:\n"
    "        lines = []\n"
    "        for w in relevant:\n"
    "            cm = w.get(\"closest_match\")\n"
    "            cm_text = f\"'{cm}'\" if cm else \"(no single close match found)\"\n"
    "            lines.append(\n"
    "                f\"[disk-reconciliation-note] Brief step {w['step_number']} \"\n"
    "                f\"references '{w['path']}'; this path does not exist at \"\n"
    "                f\"target_repo_path. Closest match on disk: {cm_text}. \"\n"
    "                \"The Coder will reconcile at execute time; you may want \"\n"
    "                \"to flag this in escalation_triggers.\"\n"
    "            )\n"
    "        out = out.rstrip() + \"\\n\\n\" + \"\\n\".join(lines) + \"\\n\"\n"
    "\n"
    "    return out\n"
)

if "Phase 2 Step 6 (decision #18 layer 2)" in src:
    print("[4/5] _assemble_stage_a_prompt already appends disk-reconciliation-note; skipping.")
elif planner_old in src:
    src = src.replace(planner_old, planner_new, 1)
    print("[4/5] _assemble_stage_a_prompt appends disk-reconciliation-note block.")
else:
    print("error: could not find _assemble_stage_a_prompt return anchor.",
          file=sys.stderr)
    sys.exit(5)

if src != orig:
    backup = PLANNER.with_suffix(".py.pre-phase-2-step-6.bak")
    backup.write_text(orig, encoding="utf-8")
    PLANNER.write_text(src, encoding="utf-8")
    print(f"wrote {PLANNER} (backup at {backup})")
    print("[5/5] both files patched.")
else:
    print("planner.py already at patched state.")
    print("[5/5] no changes to planner.py.")

print("\nverify with:")
print("  .venv/bin/python -m py_compile anvil/brief.py anvil/planner.py")
print("  .venv/bin/python -m unittest discover tests/ -v")
