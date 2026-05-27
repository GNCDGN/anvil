"""ANVIL always-on monitor (v5 Phase 1).

The VPS-resident `anvil-monitor.service` — a long-lived process that
receives operator-configured triggers (schedule, Sentry-poll) and routes
them, waking the Mac-side orchestrator for builds. It never runs Claude
Code, never writes the vault, never generates briefs (the trigger-dispatch
Boundary contract). Build execution stays Mac-bound; the monitor coordinates
from the VPS, sharing the box with Veronica as a separate systemd service.

Phase 1a ships the substrate + state only (this package's `anvil_ops`
operations ledger + the idle `main` entry point); the triggers that consume
them are Phase 1b/1c. Available-but-not-consumed.
"""
