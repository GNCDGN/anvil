"""Phase 2 Step 3 — decision #19 regression tests.

Covers the five cases the brief specified:
  (a) _escalate(options=("go", "abort")) + reply "go" → proceeds
  (b) same + reply "abort" → aborts
  (c) same + reply "fix and re-run" → falls through to paused-by-user
  (d) _escalate(options=("abort",)) + reply "go" → falls through
      (proceed is not a valid option for this escalation)
  (e) _escalate with Planner-self-emitted prose options → message contains
      both prose and grammar lines; reply "go" proceeds

Plus one safety net:
  (f) legacy string options still works (back-compat, emits a warning)
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anvil.brief import Brief, Step
from anvil.orchestrator import Orchestrator
from anvil.state import init_state, write_state


def _trivial_brief() -> Brief:
    return Brief(
        brief_version=1, project="anvil", build_name="grammar-test",
        target_repo="x", target_repo_path=Path("/tmp"), vps_deploy="no",
        steps=[Step(
            number=1, name="Example", scope_files=["a.py"],
            scope_operations=["write", "commit"], smoke="echo x",
            confirm="explicit",
        )],
    )


def _trivial_state(brief):
    state = init_state(brief, "2026-05-18T00:00:00",
                       brief_path="/tmp/grammar-test.md")
    write_state(state)
    return state


class _SingleReplyTG:
    def __init__(self, reply_text):
        self._reply_text = reply_text
        self.sent = []

    def send(self, text):
        self.sent.append(text)

    def wait_for_reply(self, timeout=None):
        return SimpleNamespace(text=self._reply_text)


class EscalationGrammarTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ANVIL_STATE_DIR")
        self._dir = Path(tempfile.mkdtemp(prefix="anvil-test-d19-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._dir)
        self.brief = _trivial_brief()
        self.state = _trivial_state(self.brief)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev
        shutil.rmtree(self._dir, ignore_errors=True)

    def _orch(self, tg):
        return Orchestrator(
            SimpleNamespace(
                anthropic_api_key="x", planner_model="x",
                planner_timeout=60, vault_path=Path("/tmp/no-vault"),
            ),
            planner=mock.Mock(),
            telegram=tg,
            git=mock.Mock(),
            run_smoke=mock.Mock(),
        )

    # --- (a) go-abort grammar, reply "go" → proceeds ---
    def test_a_go_abort_with_go_proceeds(self):
        tg = _SingleReplyTG("go")
        orch = self._orch(tg)
        orch._escalate(self.state, "test", "detail", ("go", "abort"))
        result = orch._await_user_decision(self.state)
        self.assertTrue(result)

    # --- (b) go-abort grammar, reply "abort" → aborts ---
    def test_b_go_abort_with_abort_aborts(self):
        tg = _SingleReplyTG("abort")
        orch = self._orch(tg)
        orch._escalate(self.state, "test", "detail", ("go", "abort"))
        result = orch._await_user_decision(self.state)
        self.assertFalse(result)

    # --- (c) prose reply falls through to paused-by-user ---
    def test_c_prose_reply_falls_through(self):
        tg = _SingleReplyTG("fix and re-run")
        orch = self._orch(tg)
        orch._escalate(self.state, "test", "detail", ("go", "abort"))
        result = orch._await_user_decision(self.state)
        self.assertFalse(result)

    # --- (d) abort-only grammar, reply "go" → does NOT proceed ---
    def test_d_abort_only_rejects_go(self):
        tg = _SingleReplyTG("go")
        orch = self._orch(tg)
        orch._escalate(
            self.state, "planner-validation-failure",
            "validated twice", ("abort",),
        )
        result = orch._await_user_decision(self.state)
        # "go" is not in ("abort",), and is not the literal "abort", so
        # this is paused-by-user.
        self.assertFalse(result)

    # --- (e) Planner-self-emitted prose options ---
    def test_e_planner_prose_options_render_both_layers(self):
        tg = _SingleReplyTG("go")
        orch = self._orch(tg)
        orch._escalate(
            self.state,
            "judgment-call",
            "the Planner faced a real design question",
            ["amend brief to widen scope", "split step into two"],
        )
        result = orch._await_user_decision(self.state)
        self.assertTrue(result)
        # The Telegram message must include both the prose options as
        # numbered list and a grammar line saying "Reply: go / abort".
        sent = "\n".join(tg.sent)
        self.assertIn("1. amend brief to widen scope", sent)
        self.assertIn("2. split step into two", sent)
        self.assertIn("go / abort", sent)

    # --- (f) legacy string options still works (back-compat) ---
    def test_f_legacy_string_options_warns_and_works(self):
        tg = _SingleReplyTG("go")
        orch = self._orch(tg)
        with self.assertLogs("anvil", level="WARNING") as captured:
            orch._escalate(
                self.state, "smoke test failed", "out",
                "fix and re-run / abort",
            )
            result = orch._await_user_decision(self.state)
        self.assertTrue(result)
        # Some logger in the anvil namespace warned about the legacy form.
        self.assertTrue(
            any("legacy string options" in m for m in captured.output),
            f"expected a 'legacy string options' warning; got {captured.output}",
        )


if __name__ == "__main__":
    unittest.main()
