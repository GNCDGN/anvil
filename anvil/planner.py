"""Planner — the real two-stage Opus-driven planner (design Parts 1-4 /
implementation-notes Component 3).

Stage A selects vault context files from a frontmatter index; Stage B
generates the JSON plan, with retry-once-with-error and escalation on a
second failure. `plan_step(brief, state, step_idx)` returns either a
validated `Plan` pydantic model or an escalation dict (`escalate: True`).
The caller checks `isinstance(result, dict) and result.get("escalate")`
first; otherwise it is a `Plan`.

The Phase 0 stub (hardcoded plans from a fixture) and the Phase 0
`validate_plan_scope` are removed here — scope is now enforced inside
`_validate_plan_structure` (checks 4 and 5) before any `Plan` is
constructed, so an out-of-scope plan becomes a planner-validation-failure
escalation rather than a separate orchestrator-side check.

`step_idx` is 0-based (the orchestrator loops `range(len(brief.steps))`);
`Plan.step_number` is 1-based.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Literal

import anthropic
import yaml
from pydantic import BaseModel

from anvil.brief import Step
from anvil.voice import load_voice_spec

log = logging.getLogger("anvil.planner")

# design Part 2: Stage A timeout is fixed at 30s (impl-notes Component 3).
# Stage B uses self.timeout (the configured planner_timeout).
_STAGE_A_TIMEOUT = 30


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


class Planner:
    """Two-stage planner. __init__ params are optional with defaults so
    Planner() still constructs (Phase 0 precedent, kept for the
    orchestrator default and the Step 5 retry tests). With no api_key the
    client is None; the system prompt is still loaded so tests that mock
    _call_anthropic at the method level work unchanged."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        vault_root=None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.vault_root = Path(vault_root) if vault_root else Path(".")
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else None
        # Voice substitution is one-shot at construction (design Part 1):
        # planner-system.md loaded once, {VOICE_SPEC} replaced by the live
        # spec (load_voice_spec() reads VAULT_PATH from the env — decision
        # #1, the Phase 0 vault_root shim is gone).
        system_path = (
            Path(__file__).resolve().parent / "prompts" / "planner-system.md"
        )
        voice_spec = load_voice_spec()
        self._system_prompt = system_path.read_text(encoding="utf-8").replace(
            "{VOICE_SPEC}", voice_spec
        )
        # Phase 4 Step 3: completion-artefacts prompt. Loaded once at
        # construction with {VOICE_SPEC} substituted, frozen for the
        # build lifetime. Separate from _system_prompt because the
        # discipline framing differs (drafting paperwork vs planning
        # a step).
        artefacts_path = (
            Path(__file__).resolve().parent / "prompts" / "planner-completion-artefacts.md"
        )
        self._artefacts_prompt = artefacts_path.read_text(encoding="utf-8").replace(
            "{VOICE_SPEC}", voice_spec
        )

    def plan_step(self, brief, state, step_idx: int):
        """Stage A -> Stage B (with retry). Returns a validated Plan, or an
        escalation dict (`escalate: True`) the orchestrator routes to the
        Telegram escalation path. Stage A failure / empty -> zero selected
        files; Stage B still sees the brief and state (design Part 2)."""
        vault_index = _build_vault_index(
            [str(p) for p in brief.context_paths], self.vault_root
        )
        stage_a_prompt = _assemble_stage_a_prompt(
            brief, state, step_idx, vault_index
        )
        stage_a_resp = self._call_anthropic(
            system=self._system_prompt, user=stage_a_prompt,
            timeout=_STAGE_A_TIMEOUT, step=step_idx + 1, stage="A",
        )
        selected = _parse_stage_a_response(stage_a_resp, vault_index)
        result = self._run_stage_b_with_retry(brief, state, step_idx, selected)
        if result.get("escalate"):
            return result
        return Plan(**result)

    # -----------------------------------------------------------------------
    # Phase 1 Step 5 — Anthropic call wrapper + Stage B retry loop
    #
    # Added to the existing Phase 0 class without touching plan_step (the
    # stub stays callable until Step 6 replaces it). Step 6 wires
    # self._system_prompt and self.vault_root in __init__; here they are
    # read defensively so this step's tests (which mock _call_anthropic at
    # the method level) do not depend on Step 6 wiring.
    # -----------------------------------------------------------------------

    def _call_anthropic(
        self, system: str, user: str, timeout: int, *, step: int, stage: str
    ) -> str:
        """Subprocess-free streaming call. Returns the model text, or ""
        on timeout / rate-limit / any error (never raises).

        SDK note (anthropic 0.102.0): messages.stream(...) is a context
        manager; the brief's stream.get_final_message().usage is reached
        via `with ... as stream`. RateLimitError / APITimeoutError are
        retried once after sleeping min(60, retry-after); a second
        failure returns "". A broad Exception is logged and returns "".
        """

        def _attempt() -> str:
            client = self._client.with_options(timeout=timeout)
            t0 = time.monotonic()
            with client.messages.stream(
                model=self.model,
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                final = stream.get_final_message()
            duration = time.monotonic() - t0
            text = "".join(
                b.text for b in final.content
                if getattr(b, "type", None) == "text"
            )
            u = final.usage
            log.info(
                f"[planner] step={step} stage={stage} model={self.model} "
                f"input_tokens={u.input_tokens} "
                f"output_tokens={u.output_tokens} "
                f"cache_creation_input_tokens="
                f"{u.cache_creation_input_tokens or 0} "
                f"cache_read_input_tokens="
                f"{u.cache_read_input_tokens or 0} "
                f"duration_s={duration:.1f}"
            )
            return text

        def _retry_after(exc: Exception) -> int:
            resp = getattr(exc, "response", None)
            hdr = getattr(resp, "headers", None)
            try:
                return int(hdr.get("retry-after")) if hdr else 1
            except (TypeError, ValueError):
                return 1

        try:
            try:
                return _attempt()
            except (anthropic.APITimeoutError, anthropic.RateLimitError) as e:
                time.sleep(min(60, _retry_after(e)))
                try:
                    return _attempt()
                except (
                    anthropic.APITimeoutError,
                    anthropic.RateLimitError,
                ) as e2:
                    log.error(
                        f"[planner] step={step} stage={stage} "
                        f"rate-limit/timeout twice ({e2}); returning empty"
                    )
                    return ""
        except Exception as e:  # noqa: BLE001 — never-raise contract
            log.error(
                f"[planner] step={step} stage={stage} call failed "
                f"({e}); returning empty"
            )
            return ""

    def _run_stage_b_with_retry(
        self, brief, state, step_idx: int, selected_paths: list[str]
    ) -> dict:
        """Stage B call -> parse -> validate, with one retry-with-error.
        Returns a validated plan dict, or a planner-validation-failure
        escalation block (design Part 3 retry block). Stage A is not
        re-run; an empty response is a tooling failure -> immediate
        escalation, no retry."""
        system = getattr(self, "_system_prompt", "")
        vault_root = getattr(self, "vault_root", Path("."))
        files = _load_files(selected_paths, vault_root)
        user_prompt = _assemble_stage_b_prompt(brief, state, step_idx, files)
        step_no = step_idx + 1

        response = self._call_anthropic(
            system=system, user=user_prompt, timeout=self.timeout,
            step=step_no, stage="B",
        )
        if not response:
            return _escalation_block(
                "planner-validation-failure",
                "Stage B returned empty after first attempt",
                step_idx,
            )
        try:
            plan = _parse_plan_json(response)
            _validate_plan_structure(plan, brief.steps[step_idx])
            return plan
        except (PlanParseError, PlanValidationError) as e:
            retry_prompt = (
                user_prompt
                + "\n\n## Previous attempt failed validation\n\n"
                + "The previous response failed validation with this "
                + "error:\n\n<validation_error>\n"
                + f"{e}\n</validation_error>\n\n"
                + "Produce a corrected JSON object that addresses the "
                + "error. Output only the JSON, no preamble."
            )
            response2 = self._call_anthropic(
                system=system, user=retry_prompt, timeout=self.timeout,
                step=step_no, stage="B",
            )
            if not response2:
                return _escalation_block(
                    "planner-validation-failure",
                    f"Stage B retry returned empty. First error: {e}",
                    step_idx,
                )
            try:
                plan = _parse_plan_json(response2)
                _validate_plan_structure(plan, brief.steps[step_idx])
                return plan
            except (PlanParseError, PlanValidationError) as e2:
                return _escalation_block(
                    "planner-validation-failure",
                    f"Plan validation failed twice. First: {e}. "
                    f"Second: {e2}",
                    step_idx,
                )


    # -----------------------------------------------------------------------
    # Phase 4 Step 4 — draft_completion_artefacts
    #
    # One-shot Stage-C call: drafts the setup-log entry and checkpoint body
    # from the completed build state. Reuses _call_anthropic plumbing; same
    # retry-once-with-error pattern as Stage B. Returns either the validated
    # draft dict or a completion-artefacts-draft-failed escalation.
    # -----------------------------------------------------------------------

    def draft_completion_artefacts(self, brief, state) -> dict:
        """Draft setup_log_entry and checkpoint body from completed state.

        Returns either:
          {"setup_log_entry": str, "checkpoint": str}
        or an escalation dict:
          {"escalate": True, "reason": "completion-artefacts-draft-failed",
           "detail": <error>, "step_number": 0}

        Never raises. Two API attempts max (initial + retry-with-error);
        a second failure escalates. step_number=0 signals post-build
        drafting, not a numbered build step.
        """
        system = getattr(self, "_artefacts_prompt", "")
        user_prompt = _assemble_artefacts_prompt(brief, state)

        response = self._call_anthropic(
            system=system, user=user_prompt, timeout=self.timeout or 120,
            step=0, stage="C",
        )
        if not response:
            return {
                "escalate": True,
                "reason": "completion-artefacts-draft-failed",
                "detail": "Stage C returned empty after first attempt",
                "step_number": 0,
            }
        try:
            draft = _parse_artefacts_json(response)
            _validate_artefacts_structure(draft)
            return draft
        except (PlanParseError, PlanValidationError) as e:
            retry_prompt = (
                user_prompt
                + "\n\n## Previous attempt failed validation\n\n"
                + "The previous response failed validation with this "
                + "error:\n\n<validation_error>\n"
                + f"{e}\n</validation_error>\n\n"
                + "Produce a corrected JSON object that addresses the "
                + "error. Output only the JSON, no preamble."
            )
            response2 = self._call_anthropic(
                system=system, user=retry_prompt, timeout=self.timeout or 120,
                step=0, stage="C",
            )
            if not response2:
                return {
                    "escalate": True,
                    "reason": "completion-artefacts-draft-failed",
                    "detail": f"Retry returned empty. First error: {e}",
                    "step_number": 0,
                }
            try:
                draft = _parse_artefacts_json(response2)
                _validate_artefacts_structure(draft)
                return draft
            except (PlanParseError, PlanValidationError) as e2:
                return {
                    "escalate": True,
                    "reason": "completion-artefacts-draft-failed",
                    "detail": (
                        f"Artefact validation failed twice. "
                        f"First: {e}. Second: {e2}"
                    ),
                    "step_number": 0,
                }


# ---------------------------------------------------------------------------
# Phase 4 Step 4 — artefact drafting helpers
#
# Module-level pure functions. Same shape as the Stage A/B helpers below.
# ---------------------------------------------------------------------------


def _assemble_artefacts_prompt(brief, state) -> str:
    """Build the user prompt for Stage C from brief + state.

    str.replace per placeholder (not str.format) — matches Stage A/B
    discipline so JSON braces in the embedded state don\'t break parsing.
    """
    try:
        brief_md = Path(state.brief_path).read_text(encoding="utf-8")
    except OSError:
        brief_md = ""

    state_json = state.model_dump_json(indent=2)
    deploy_block = (
        json.dumps(state.deploy, indent=2) if state.deploy
        else "(no deploy on this build)"
    )

    return (
        "## Build brief\n\n"
        "<brief>\n"
        f"{brief_md}\n"
        "</brief>\n\n"
        "## Final build state\n\n"
        "<state>\n"
        f"{state_json}\n"
        "</state>\n\n"
        "## Deploy outcome\n\n"
        f"{deploy_block}\n\n"
        "## Instruction\n\n"
        "Produce the completion artefacts as a JSON object matching "
        "the schema above. Output only the JSON, no preamble, no fences."
    )


def _parse_artefacts_json(text: str) -> dict:
    """json.loads the response; raise PlanParseError on JSONDecodeError.

    Reuses PlanParseError so the retry-once-with-error catch contract
    holds (Stage B and Stage C both raise PlanParseError on bad JSON).
    Markdown fences are NOT stripped — fenced output fails deliberately.
    """
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise PlanParseError(
            f"Stage C output is not valid JSON ({e}). Raw text:\n{text}"
        ) from e


def _validate_artefacts_structure(draft: dict) -> None:
    """Validate draft has exactly two non-empty string fields.

    Reuses PlanValidationError so the retry-with-error catch contract
    is the same shape as Stage B\'s. Also handles the escalation case:
    if the model returns an escalate block, we accept it as-is and
    the caller routes it.
    """
    if not isinstance(draft, dict):
        raise PlanValidationError("draft is not a dict")
    # Accept escalation block as-is
    if draft.get("escalate") is True:
        if not isinstance(draft.get("reason"), str):
            raise PlanValidationError("escalation missing or non-str reason")
        if not isinstance(draft.get("detail"), str):
            raise PlanValidationError("escalation missing or non-str detail")
        return None
    # Normal draft validation
    if set(draft.keys()) != {"setup_log_entry", "checkpoint"}:
        raise PlanValidationError(
            f"draft keys must be exactly {{\"setup_log_entry\", \"checkpoint\"}}; "
            f"got {sorted(draft.keys())}"
        )
    for key in ("setup_log_entry", "checkpoint"):
        if not isinstance(draft[key], str):
            raise PlanValidationError(f"{key} must be a string")
        if not draft[key].strip():
            raise PlanValidationError(f"{key} is empty")
    if not draft["setup_log_entry"].lstrip().startswith("## "):
        raise PlanValidationError(
            "setup_log_entry must start with \"## \" (the date heading)"
        )
    # Checkpoint body should start with a markdown heading (# or ##)
    if not draft["checkpoint"].lstrip().startswith(("#", "##")):
        raise PlanValidationError(
            "checkpoint must start with a markdown heading"
        )
    return None


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

    # Phase 2 Step 6 (decision #18 layer 2): append a
    # [disk-reconciliation-note] block when the brief carries
    # parse_warnings for THIS step. Stage B sees this through the
    # Stage A->B handoff and may surface the reconciliation in
    # escalation_triggers. The Stage A template itself is not
    # modified; the block is appended at assembly time, same posture
    # as Phase 1's {PRIOR_STEP_BLOCK} rendering. The block is omitted
    # entirely when no warning applies to the current step.
    relevant = [
        w for w in getattr(brief, "parse_warnings", []) or []
        if w.get("step_number") == step.number
    ]
    if relevant:
        lines = []
        for w in relevant:
            cm = w.get("closest_match")
            cm_text = f"'{cm}'" if cm else "(no single close match found)"
            lines.append(
                f"[disk-reconciliation-note] Brief step {w['step_number']} "
                f"references '{w['path']}'; this path does not exist at "
                f"target_repo_path. Closest match on disk: {cm_text}. "
                "The Coder will reconcile at execute time; you may want "
                "to flag this in escalation_triggers."
            )
        out = out.rstrip() + "\n\n" + "\n".join(lines) + "\n"

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


# ---------------------------------------------------------------------------
# Phase 1 Stage B — file loader, prompt assembly, JSON parse, validation
#
# Added alongside the Phase 0 stub and Stage A (above). The Phase 0
# validate_plan_scope (Plan-object -> bool) stays callable; it is the
# orchestrator's Phase 0 path, retired in Step 6 with the stub. The new
# _validate_plan_structure (raw dict -> raise) is the Phase 1 entry point
# and is independent of pydantic so its errors thread cleanly into the
# Step 5 retry prompt. No Anthropic call here; _call_anthropic is Step 5.
# ---------------------------------------------------------------------------

_STAGE_B_TEMPLATE = _PROMPTS_DIR / "planner-stage-b.md"
_TRUNCATION_MARKER = "\n\n[... truncated at 50000 chars]"

_REQUIRED_PLAN_FIELDS = (
    "step_number",
    "step_name",
    "files_to_touch",
    "operations",
    "approach",
    "smoke_test",
    "expected_outcome",
    "commit_message",
    "scope_boundaries",
    "confidence",
    "escalation_triggers",
)
_VALID_CONFIDENCE = {"high", "medium", "low"}


class PlanParseError(Exception):
    """Stage B output was not parseable as a single JSON object. Carries
    the raw text. Subclasses Exception (not PlannerError) so a broad
    `except PlannerError` cannot swallow it; Step 5's retry catches
    (PlanParseError, PlanValidationError)."""


class PlanValidationError(Exception):
    """Stage B output parsed as JSON but failed structural validation (or
    was a malformed escalation block). Same catch contract as
    PlanParseError."""


def _load_files(
    paths: list[str], vault_root: Path, max_chars_per_file: int = 50_000
) -> dict[str, str]:
    """{path: content} for each readable path. Over the cap, content is
    truncated to the cap and the literal design marker is appended.
    Missing or unreadable files are logged and omitted (never raised) --
    Stage A's index can be stale relative to disk by the time Stage B
    runs, so a vanished file degrades the context, it does not abort.
    """
    vault_root = Path(vault_root)
    out: dict[str, str] = {}
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            p = vault_root / p
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as e:
            log.warning(f"[planner] selected file unreadable, omitting: {raw} ({e})")
            continue
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file] + _TRUNCATION_MARKER
        out[raw] = content
    return out


def _prior_step_block(state, step_idx: int) -> str:
    """design Part 3 {PRIOR_STEP_BLOCK}. step_idx == 0 -> the literal
    first-step string; otherwise a structured block from
    state.steps[step_idx - 1]. coder output is read defensively
    (coder_output then the on-disk coder_result -- decision #6, the
    Step 7 rename is deferred)."""
    if step_idx == 0:
        return "(none — this is the first step)"
    prior = state.steps[step_idx - 1]
    plan_json = json.dumps(prior.plan) if prior.plan else "(no plan persisted)"
    coder_out = (
        getattr(prior, "coder_output", None)
        or getattr(prior, "coder_result", None)
        or "(manual mode — no output captured)"
    )
    return (
        f"Step {prior.n}: {prior.name}\n"
        f"Plan: {plan_json}\n"
        f"Coder output: {coder_out}\n"
        f"Smoke test result: {prior.smoke or '(none)'}\n"
        f"Commit hash: {prior.commit or '(none)'}"
    )


def _assemble_stage_b_prompt(brief, state, step_idx: int, files: dict[str, str]) -> str:
    """Load planner-stage-b.md and substitute placeholders.

    str.replace per placeholder in a fixed order, NOT str.format: the
    template plus the embedded JSON state and vault-file blocks contain
    literal { } braces that would break str.format. Do not "simplify"
    this to .format.

    {VOICE_SPEC} is not handled here (system-prompt concern, Step 6).
    {STATE_JSON} uses model_dump_json (decision #5). {BRIEF_MARKDOWN}
    source is state.brief_path; unreadable -> "" (never-raise).
    """
    template = _STAGE_B_TEMPLATE.read_text(encoding="utf-8")
    step = brief.steps[step_idx]

    try:
        brief_md = Path(state.brief_path).read_text(encoding="utf-8")
    except OSError:
        brief_md = ""

    if files:
        vault_files_blocks = "".join(
            f'<vault_file path="{p}">\n{c}\n</vault_file>\n\n'
            for p, c in files.items()
        )
    else:
        vault_files_blocks = "(none selected)"

    subs = [
        ("{BRIEF_MARKDOWN}", brief_md),
        ("{STATE_JSON}", state.model_dump_json(indent=2)),
        ("{PRIOR_STEP_BLOCK}", _prior_step_block(state, step_idx)),
        ("{VAULT_FILES_BLOCKS}", vault_files_blocks),
        ("{STEP_NUMBER}", str(step.number)),
        ("{STEP_NAME}", step.name),
        ("{STEP_SCOPE_FILES}", ", ".join(step.scope_files)),
        ("{STEP_SCOPE_OPERATIONS}", ", ".join(step.scope_operations)),
        ("{STEP_NOTES}", step.notes or ""),
        ("{CONTEXT_PATHS}", ", ".join(str(c) for c in brief.context_paths)),
    ]
    out = template
    for token, value in subs:
        out = out.replace(token, value)
    return out


def _parse_plan_json(text: str) -> dict:
    """Strip surrounding whitespace, json.loads. Raise PlanParseError on
    JSONDecodeError with the raw text in the message. Markdown fences are
    NOT stripped -- fenced output fails parsing deliberately so the model
    does not drift toward emitting fences."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError as e:
        raise PlanParseError(
            f"Stage B output is not valid JSON ({e}). Raw text:\n{text}"
        ) from e


def _validate_plan_structure(plan: dict, brief_step: Step) -> None:
    """The eight design Part 3 checks, in order, raising
    PlanValidationError on the first failure. Returns None on success.

    Operates on the raw dict, not a Plan model -- the caller constructs
    Plan(**plan) only after this passes. brief_step is anvil.brief.Step
    (design's "BriefStep"); its scope is the flat scope_files /
    scope_operations attrs (design's brief_step.scope.files shorthand),
    same as Stage A.
    """
    # 1. Escalation short-circuit.
    if plan.get("escalate") is True:
        if not isinstance(plan.get("reason"), str):
            raise PlanValidationError("escalation missing or non-str reason")
        if not isinstance(plan.get("detail"), str):
            raise PlanValidationError("escalation missing or non-str detail")
        if not isinstance(plan.get("step_number"), int):
            raise PlanValidationError("escalation missing or non-int step_number")
        if "options" in plan and not (
            isinstance(plan["options"], list)
            and all(isinstance(o, str) for o in plan["options"])
        ):
            raise PlanValidationError("escalation options must be list[str]")
        return None

    # 2. Required fields present.
    for name in _REQUIRED_PLAN_FIELDS:
        if name not in plan:
            raise PlanValidationError(f"missing field: {name}")

    # 3. step_number matches the brief step.
    if plan["step_number"] != brief_step.number:
        raise PlanValidationError(
            f"step_number mismatch: plan {plan['step_number']} "
            f"vs brief {brief_step.number}"
        )

    # 4. files_to_touch within declared scope (literal-equal, no globs).
    for path in plan["files_to_touch"]:
        if path not in brief_step.scope_files:
            raise PlanValidationError(f"out-of-scope file: {path}")

    # 5. operations within declared scope.
    for op in plan["operations"]:
        if op not in brief_step.scope_operations:
            raise PlanValidationError(f"out-of-scope operation: {op}")

    # 6. confidence in the allowed set.
    if plan["confidence"] not in _VALID_CONFIDENCE:
        raise PlanValidationError(f"invalid confidence: {plan['confidence']}")

    # 7. escalation_triggers is list[str] (may be empty).
    et = plan["escalation_triggers"]
    if not (isinstance(et, list) and all(isinstance(x, str) for x in et)):
        raise PlanValidationError("escalation_triggers must be list[str]")

    # 8. scope_boundaries is a dict with str in_scope / out_of_scope.
    sb = plan["scope_boundaries"]
    if not (
        isinstance(sb, dict)
        and isinstance(sb.get("in_scope"), str)
        and isinstance(sb.get("out_of_scope"), str)
    ):
        raise PlanValidationError(
            "scope_boundaries must be a dict with str in_scope and out_of_scope"
        )
    return None


def _escalation_block(reason: str, detail: str, step_idx: int) -> dict:
    """design Part 3 escalation shape. No `options` field:
    planner-validation-failure escalations carry no choice for Genco,
    only an abort decision. step_number is 1-indexed (brief) from the
    0-indexed step_idx."""
    return {
        "escalate": True,
        "reason": reason,
        "detail": detail,
        "step_number": step_idx + 1,
    }
