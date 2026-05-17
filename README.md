# ANVIL

Autonomous build orchestrator. ANVIL takes a structured design brief and runs an end-to-end build — planning, coding, testing, committing, deploying — escalating to Genco only when there's a real design decision or an out-of-scope action. It reduces the relay work between Genco and Claude on multi-step builds where most of the conversation is "paste this command, give me the output" rather than substantive design discussion.

The Coder surface is Claude Code; ANVIL adds planning, scoping, scheduling, escalation, and persistence on top of it. Bishop is the hands-on counterpart — ANVIL is the delegated mode.

## Status

**Phase 0 build in progress** — manual bootstrap (repo skeleton, config, brief schema, state management, Telegram client, stub Planner, git ops, orchestrator core in manual-Coder mode, CLI, end-to-end trivial run). Built in the Bishop workflow by Genco + Claude Code; ANVIL does not yet build itself. Phase 1 replaces the stub Planner with the real Opus-driven Planner.

Canonical design and technical spec live in the vault:

- `01-Projects/code-workspace/anvil/design.md` — the what and why
- `01-Projects/code-workspace/anvil/implementation-notes.md` — the how (code-level)
- `01-Projects/code-workspace/anvil/builds/2026-05-17-anvil-phase-0/brief.md` — the Phase 0 build brief
