"""Tests for anvil.copilot — v4 Phase 3b Step 2 (the per-session opt-in state).

Hermetic in-memory state; no I/O. Covers the DC8 opt-in semantics: default-off,
the two grant paths (CLI-at-start + the Telegram token mid-session), the guard,
expiry-at-session-end, no carry-over, and the reserved-token non-collision (Q-B6).
Available-but-not-consumed in Phase 3b — the substrate guard-check + the CLI/
Telegram grant wiring are Phase 3c.
"""
import unittest

import anvil.copilot as cp


class TestCopilotOptIn(unittest.TestCase):
    def test_start_session_defaults_off(self):
        s = cp.start_session("screen://Hetzner-console")
        self.assertFalse(s.autonomous_actuation_enabled)
        self.assertFalse(cp.is_autonomous_enabled(s))
        self.assertFalse(s.ended)
        self.assertTrue(s.session_id.startswith("cp-"))
        self.assertEqual(s.target, "screen://Hetzner-console")

    def test_start_session_with_cli_autonomous_flag(self):
        # the CLI `--autonomous` grant-at-start path (DC8)
        s = cp.start_session("screen://x", autonomous=True)
        self.assertTrue(cp.is_autonomous_enabled(s))

    def test_enable_autonomous_grants_mid_session(self):
        # the Telegram AUTONOMOUS_OPT_IN_TOKEN path (DC8)
        s = cp.start_session("screen://x")
        self.assertFalse(cp.is_autonomous_enabled(s))
        cp.enable_autonomous(s)
        self.assertTrue(cp.is_autonomous_enabled(s))

    def test_end_session_expires_the_opt_in(self):
        s = cp.start_session("screen://x", autonomous=True)
        self.assertTrue(cp.is_autonomous_enabled(s))
        cp.end_session(s)
        self.assertTrue(s.ended)
        self.assertFalse(cp.is_autonomous_enabled(s))  # expired

    def test_no_carryover_between_sessions(self):
        s1 = cp.start_session("screen://x")
        cp.enable_autonomous(s1)
        self.assertTrue(cp.is_autonomous_enabled(s1))
        s2 = cp.start_session("screen://y")  # a fresh session
        self.assertFalse(cp.is_autonomous_enabled(s2))  # no carry-over
        self.assertNotEqual(s1.session_id, s2.session_id)

    def test_enable_after_end_is_noop(self):
        s = cp.start_session("screen://x")
        cp.end_session(s)
        cp.enable_autonomous(s)  # cannot grant after the session ended
        self.assertFalse(cp.is_autonomous_enabled(s))

    def test_session_id_minted_or_supplied(self):
        # Phase 3c aligns the opt-in id with the visibility_session co-pilot id
        s = cp.start_session("screen://x", session_id="cp-shared-123")
        self.assertEqual(s.session_id, "cp-shared-123")

    def test_reserved_token_no_collision(self):
        # Q-B6: the opt-in token does not collide with the go/resume/abort reply
        # vocabulary (design Part 4)
        self.assertEqual(cp.AUTONOMOUS_OPT_IN_TOKEN, "autonomous: on")
        for reserved in ("go", "resume", "abort"):
            self.assertNotEqual(cp.AUTONOMOUS_OPT_IN_TOKEN, reserved)
            self.assertNotIn(reserved, cp.AUTONOMOUS_OPT_IN_TOKEN.split())


class TestTelegramGrant(unittest.TestCase):
    """v4 Phase 3c Step 2: the Telegram opt-in grant path (apply_telegram_grant)."""

    def test_reserved_token_grants(self):
        s = cp.start_session("screen://main")
        self.assertTrue(cp.apply_telegram_grant(s, "autonomous: on"))
        self.assertTrue(cp.is_autonomous_enabled(s))

    def test_token_case_and_whitespace_insensitive(self):
        s = cp.start_session("screen://main")
        self.assertTrue(cp.apply_telegram_grant(s, "  Autonomous: On  "))
        self.assertTrue(cp.is_autonomous_enabled(s))

    def test_other_reply_is_noop(self):
        s = cp.start_session("screen://main")
        self.assertFalse(cp.apply_telegram_grant(s, "go"))
        self.assertFalse(cp.is_autonomous_enabled(s))

    def test_none_reply_is_noop(self):
        s = cp.start_session("screen://main")
        self.assertFalse(cp.apply_telegram_grant(s, None))
        self.assertFalse(cp.is_autonomous_enabled(s))

    def test_grant_on_ended_session_does_not_enable(self):
        s = cp.start_session("screen://main")
        cp.end_session(s)
        # apply returns True (token matched) but enable_autonomous is a no-op on
        # an ended session, so the guard stays False.
        cp.apply_telegram_grant(s, "autonomous: on")
        self.assertFalse(cp.is_autonomous_enabled(s))


if __name__ == "__main__":
    unittest.main()
