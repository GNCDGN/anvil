"""External-surface connector wrappers (v4 Phase 1b/1c).

Each module here wraps one external surface (GitHub Issues via the `gh` CLI in
Phase 1b; Sentry via REST in Phase 1b; Vercel/Netlify in Phase 1c) behind the
same never-raises contract the Coder subprocess wrapper and
`routing.call_model_for_subtask` use: a structured ``{"ok": True, "result": …}``
on success, a structured ``{"ok": False, "error": …}`` on any failure — no
exception escapes. The wrappers are available-but-not-consumed in Phase 1b
(Q-B5): callable, unit-tested, scope-enforcing; the build-loop invocation
wiring is a later concern (v5 escalation→issue, per v4 design §9).
"""
