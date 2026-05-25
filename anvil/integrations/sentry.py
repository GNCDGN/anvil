"""Sentry connector — v4 Phase 1b Step 2 (brief Step 2; Q-B2/Q-B3/Q-B5/Q-B6).

A read-only wrapper around the Sentry REST API. The REST instance of the
connector-wrapper contract Step 1 crystallized on the subprocess case
(github_issues.py): every public function **never raises** — a structured
``{"ok": True, "result": …}`` on success, ``{"ok": False, "error": "<reason>"}``
on any failure (HTTP error, malformed JSON, missing token, scope violation).
The caller inspects ``["ok"]``; no try/except needed.

HTTP client (Q-B6): stdlib ``urllib.request`` — anvil takes no third-party HTTP
dependency (`requests` is not in requirements.txt), so the connector adds none.
Tests REST-mock ``urllib.request.urlopen``; no live Sentry call, no network, no
token required to run the suite.

Auth (Q-B2): ``SENTRY_API_TOKEN`` is read from the environment **lazily, at call
time** (the orchestrator loads `.env`; mirrors routing.py's
``os.environ.get("ANTHROPIC_API_KEY")``), sent as ``Authorization: Bearer
<token>``. A missing token returns a structured error — never raises at import
or call. No token is needed to import this module or to run the mocked tests.

Endpoints (Q-B2): base ``https://sentry.io/api/0``; ``list_issues`` →
``/projects/<org>/<project>/issues/`` (an org/project slug pair — `project` is
the function arg, `org` is the ``SENTRY_ORG`` env var or the module default),
``get_issue`` → ``/issues/<id>/``, ``list_events`` → ``/issues/<id>/events/``.

Scope (Q-B3): ``sentry: read`` is the only valid scope (rule 15 rejects anything
else at the brief). There are **no write methods** — the absence of write
methods IS the write-scope enforcement. The step's declared `sentry:` scope is
passed per call as keyword-only ``scope`` and checked HERE before any HTTP
request. Orthogonal to `issues:` (github_issues) and `model:` (Planner routing).

No new event kinds (Q-B4): logs via the `anvil.integrations` logger, emits no
operations-table event; `VALID_KINDS` unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("anvil.integrations.sentry")

# REST base + the org slug for project-scoped endpoints. `SENTRY_ORG` overrides
# the default for a different org; mock-only in Phase 1b so the default is only
# exercised against the mocked client (Q-B2: org/project slugs settle against
# the live org if a live wiring ever lands).
_SENTRY_BASE = "https://sentry.io/api/0"
_DEFAULT_ORG = "anvil"
_SENTRY_TIMEOUT = 30  # wall-clock cap per request, matching the gh wrapper

# v4 Phase 1b Step 2 (Q-B3): the only valid `sentry:` scope (mirrors
# brief.SENTRY_SCOPES — kept local so the connector has no import dependency on
# brief.py).
_VALID_SCOPE = "read"


def _ok(result) -> dict:
    return {"ok": True, "result": result}


def _err(reason: str) -> dict:
    return {"ok": False, "error": reason}


def _org() -> str:
    return os.environ.get("SENTRY_ORG", _DEFAULT_ORG)


def _enforce_scope(operation: str, scope: str | None) -> dict | None:
    """Return a structured out-of-scope error, or None if in scope. Sentry is
    read-only: `read` is the only valid scope. Anything else (None, or a value
    rule 15 would have rejected at the brief) is refused before any HTTP call.
    `operation` is accepted for symmetry with github_issues._enforce_scope and
    future message specificity; the read-only connector has one refusal."""
    if scope != _VALID_SCOPE:
        return _err("out-of-scope: sentry scope not declared on this step")
    return None


def _get(path: str, *, params: dict | None = None) -> dict:
    """Authenticated GET against the Sentry REST API (never-raises).

    Returns ``{"ok": True, "result": <parsed JSON>}`` on a 2xx with a parseable
    body, else a structured error. The token is read lazily here so importing
    the module and running the mocked suite need no token. The except ladder
    mirrors github_issues._run_gh: the HTTP error (4xx/5xx), the transport error
    (URLError/ConnectionError/TimeoutError), and a final broad catch for the
    never-raise contract.
    """
    token = os.environ.get("SENTRY_API_TOKEN")
    if not token:
        return _err("sentry: SENTRY_API_TOKEN not set")

    url = f"{_SENTRY_BASE}/{path.lstrip('/')}"
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        if query:
            url = f"{url}?{query}"

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=_SENTRY_TIMEOUT) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 — body is best-effort context only
            pass
        return _err(f"sentry: HTTP {exc.code}" + (f": {body}" if body else ""))
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return _err(f"sentry: request failed: {reason}")
    except Exception as exc:  # noqa: BLE001 — never-raise contract
        log.error("[sentry] unexpected error GET %s: %s", url, exc)
        return _err(f"sentry: unexpected error: {type(exc).__name__}")

    # Defensive: urlopen returns only on 2xx (raises HTTPError otherwise), but
    # guard the contract explicitly in case a future client returns non-2xx.
    if not (200 <= int(status) < 300):
        return _err(f"sentry: HTTP {status}")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        return _ok(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        snippet = (raw or "").strip()[:200]
        return _err(f"sentry returned non-JSON: {snippet!r}")


def list_issues(project: str, *, since=None, scope=None) -> dict:
    """List a project's issues via ``GET /projects/<org>/<project>/issues/``
    (read scope). `project` is the slug; `org` is the ``SENTRY_ORG`` env var or
    the module default. Optional `since` (ISO timestamp) passes as a query
    param. Refused (no HTTP) when `scope` is not `read`."""
    blocked = _enforce_scope("list_issues", scope)
    if blocked:
        return blocked
    path = f"/projects/{_org()}/{project}/issues/"
    return _get(path, params={"since": since} if since else None)


def get_issue(issue_id, *, scope=None) -> dict:
    """Fetch one issue via ``GET /issues/<id>/`` (read scope). Refused (no HTTP)
    when `scope` is not `read`."""
    blocked = _enforce_scope("get_issue", scope)
    if blocked:
        return blocked
    return _get(f"/issues/{issue_id}/")


def list_events(issue_id, *, scope=None) -> dict:
    """List an issue's events via ``GET /issues/<id>/events/`` (read scope).
    Refused (no HTTP) when `scope` is not `read`."""
    blocked = _enforce_scope("list_events", scope)
    if blocked:
        return blocked
    return _get(f"/issues/{issue_id}/events/")
