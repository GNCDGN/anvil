"""Planner (implementation-notes Component 3 / design "Plan output schema").

Phase 0: a STUB Planner. There is no LLM call — `plan_step` reads
`tests/fixtures/stub-plans.json` and returns the hardcoded plan whose
`step_number` matches the brief step at the requested index. Phase 1 replaces
this body with the real two-stage Opus-driven Planner; the `Plan` model and
`validate_plan_scope` below are kept as-is for Phase 1.

`validate_plan_scope` is REAL validation (not a stub): it enforces that a
plan's `files_to_touch` ⊆ the brief step's declared `scope.files` and its
`operations` ⊆ declared `scope.operations`. The stub plans happen to pass it;
it must still catch genuine out-of-scope plans when the real Planner runs.

Component-3-faithfulness notes (Phase-0 stub adaptations, not deviations):
- Component 3's `Planner.__init__(api_key, model, timeout)` targets the real
  Phase 1 Planner. The Phase 0 stub needs none of those; they are accepted
  but optional, and `stub_plans_path` is added (defaults to the fixture).
  Phase 1 removes the stub path and uses api_key/model/timeout.
- `step_idx` is 0-based (the orchestrator loops `range(len(brief.steps))`);
  `Plan.step_number` is 1-based. `plan_step` maps via
  `brief.steps[step_idx].number` and raises `PlannerError` if no stub plan
  matches that number.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from anvil.errors import PlannerError

_DEFAULT_STUB_PLANS = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "stub-plans.json"
)


class ScopeBoundaries(BaseModel):
    in_scope: str
    out_of_scope: str


class Plan(BaseModel):
    step_number: int
    step_name: str
    files_to_touch: list[str]
    operations: list[str]
    approach: str
    smoke_test: str
    expected_outcome: str
    commit_message: str
    scope_boundaries: ScopeBoundaries
    confidence: Literal["high", "medium", "low"]
    escalation_triggers: list[str] = []


def validate_plan_scope(plan: Plan, step) -> bool:
    """REAL validation. True iff the plan stays within the brief step's
    declared scope:
      - every path in plan.files_to_touch is in step.scope_files
      - every op in plan.operations is in step.scope_operations
    Returns False on any out-of-scope file or operation. Used by the
    orchestrator: `if not validate_plan_scope(...): escalate(...)`.
    """
    files_ok = set(plan.files_to_touch).issubset(set(step.scope_files))
    ops_ok = set(plan.operations).issubset(set(step.scope_operations))
    return files_ok and ops_ok


class Planner:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        *,
        stub_plans_path: Path | None = None,
    ) -> None:
        # Phase 0 stub ignores api_key/model/timeout (no LLM). Retained in
        # the signature so Phase 1 can drop in the real Planner unchanged.
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._stub_plans_path = Path(stub_plans_path or _DEFAULT_STUB_PLANS)

    def _load_stub_plans(self) -> list[dict]:
        try:
            data = json.loads(self._stub_plans_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PlannerError(
                f"stub plans not found at {self._stub_plans_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise PlannerError(
                f"stub plans is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise PlannerError("stub-plans.json must be a JSON list of plans")
        return data

    def plan_step(self, brief, state, step_idx: int) -> Plan:
        """Phase 0: return the hardcoded plan whose step_number matches the
        brief step at step_idx (0-based). Raises PlannerError if step_idx is
        out of range or no stub plan matches that step number."""
        if step_idx < 0 or step_idx >= len(brief.steps):
            raise PlannerError(
                f"step_idx {step_idx} out of range "
                f"(brief has {len(brief.steps)} steps)"
            )
        target_number = brief.steps[step_idx].number
        for raw in self._load_stub_plans():
            if raw.get("step_number") == target_number:
                try:
                    return Plan.model_validate(raw)
                except Exception as exc:  # pydantic ValidationError
                    raise PlannerError(
                        f"stub plan for step {target_number} fails the Plan "
                        f"schema: {exc}"
                    ) from exc
        raise PlannerError(
            f"no stub plan for step_number {target_number} "
            f"(step_idx {step_idx})"
        )


# ---------------------------------------------------------------------------
# Phase 1 Stage A — vault index, prompt assembly, response parsing
#
# Added alongside the Phase 0 stub (above), which stays callable until Step 6
# replaces it. These are pure module-level functions: no Anthropic call, no
# client state. Step 6's real Planner class calls them; _call_anthropic
# lands in Step 5.
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_STAGE_A_TEMPLATE = _PROMPTS_DIR / "planner-stage-a.md"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_frontmatter(path: Path) -> dict:
    """First 4096 bytes -> leading `---` YAML block -> dict, or {} if the
    block is absent, unparseable, or not a mapping.

    Adapted from the Veronica vault-index reference (conversational
    implementation-notes Component 1) and anvil.brief._split_frontmatter;
    kept local so planner has no sibling-private coupling. The 4096-byte
    cap matches the reference; a frontmatter block larger than that
    truncates and yields {} (acceptable per "no parseable frontmatter
    -> {}").
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return {}
    text = head.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _build_vault_index(
    context_paths: list[str], vault_root: Path
) -> dict[str, dict]:
    """{path_str: frontmatter_dict} for every file under context_paths.

    A path that is a file is indexed directly. A folder is walked with a
    depth-2 cap: a file directly inside the folder is level 1, a file one
    subfolder deep is level 2, anything deeper is excluded. Dotfiles and
    .DS_Store are skipped (the Veronica reference's noise filter). Files
    with no parseable frontmatter map to {} -- present, not skipped.
    """
    vault_root = Path(vault_root)
    index: dict[str, dict] = {}
    for raw in context_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = vault_root / p
        if p.is_file():
            index[str(p)] = _parse_frontmatter(p)
            continue
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if f.name.startswith(".") or f.name == ".DS_Store":
                    continue
                if len(f.relative_to(p).parts) > 2:
                    continue
                index[str(f)] = _parse_frontmatter(f)
    return index


def _assemble_stage_a_prompt(
    brief, state, step_idx: int, vault_index: dict[str, dict]
) -> str:
    """Load planner-stage-a.md and substitute its placeholders.

    Substitution is str.replace per placeholder in a fixed order, NOT
    str.format: the template plus the YAML vault-index block contain
    literal { } braces that would break str.format. Do not "simplify"
    this to .format.

    {VOICE_SPEC} is not handled here. It is a system-prompt concern
    (Step 2 / Step 6 Planner.__init__); Stage A's user-prompt template
    has no {VOICE_SPEC} token. system prompt = system= arg; this
    template = user= arg.

    {BRIEF_MARKDOWN} source is state.brief_path (the Brief object carries
    no raw text; the orchestrator persists the brief path into state).
    Unreadable -> "" (never-raise); the structured step fields are still
    substituted.

    {STATE_JSON} uses state.model_dump_json(indent=2). design Part 2's
    sample said json.dumps(state.dict(), indent=2); pinned pydantic v2
    -> model_dump_json, same precedent as brief.py's dataclass->pydantic
    reconciliation (tracked decision #5).
    """
    template = _STAGE_A_TEMPLATE.read_text(encoding="utf-8")
    step = brief.steps[step_idx]

    try:
        brief_md = Path(state.brief_path).read_text(encoding="utf-8")
    except OSError:
        brief_md = ""

    state_json = state.model_dump_json(indent=2)
    vault_index_yaml = yaml.safe_dump(
        vault_index, default_flow_style=False, sort_keys=True
    )

    subs = [
        ("{BRIEF_MARKDOWN}", brief_md),
        ("{STATE_JSON}", state_json),
        ("{STEP_NUMBER}", str(step.number)),
        ("{STEP_NAME}", step.name),
        ("{STEP_SCOPE_FILES}", ", ".join(step.scope_files)),
        ("{STEP_SCOPE_OPERATIONS}", ", ".join(step.scope_operations)),
        ("{STEP_NOTES}", step.notes or ""),
        ("{CONTEXT_PATHS}", ", ".join(str(c) for c in brief.context_paths)),
        ("{VAULT_INDEX_YAML}", vault_index_yaml),
    ]
    out = template
    for token, value in subs:
        out = out.replace(token, value)
    return out


def _parse_stage_a_response(
    text: str, vault_index: dict[str, dict]
) -> list[str]:
    """Newline-split, strip, drop empties, filter to paths in vault_index
    (hallucination guard), then dedupe preserving first occurrence.

    The filter runs before the dedupe so the dedupe target is the
    surviving in-index set, not the raw response.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        p = line.strip()
        if not p or p not in vault_index:
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out
