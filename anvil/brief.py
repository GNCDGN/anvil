"""Brief parsing and schema validation (implementation-notes Component 2).

Models are Pydantic v2 per the brief's Step 3 note ("implement Step, Brief,
EndToEndTest Pydantic models per implementation-notes Component 2"). Component
2's code sketch shows `@dataclass`; the Step 3 note is the authority and says
Pydantic, and requirements.txt pins pydantic>=2.0 — reconciled to Pydantic.

`validate_or_reject` collects ALL violations and raises a single
`BriefValidationError` carrying the full list (never just the first).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from anvil.errors import BriefValidationError

_VALID_OPERATIONS = {"read", "write", "smoke-test", "commit", "shell"}
# v4 Phase 1a Step 2 (Q-A4): operations that engage the Coder LLM (Claude Code
# receives these as tool grants — coder.py:178-190: read/write → read & edit
# tools, shell → Bash; smoke-test/commit are orchestrator-owned and silently
# dropped from the Coder grant set). A per-step `model:` only bears on steps
# with at least one of these; declared on a purely orchestrator-owned step it
# is accepted-with-warning, never rejected. Single source of truth so future
# operations can be added cleanly.
LLM_CALLING_OPERATIONS = frozenset({"read", "write", "shell"})
# v4 Phase 1b Step 1 (Q-B3): the per-step `issues:` connector-scope axis. A
# separate axis from _VALID_OPERATIONS (Coder tool grants) and from `model:`
# (Planner routing) — a step may declare any combination, validated
# independently (rule 14). `write` permits reads at the connector wrapper; the
# brief field itself is one of these two literals (or absent → None).
ISSUES_SCOPES = frozenset({"read", "write"})
# v4 Phase 1b Step 2 (Q-B3): the per-step `sentry:` connector-scope axis. The
# Sentry connector is read-only (no write methods), so `read` is the only valid
# scope; validated in rule 15. Like ISSUES_SCOPES, a separate axis from model:
# and scope.operations — validated independently.
SENTRY_SCOPES = frozenset({"read"})
# v4 Phase 1c Step 1 (Q-C3): the per-brief `deploy_target:` frontmatter field —
# the deploy-target axis, extending the legacy required `vps_deploy:` bool. A
# brief declares at most one target. "none"/""/"null" normalize to None at parse
# (the model:/issues:/sentry: defensive precedent), so "none" is documented
# vocabulary the parser maps to absent rather than a stored value. Validated in
# rule 16; vps_deploy: coexistence is a warning (vps-deploy-deprecated /
# deploy-target-mismatch), never a rejection.
DEPLOY_TARGETS = frozenset({"vps", "vercel", "netlify", "none"})
_REQUIRED_FRONTMATTER = (
    "brief_version",
    "project",
    "build_name",
    "target_repo",
    "target_repo_path",
    "vps_deploy",
)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


class Step(BaseModel):
    number: int
    name: str
    scope_files: list[str]
    scope_operations: list[str]
    smoke: str
    confirm: Literal["explicit", "auto"]
    commit_message_hint: str | None = None
    notes: str | None = None
    # v4 Phase 1a Step 2: optional per-step model selection (opt-in; absent →
    # None → the v3 default model). Validated in validate_or_reject rule 13
    # against routing.MODEL_ALIASES ∪ events.MODEL_RATES; consumed at the
    # Planner/subtask call sites in Step 3. brief_version stays 1 — this is a
    # backwards-compatible addition, not a migration.
    model: str | None = None
    # v4 Phase 1b Step 1 (Q-B3): optional per-step GitHub Issues connector scope
    # (opt-in; absent → None → the step declares no issues access). Validated in
    # validate_or_reject rule 14 against ISSUES_SCOPES; enforced at the connector
    # wrapper (integrations.github_issues) at call time. Orthogonal to model: and
    # scope.operations — a step may declare any combination. brief_version stays 1.
    issues: str | None = None
    # v4 Phase 1b Step 2 (Q-B3): optional per-step Sentry connector scope
    # (opt-in; absent → None). Validated in rule 15 against SENTRY_SCOPES
    # ({"read"} — the connector is read-only); enforced at the connector wrapper
    # (integrations.sentry). Orthogonal to model:, issues:, and scope.operations.
    sentry: str | None = None


class EndToEndTest(BaseModel):
    script: str
    expected_exit: int = 0
    on_fail: str = "escalate"


class Brief(BaseModel):
    brief_version: int
    project: str
    build_name: str
    target_repo: str
    target_repo_path: Path
    vps_deploy: Literal["yes", "no"]
    service_name: str | None = None
    # Phase 3 Step 1: VPS-side path for deploy chain (cd here, then git pull --ff-only)
    vps_target_path: str | None = None
    # v4 Phase 1c Step 1 (Q-C3): optional per-brief deploy target (opt-in;
    # absent → None → derive from vps_deploy). Validated in rule 16 against
    # DEPLOY_TARGETS; coexists with the legacy required vps_deploy: (warnings,
    # not errors). Parsed-not-consumed in first-pass — the orchestrator's deploy
    # chain still reads vps_deploy/vps_target_path directly (available-but-not-
    # consumed; Step 1 does not wire this into the deploy loop).
    deploy_target: str | None = None
    goal: str = ""
    context_links: list[str] = []
    context_paths: list[Path] = []
    steps: list[Step] = []
    end_to_end_test: EndToEndTest | None = None
    # Phase 2 Step 6 (decision #18 layer 1): scope.files paths that
    # don't exist at target_repo_path AND aren't write-targets of any
    # step land here as warnings (not validation errors). Each entry:
    # {'kind': 'path-not-found', 'step_number': int, 'path': str,
    #  'closest_match': str | None}.
    parse_warnings: list[dict] = []


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _sections(body: str) -> dict[str, str]:
    """Split markdown body into top-level ## sections (keyed by lowercased
    heading text)."""
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h and not line.startswith("###"):
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur = h.group(1).strip().lower()
            buf = []
        else:
            if cur is not None:
                buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def _clean(v: str) -> str:
    return v.strip().strip("`").strip()


def _norm_yes_no(raw: object) -> str:
    """Normalise a vps_deploy value to 'yes'/'no'. YAML 1.1 parses unquoted
    yes/no as booleans, so handle bool as well as string forms; anything
    unrecognised defaults to 'no' (rule-4-safe)."""
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    s = str(raw).strip().lower()
    return s if s in ("yes", "no") else "no"


def _norm_deploy_target(raw: object) -> str | None:
    """Normalise a deploy_target value. Absent / "" / "null" / "none" → None
    (the model:/issues:/sentry: defensive precedent — "none" is documented
    vocabulary that maps to absent, not a stored value). Otherwise the lowercased
    string (rule 16 validates membership in DEPLOY_TARGETS)."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return None if s in ("", "null", "none") else s


def _field(block: str, key: str) -> str | None:
    m = re.search(
        rf"^\s*-\s*\*\*{re.escape(key)}:\*\*\s*(.*)$", block, re.M
    )
    return _clean(m.group(1)) if m else None


def _csv(val: str | None) -> list[str]:
    if not val:
        return []
    if val.lower().startswith("(none"):
        return []
    return [p.strip().strip("`") for p in val.split(",") if p.strip()]


def _parse_steps(steps_section: str) -> list[Step]:
    steps: list[Step] = []
    # Split on "### Step N — Name" headers, keeping each block.
    parts = re.split(r"^###\s+Step\s+(\d+)\s*[—-]\s*(.+?)\s*$",
                     steps_section, flags=re.M)
    # parts[0] is preamble; then repeating (number, name, block)
    for i in range(1, len(parts), 3):
        number = int(parts[i])
        name = parts[i + 1].strip()
        block = parts[i + 2]
        confirm = (_field(block, "confirm") or "").lower()
        # v4 Phase 1a Step 2: optional per-step model. Empty ("") and the
        # literal "null"/"none" are treated as absent (None) — the defensive
        # contract; an absent field already yields None from _field.
        raw_model = _field(block, "model")
        model = (
            raw_model
            if (raw_model and raw_model.lower() not in ("null", "none"))
            else None
        )
        # v4 Phase 1b Step 1 (Q-B3): per-step `issues:` connector scope. Same
        # defensive contract as model: empty ("") and the literal "null"/"none"
        # are treated as absent (None); an absent field already yields None.
        raw_issues = _field(block, "issues")
        issues = (
            raw_issues
            if (raw_issues and raw_issues.lower() not in ("null", "none"))
            else None
        )
        # v4 Phase 1b Step 2 (Q-B3): per-step `sentry:` connector scope. Same
        # defensive contract as model: / issues:.
        raw_sentry = _field(block, "sentry")
        sentry = (
            raw_sentry
            if (raw_sentry and raw_sentry.lower() not in ("null", "none"))
            else None
        )
        steps.append(
            Step(
                number=number,
                name=name,
                scope_files=_csv(_field(block, "scope.files")),
                scope_operations=_csv(_field(block, "scope.operations")),
                smoke=_field(block, "smoke") or "",
                confirm=confirm if confirm in ("explicit", "auto") else "explicit",
                commit_message_hint=_field(block, "commit_message_hint"),
                notes=_field(block, "notes"),
                model=model,
                issues=issues,
                sentry=sentry,
            )
        )
    return steps


def _parse_context(ctx_section: str) -> list[str]:
    if not ctx_section or ctx_section.strip().lower().startswith("(none"):
        return []
    return _WIKILINK_RE.findall(ctx_section)


def _parse_e2e(section: str | None) -> EndToEndTest | None:
    if not section or not section.strip():
        return None
    script = _field(section, "script")
    if not script:
        return None
    exp = _field(section, "expected_exit")
    on_fail = _field(section, "on_fail")
    return EndToEndTest(
        script=script,
        expected_exit=int(exp) if exp and exp.isdigit() else 0,
        on_fail=on_fail or "escalate",
    )


def _basename_match(repo: Path, target: str) -> str | None:
    """Walk repo (excluding .git/__pycache__/.venv/node_modules) for a
    file with the same basename as `target`. Return the relative path
    of the single match, or None if zero or multiple matches exist.
    Deterministic: walks in sorted order so the same input always
    produces the same answer."""
    base = Path(target).name
    excluded = {".git", "__pycache__", ".venv", "node_modules"}
    hits: list[str] = []
    try:
        for p in sorted(repo.rglob(base)):
            if any(seg in excluded for seg in p.parts):
                continue
            if not p.is_file():
                continue
            try:
                hits.append(str(p.relative_to(repo)))
            except ValueError:
                continue
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def _compute_parse_warnings(brief: Brief) -> list[dict]:
    """Phase 2 Step 6 (decision #18): for each step's scope.files,
    warn if the path doesn't exist at target_repo_path AND isn't a
    write-target of any step. Files the build creates would otherwise
    be falsely warned about.

    Returns a list of warning dicts; the caller assigns them to
    brief.parse_warnings AND emits the human-readable line via
    _emit_parse_warnings.

    target_repo_path is checked for existence; if it doesn't exist
    yet (validation will catch that separately), the warning pass is
    skipped entirely — no false-positive flood of "everything missing".
    """
    repo = brief.target_repo_path
    if not repo.is_dir():
        return []
    write_targets: set[str] = set()
    for step in brief.steps:
        if "write" in step.scope_operations:
            write_targets.update(step.scope_files)
    warnings: list[dict] = []
    for step in brief.steps:
        for sf in step.scope_files:
            if (repo / sf).exists():
                continue
            if sf in write_targets:
                continue
            warnings.append({
                "kind": "path-not-found",
                "step_number": step.number,
                "path": sf,
                "closest_match": _basename_match(repo, sf),
            })
    return warnings


def _compute_model_warnings(brief: Brief) -> list[dict]:
    """v4 Phase 1a Step 2 (Q-A4): a per-step `model:` selection only bears on
    steps whose scope.operations engage the Coder LLM (read/write/shell, per
    LLM_CALLING_OPERATIONS). Declared on a step that is purely orchestrator-
    owned ops (smoke-test/commit), the field is accepted but does nothing, so
    warn — never reject (the never-raises substrate). Repo-independent (unlike
    _compute_parse_warnings); surfaces via the same _emit_parse_warnings
    channel (stderr + the anvil logger / run log)."""
    warnings: list[dict] = []
    for step in brief.steps:
        if step.model is None:
            continue
        if set(step.scope_operations) & LLM_CALLING_OPERATIONS:
            continue
        warnings.append({
            "kind": "model-on-non-llm-step",
            "step_number": step.number,
            "model": step.model,
            "operations": list(step.scope_operations),
        })
    return warnings


def _compute_deploy_target_warnings(brief: Brief) -> list[dict]:
    """v4 Phase 1c Step 1 (Q-C3): the deploy_target: / vps_deploy: coexistence.
    Two non-rejecting warnings (deploy_target wins; vps_deploy is the deprecated
    legacy bool the deploy chain still reads directly):
    - `vps-deploy-deprecated`: vps_deploy: yes with no deploy_target: declared —
      treat as deploy_target: vps; declare deploy_target: vps instead (vps_deploy
      support is dropped in Phase 2).
    - `deploy-target-mismatch`: deploy_target: declared but disagreeing with
      vps_deploy: — i.e. (deploy_target == 'vps') != (vps_deploy == 'yes').
      deploy_target wins; the warning flags the inconsistency to reconcile.
    Neither rejects; repo-independent; surfaces via _emit_parse_warnings."""
    warnings: list[dict] = []
    dt = brief.deploy_target
    vps_yes = brief.vps_deploy == "yes"
    if dt is None:
        if vps_yes:
            warnings.append({"kind": "vps-deploy-deprecated"})
    elif (dt == "vps") != vps_yes:
        warnings.append({
            "kind": "deploy-target-mismatch",
            "deploy_target": dt,
            "vps_deploy": brief.vps_deploy,
        })
    return warnings


def _emit_parse_warnings(warnings: list[dict]) -> None:
    """Emit each warning to stderr and to the anvil logger. Stderr
    line shape matches the brief's spec:
      [brief-warning] step N: scope.files entry 'X' does not exist
      at target_repo_path; closest match: 'Y'. Continuing; the Coder
      will reconcile at execute time."""
    if not warnings:
        return
    import logging
    log = logging.getLogger("anvil.brief")
    for w in warnings:
        if w.get("kind") == "model-on-non-llm-step":
            line = (
                f"[brief-warning] step {w['step_number']}: model "
                f"'{w['model']}' is declared but scope.operations "
                f"{sorted(w['operations'])} include no LLM-calling operation "
                "(read/write/shell); the model selection will not affect this "
                "step's substantive work. Continuing; the brief still validates."
            )
        elif w.get("kind") == "vps-deploy-deprecated":
            line = (
                "[brief-warning] vps_deploy: yes is deprecated; declare "
                "deploy_target: vps instead (treating as deploy_target: vps for "
                "the deploy chain, which still reads vps_deploy directly). "
                "vps_deploy support is dropped in Phase 2. The brief still validates."
            )
        elif w.get("kind") == "deploy-target-mismatch":
            line = (
                f"[brief-warning] deploy_target: {w['deploy_target']!r} conflicts "
                f"with vps_deploy: {w['vps_deploy']!r}; deploy_target wins. "
                "Reconcile the frontmatter. The brief still validates."
            )
        else:  # path-not-found
            cm = w.get("closest_match")
            cm_text = f"'{cm}'" if cm else "(none)"
            line = (
                f"[brief-warning] step {w['step_number']}: scope.files "
                f"entry '{w['path']}' does not exist at target_repo_path; "
                f"closest match: {cm_text}. Continuing; the Coder will "
                "reconcile at execute time."
            )
        print(line, file=sys.stderr)
        log.warning(line)


def parse_brief_raw(path: Path) -> tuple[Brief, dict]:
    """Parse a brief markdown file. Returns (Brief, raw_frontmatter). The raw
    frontmatter dict lets validate_or_reject's rule 1 distinguish a literally
    absent key from a parse-time default. Does not validate."""
    text = Path(path).read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    sections = _sections(body)
    brief = Brief(
        brief_version=fm.get("brief_version", 0),
        project=str(fm.get("project", "")),
        build_name=str(fm.get("build_name", "")),
        target_repo=str(fm.get("target_repo", "")),
        target_repo_path=Path(
            str(fm.get("target_repo_path", "")) or "."
        ).expanduser(),
        vps_deploy=_norm_yes_no(fm.get("vps_deploy", "no")),
        service_name=(
            str(fm["service_name"]) if fm.get("service_name") else None
        ),
        vps_target_path=(
            str(fm["vps_target_path"]) if fm.get("vps_target_path") else None
        ),
        deploy_target=_norm_deploy_target(fm.get("deploy_target")),
        goal=sections.get("goal", ""),
        context_links=_parse_context(sections.get("context", "")),
        steps=_parse_steps(sections.get("steps", "")),
        end_to_end_test=_parse_e2e(sections.get("end-to-end test")),
    )
    # Phase 2 Step 6 (decision #18 layer 1): compute parse-time path
    # warnings and attach them to the Brief. Emit each to stderr +
    # logger so the build session sees them before Stage A runs.
    warnings = (
        _compute_parse_warnings(brief)
        + _compute_model_warnings(brief)
        + _compute_deploy_target_warnings(brief)
    )
    if warnings:
        brief = brief.model_copy(update={"parse_warnings": warnings})
        _emit_parse_warnings(warnings)
    return brief, (fm if isinstance(fm, dict) else {})


def parse_brief(path: Path) -> Brief:
    """Parse a brief markdown file into a Brief (Component 2 API). Does not
    validate — call validate_or_reject for that."""
    brief, _ = parse_brief_raw(path)
    return brief


# ---------------------------------------------------------------------------
# Validation — 16 rules. Rules 1-12 are implementation-notes Component 2; rule
# 13 (per-step model:, v4 Phase 1a Step 2), rule 14 (issues:, v4 Phase 1b Step
# 1), rule 15 (sentry:, v4 Phase 1b Step 2), and rule 16 (per-brief
# deploy_target:, v4 Phase 1c Step 1) extend it backwards-compatibly.
# ---------------------------------------------------------------------------

def _is_git_repo(path: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


def _escapes(repo: Path, rel: str) -> bool:
    """True if `rel` is absolute-outside-repo or `..`-escapes the repo."""
    p = Path(rel)
    if p.is_absolute():
        try:
            p.resolve().relative_to(repo.resolve())
            return False
        except ValueError:
            return True
    try:
        (repo / p).resolve().relative_to(repo.resolve())
        return False
    except ValueError:
        return True


def _script_exists(repo: Path, script: str) -> bool:
    """True if `script` resolves to an existing file — relative to `repo`, or
    as an absolute / ~-expanded path. Never raises: a value that is not a
    valid path (prose, an over-long single component → os.stat OSError
    [Errno 63] File name too long, or an embedded NUL → ValueError) yields
    False so the caller rejects it cleanly rather than crashing — the
    reject-before-any-agent-runs / never-raises validator contract.
    v4 Phase 1a housekeeping (surfaced by the 2026-05-20-anvil-v2-phase-1
    brief, whose end_to_end_test.script was a prose sentence)."""
    try:
        if (repo / script).is_file():
            return True
    except (OSError, ValueError):
        pass
    try:
        if Path(script).expanduser().is_file():
            return True
    except (OSError, ValueError):
        pass
    return False


def validate_or_reject(
    brief: Brief, raw_frontmatter: dict | None = None,
    vault_root: Path | None = None,
) -> None:
    """Raise BriefValidationError listing ALL violations, or return None.

    `raw_frontmatter` lets rule 1 see which required keys were literally
    absent (parse_brief defaults them). `vault_root` enables rule 11
    (context wiki-link resolution); Component 2's signature is
    validate_or_reject(brief) — vault_root is an optional extension since
    rule 11 inherently needs vault context (resolve_context_paths owns the
    hard resolution).
    """
    e: list[str] = []
    fm = raw_frontmatter if raw_frontmatter is not None else {}

    # 1. Frontmatter completeness
    for k in _REQUIRED_FRONTMATTER:
        if raw_frontmatter is not None:
            if k not in fm or fm.get(k) in (None, ""):
                e.append(f"frontmatter: required key '{k}' missing or empty")
        else:
            if getattr(brief, k, None) in (None, "", 0) and k != "brief_version":
                e.append(f"frontmatter: required key '{k}' missing or empty")

    # 2. brief_version == 1
    if brief.brief_version != 1:
        e.append(f"brief_version must be 1 (got {brief.brief_version})")

    # 3. target_repo_path exists and is a git repo
    if not brief.target_repo_path.exists():
        e.append(f"target_repo_path does not exist: {brief.target_repo_path}")
    elif not _is_git_repo(brief.target_repo_path):
        e.append(f"target_repo_path is not a git repo: {brief.target_repo_path}")

    # 4. vps_deploy: yes requires service_name AND vps_target_path (Phase 3 Step 1)
    if brief.vps_deploy == "yes" and not brief.service_name:
        e.append("vps_deploy is 'yes' but service_name is not set")
    if brief.vps_deploy == "yes" and not brief.vps_target_path:
        e.append("vps_deploy is 'yes' but vps_target_path is not set")

    # 5. At least one step
    if not brief.steps:
        e.append("brief has no steps")

    # 6. Step numbers unique, starting at 1, sequential
    nums = [s.number for s in brief.steps]
    if nums and nums != list(range(1, len(nums) + 1)):
        e.append(f"step numbers must be 1..N sequential, got {nums}")

    for s in brief.steps:
        tag = f"step {s.number} ({s.name})"
        # 7. scope.files within target_repo_path
        for f in s.scope_files:
            if _escapes(brief.target_repo_path, f):
                e.append(f"{tag}: scope.files path escapes target_repo_path: {f}")
        # 8. scope.operations non-empty subset of the allowed set
        if not s.scope_operations:
            e.append(f"{tag}: scope.operations is empty")
        bad_ops = set(s.scope_operations) - _VALID_OPERATIONS
        if bad_ops:
            e.append(f"{tag}: unknown operations {sorted(bad_ops)}")
        # 9. smoke is an existing script path or an inline command
        sm = s.smoke.strip()
        if not sm:
            e.append(f"{tag}: smoke is empty")
        elif (
            " " not in sm and sm.endswith(".sh")
            and not (brief.target_repo_path / sm).is_file()
            and not Path(sm).expanduser().is_file()
        ):
            e.append(f"{tag}: smoke looks like a script path but does not exist: {sm}")
        # 10. confirm is explicit|auto (Step model already constrains; double-check)
        if s.confirm not in ("explicit", "auto"):
            e.append(f"{tag}: confirm must be explicit|auto, got {s.confirm!r}")

    # 11. Context wiki-links resolve (needs vault context)
    if vault_root is not None:
        for link in brief.context_links:
            if _resolve_one(link, vault_root) is None:
                e.append(f"context wiki-link does not resolve: [[{link}]]")

    # 12. end_to_end_test.script exists if declared. _script_exists is
    # OSError/ValueError-tolerant: a non-path value (prose, over-long string,
    # embedded NUL) becomes a clean rejection here, not a crash on os.stat.
    if brief.end_to_end_test is not None:
        sp = brief.end_to_end_test.script
        if not _script_exists(brief.target_repo_path, sp):
            e.append(f"end_to_end_test.script does not exist: {sp}")

    # 13. v4 Phase 1a Step 2: per-step model: must be a known alias or a known
    # version string. MODEL_ALIASES (anvil.routing) and MODEL_RATES
    # (anvil.events) are the single sources of truth — no hardcoded list.
    # Unknown names reject before any agent runs (design Part 6); Sonnet was
    # dropped from MODEL_ALIASES (Amendment 1), so `sonnet` is unknown here.
    # The model:-on-non-LLM-step case is a warning (Q-A4), not an error, and
    # is handled at parse time (_compute_model_warnings). Imported locally to
    # keep the pure-parse path free of routing/anthropic (brief.py's existing
    # deferred-import idiom, e.g. _emit_parse_warnings' logging import).
    declared = [(s, s.model) for s in brief.steps if s.model]
    if declared:
        from anvil.events import MODEL_RATES
        from anvil.routing import MODEL_ALIASES
        known = set(MODEL_ALIASES) | set(MODEL_RATES)
        for s, m in declared:
            if m not in known:
                e.append(
                    f"step {s.number} ({s.name}): model {m!r} is not a known "
                    f"alias {sorted(MODEL_ALIASES)} or version string "
                    f"{sorted(MODEL_RATES)}"
                )

    # 14. v4 Phase 1b Step 1 (Q-B3): per-step `issues:` connector scope must be
    # a known scope (ISSUES_SCOPES) or absent. A separate axis from rule 13's
    # model: (Planner routing) and from scope.operations (rule 8, Coder tool
    # grants) — validated independently, so a step may carry any combination.
    # Fixed value set; no import needed (unlike rule 13). Unknown values reject
    # before any agent runs — an invalid scope must never reach the connector
    # wrapper, which enforces the declared scope at call time.
    for s in brief.steps:
        if s.issues is not None and s.issues not in ISSUES_SCOPES:
            e.append(
                f"step {s.number} ({s.name}): issues {s.issues!r} is not a "
                f"valid scope {sorted(ISSUES_SCOPES)}"
            )

    # 15. v4 Phase 1b Step 2 (Q-B3): per-step `sentry:` connector scope must be
    # a known scope (SENTRY_SCOPES = {"read"} — the connector is read-only) or
    # absent. Same independent-axis discipline as rule 14; unknown values (incl.
    # "write", which the read-only connector has no method for) reject before any
    # agent runs.
    for s in brief.steps:
        if s.sentry is not None and s.sentry not in SENTRY_SCOPES:
            e.append(
                f"step {s.number} ({s.name}): sentry {s.sentry!r} is not a "
                f"valid scope {sorted(SENTRY_SCOPES)}"
            )

    # 16. v4 Phase 1c Step 1 (Q-C3): per-brief deploy_target: must be a known
    # target (DEPLOY_TARGETS) or absent. "none"/""/null normalize to None at
    # parse (the model:/issues:/sentry: defensive precedent), so this rule only
    # sees vps/vercel/netlify or an unknown value. The vps_deploy: coexistence is
    # a warning (vps-deploy-deprecated / deploy-target-mismatch), not a rule-16
    # rejection — deploy_target is a separate axis, validated independently.
    if brief.deploy_target is not None and brief.deploy_target not in DEPLOY_TARGETS:
        e.append(
            f"deploy_target {brief.deploy_target!r} is not a known target "
            f"{sorted(DEPLOY_TARGETS)}"
        )

    if e:
        raise BriefValidationError(e)


def _resolve_one(link: str, vault_root: Path) -> Path | None:
    link = link.strip().lstrip("/")
    candidates = [
        vault_root / link,
        vault_root / f"{link}.md",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # basename match anywhere in the vault (Obsidian-style short links)
    base = Path(link).name
    for ext in ("", ".md"):
        hits = list(vault_root.rglob(f"{base}{ext}"))
        if hits:
            return hits[0]
    return None


def resolve_context_paths(brief: Brief, vault_root: Path) -> Brief:
    """Resolve every context wiki-link to a filesystem path. Raises
    BriefValidationError listing ALL unresolved links."""
    resolved: list[Path] = []
    unresolved: list[str] = []
    for link in brief.context_links:
        p = _resolve_one(link, vault_root)
        if p is None:
            unresolved.append(link)
        else:
            resolved.append(p)
    if unresolved:
        raise BriefValidationError(
            [f"unresolved context wiki-link: [[{u}]]" for u in unresolved]
        )
    return brief.model_copy(update={"context_paths": resolved})
