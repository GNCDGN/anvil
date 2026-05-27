"""v4 Phase 2c Step 1 — the orchestrator observe sub-phase (capture-only).

Hermetic: a /tmp git repo as target_repo_path, a /tmp inbox brief, ANVIL_STATE_DIR
at /tmp. The Phase 2a substrate is MOCKED at the orchestrator's import site
(`anvil.integrations.browser.BrowserSession`, `...visibility_session.write_session`)
— no live browser, no real disk writes. The Planner is a local FakePlanner
(no LLM). A synthetic brief_version: 2 brief with an observe: step runs through
handle_brief end-to-end via manual coder mode (a 'done' reply per step).

Step 1 is the MECHANICAL observe loop: launch → navigate → capture declared
surfaces → close → write_session(digest=None). NO Haiku digest, NO observe.captured
event (Step 2 adds both). Capture-only: the sub-phase never fails the step.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from anvil.config import Config
from anvil.orchestrator import Orchestrator
from anvil.planner import Plan
from anvil.state import read_state

ANVIL_REPO = Path(__file__).resolve().parent.parent


# --- minimal fakes (mirror test_orchestrator.py; re-defined for independence) ---

class FakeTelegram:
    def __init__(self, replies):
        self.sent: list[str] = []
        self._replies = deque(replies)
        self._mid = 0

    def send(self, text: str) -> int:
        self.sent.append(text)
        self._mid += 1
        return self._mid

    def wait_for_reply(self, timeout):
        if not self._replies:
            return None
        text = self._replies.popleft()

        class _R:
            pass
        r = _R()
        r.text = text
        r.message_id = 999
        r.timestamp = 0
        return r


class FakeGit:
    def __init__(self):
        self.calls = []

    def commit_step(self, repo_path, plan, step_idx, *, brief_name=None,
                    commit_message_hint=None, run_log_filename=None) -> str:
        self.calls.append({"step_idx": step_idx})
        return f"deadbeef{step_idx:02d}"

    def head_hash(self, repo_path) -> str:
        return "headhashx"


class FakePlanner:
    def plan_step(self, brief, state, step_idx: int):
        s = brief.steps[step_idx]
        return Plan(
            step_number=s.number,
            step_name=s.name,
            files_to_touch=list(s.scope_files),
            operations=list(s.scope_operations),
            approach=f"(fake) execute {s.name}",
            smoke_test=s.smoke,
            expected_outcome="ok",
            commit_message=s.commit_message_hint or f"Step {s.number}",
            scope_boundaries={"in_scope": "(none)", "out_of_scope": "x"},
            confidence="high",
            escalation_triggers=[],
        )


def _ok_session(*, dom="<html><body>x</body></html>",
                console=None, network=None,
                launch_ok=True, navigate_ok=True,
                dom_ok=True, console_ok=True, network_ok=True,
                close_raises=False):
    """Build a MagicMock BrowserSession instance with configurable results."""
    sess = mock.MagicMock(name="BrowserSession-instance")
    sess.launch.return_value = (
        {"ok": True, "result": {"headless": True}} if launch_ok
        else {"ok": False, "error": "browser not installed"})
    sess.navigate.return_value = (
        {"ok": True, "result": {"url": "u", "status": 200}} if navigate_ok
        else {"ok": False, "error": "timed out"})
    sess.snapshot_dom.return_value = (
        {"ok": True, "result": {"html": dom}} if dom_ok
        else {"ok": False, "error": "page closed"})
    sess.capture_console.return_value = (
        {"ok": True, "result": {"entries": console or []}} if console_ok
        else {"ok": False, "error": "console fail"})
    sess.capture_network.return_value = (
        {"ok": True, "result": {"entries": network or []}} if network_ok
        else {"ok": False, "error": "network fail"})
    if close_raises:
        sess.close.side_effect = RuntimeError("close boom")
    else:
        sess.close.return_value = {"ok": True, "result": {"closed": True}}
    return sess


class _ObserveBase(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_state = os.environ.get("ANVIL_STATE_DIR")
        self._tmp = Path(tempfile.mkdtemp(prefix="anvil-test-observe-loop-"))
        os.environ["ANVIL_STATE_DIR"] = str(self._tmp / "state")
        self.repo = self._tmp / "target-repo"
        self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        # a real file so the steps' scope.files exists (avoids the pre-existing
        # brief.py empty-field parse-warning noise; see the Step 1 surprise note —
        # not a Phase 2c concern, but a clean test brief is well-formed).
        (self.repo / "obs.txt").write_text("x", encoding="utf-8")
        self.inbox = self._tmp / "inbox"
        self.inbox.mkdir()
        self.cfg = Config(
            anthropic_api_key="x",
            telegram_bot_token="t",
            telegram_chat_id="123",
            vault_path=self._tmp / "no-vault",
            anvil_root=ANVIL_REPO,
            anvil_defer_window_seconds=300,
            planner_model="claude-opus-4-7",
            planner_timeout=120,
            coder_timeout=600,
        )
        # v4 Phase 2c Step 2: the observe sub-phase now routes a Haiku digest via
        # routing.call_model_for_subtask. Patch it for every observe test (default
        # a benign digest string so no real Haiku call); seam-specific tests
        # override self.mock_seam.return_value / .side_effect.
        self._seam_patcher = mock.patch(
            "anvil.orchestrator.routing.call_model_for_subtask",
            return_value="Digest: nothing notable.",
        )
        self.mock_seam = self._seam_patcher.start()
        self.addCleanup(self._seam_patcher.stop)

    def tearDown(self) -> None:
        if self._prev_state is None:
            os.environ.pop("ANVIL_STATE_DIR", None)
        else:
            os.environ["ANVIL_STATE_DIR"] = self._prev_state
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_brief(self, steps_md: str, *, version: int = 2,
                     name: str = "observe probe") -> Path:
        body = f"""---
brief_version: {version}
project: anvil
build_name: {name}
target_repo: github.com/test/test
target_repo_path: {self.repo}
vps_deploy: no
---

## Goal

Exercise the observe sub-phase.

## Steps

{steps_md}
"""
        p = self.inbox / "observe-brief.md"
        p.write_text(body, encoding="utf-8")
        return p

    def _observe_step(self, n: int, *, target: str = "https://example.com",
                      surfaces: str = "dom, console, network",
                      confirm: str = "auto") -> str:
        return (
            f"### Step {n} — Observe step {n}\n"
            f"- **scope.files:** obs.txt\n"
            f"- **scope.operations:** read\n"
            f"- **smoke:** `true`\n"
            f"- **confirm:** {confirm}\n"
            f"- **observe.target:** {target}\n"
            f"- **observe.surfaces:** {surfaces}\n"
        )

    def _plain_step(self, n: int, *, confirm: str = "auto") -> str:
        return (
            f"### Step {n} — Plain step {n}\n"
            f"- **scope.files:** obs.txt\n"
            f"- **scope.operations:** read\n"
            f"- **smoke:** `true`\n"
            f"- **confirm:** {confirm}\n"
        )

    def _orch(self, replies):
        self.tg = FakeTelegram(replies)
        self.git = FakeGit()
        return Orchestrator(
            self.cfg,
            coder_mode="manual",
            planner=FakePlanner(),
            telegram=self.tg,
            git=self.git,
            run_smoke=lambda cmd, cwd: (True, "pass"),
        )


class TestObserveSubPhaseDispatch(_ObserveBase):
    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_subphase_fires_on_observe_step(self, MockBS, mock_write):
        sess = _ok_session()
        MockBS.return_value = sess
        mock_write.return_value = {"ok": True, "result": {"path": "p", "blobs": {}}}
        orch = self._orch(["done"])
        rc = orch.handle_brief(self._write_brief(self._observe_step(1)))
        self.assertEqual(rc, 0)
        # the full lifecycle ran
        sess.launch.assert_called_once()
        sess.navigate.assert_called_once_with("https://example.com")
        sess.snapshot_dom.assert_called_once()
        sess.capture_console.assert_called_once()
        sess.capture_network.assert_called_once()
        sess.close.assert_called_once()
        # write_session called once, digest=None, the three surfaces populated
        self.assertEqual(mock_write.call_count, 1)
        args, kwargs = mock_write.call_args
        # signature: write_session(run_id, step_idx, target, observations, digest=None)
        self.assertEqual(args[1], 0)               # step_idx
        self.assertEqual(args[2], "https://example.com")  # target
        observations = args[3]
        self.assertEqual(observations["dom"], {"html": "<html><body>x</body></html>"})
        self.assertEqual(observations["console"], {"entries": []})
        self.assertEqual(observations["network"], {"entries": []})
        self.assertEqual(kwargs.get("digest"), "Digest: nothing notable.")  # Step 2: the Haiku digest

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_subphase_short_circuits_on_no_observe(self, MockBS, mock_write):
        orch = self._orch(["done"])
        rc = orch.handle_brief(self._write_brief(self._plain_step(1)))
        self.assertEqual(rc, 0)
        MockBS.assert_not_called()       # no BrowserSession instantiated
        mock_write.assert_not_called()   # no visibility_session write

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_subphase_v1_brief_no_observe_short_circuits(self, MockBS, mock_write):
        # a brief_version: 1 brief never carries observe → short-circuit
        orch = self._orch(["done"])
        rc = orch.handle_brief(
            self._write_brief(self._plain_step(1), version=1))
        self.assertEqual(rc, 0)
        MockBS.assert_not_called()
        mock_write.assert_not_called()

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_dispatches_only_declared_surfaces(self, MockBS, mock_write):
        sess = _ok_session()
        MockBS.return_value = sess
        mock_write.return_value = {"ok": True, "result": {"path": "p", "blobs": {}}}
        orch = self._orch(["done"])
        orch.handle_brief(self._write_brief(self._observe_step(1, surfaces="dom")))
        sess.snapshot_dom.assert_called_once()
        sess.capture_console.assert_not_called()
        sess.capture_network.assert_not_called()
        observations = mock_write.call_args[0][3]
        self.assertEqual(observations["dom"], {"html": "<html><body>x</body></html>"})
        self.assertIsNone(observations["console"])
        self.assertIsNone(observations["network"])

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_dedups_surfaces_order_preserving(self, MockBS, mock_write):
        sess = _ok_session()
        MockBS.return_value = sess
        mock_write.return_value = {"ok": True, "result": {"path": "p", "blobs": {}}}
        orch = self._orch(["done"])
        # duplicate dom + console, with dom repeated last (order-preserving dedup)
        orch.handle_brief(self._write_brief(
            self._observe_step(1, surfaces="dom, console, dom")))
        # each capture method called exactly once despite the duplicate
        sess.snapshot_dom.assert_called_once()
        sess.capture_console.assert_called_once()
        sess.capture_network.assert_not_called()


class TestObserveSubPhaseCaptureOnly(_ObserveBase):
    """Q-F7 failure-mode matrix: log + continue; the sub-phase never fails the step."""

    def _run_with_session(self, sess, *, write_ok=True):
        with mock.patch("anvil.integrations.browser.BrowserSession", return_value=sess), \
             mock.patch("anvil.integrations.visibility_session.write_session",
                        return_value=({"ok": True, "result": {"path": "p", "blobs": {}}}
                                      if write_ok else {"ok": False, "error": "disk full"})) as mw:
            orch = self._orch(["done"])
            rc = orch.handle_brief(self._write_brief(self._observe_step(1)))
            return rc, mw

    def test_launch_failure_continues(self):
        sess = _ok_session(launch_ok=False)
        rc, mw = self._run_with_session(sess)
        self.assertEqual(rc, 0)             # step still completes
        sess.navigate.assert_not_called()   # launch failed → no navigate
        sess.close.assert_called_once()     # still torn down
        mw.assert_called_once()             # write still attempted (all-None obs)
        self.assertTrue(all(v is None for v in mw.call_args[0][3].values()))

    def test_navigate_failure_continues(self):
        sess = _ok_session(navigate_ok=False)
        rc, mw = self._run_with_session(sess)
        self.assertEqual(rc, 0)
        sess.snapshot_dom.assert_not_called()  # navigate failed → no captures
        sess.close.assert_called_once()
        mw.assert_called_once()

    def test_capture_failure_continues(self):
        sess = _ok_session(dom_ok=False)       # dom capture fails; others ok
        rc, mw = self._run_with_session(sess)
        self.assertEqual(rc, 0)
        obs = mw.call_args[0][3]
        self.assertIsNone(obs["dom"])          # failed capture → None
        self.assertEqual(obs["console"], {"entries": []})  # others still captured
        self.assertEqual(obs["network"], {"entries": []})

    def test_write_session_failure_continues(self):
        sess = _ok_session()
        rc, mw = self._run_with_session(sess, write_ok=False)
        self.assertEqual(rc, 0)            # write failed but step proceeds
        mw.assert_called_once()

    def test_close_never_raises(self):
        sess = _ok_session(close_raises=True)  # close() raises
        rc, mw = self._run_with_session(sess)
        self.assertEqual(rc, 0)            # try/finally swallows → step proceeds
        mw.assert_called_once()            # write still happened after the finally


class TestObserveSubPhaseLifecycle(_ObserveBase):
    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_per_step_session_instantiation(self, MockBS, mock_write):
        # two observe steps → BrowserSession instantiated twice (per-step, not reused)
        MockBS.side_effect = [_ok_session(), _ok_session()]
        mock_write.return_value = {"ok": True, "result": {"path": "p", "blobs": {}}}
        orch = self._orch(["done", "done"])
        rc = orch.handle_brief(self._write_brief(
            self._observe_step(1) + "\n" + self._observe_step(2)))
        self.assertEqual(rc, 0)
        self.assertEqual(MockBS.call_count, 2)   # one per step, not reused
        self.assertEqual(mock_write.call_count, 2)
        self.assertEqual([c[0][1] for c in mock_write.call_args_list], [0, 1])  # step idxs


class TestObserveLoopEndToEnd(_ObserveBase):
    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_loop_synthetic_brief_full_flow(self, MockBS, mock_write):
        # full flow: smoke pass → observe fires → write(digest=None) → commit reached
        sess = _ok_session(
            console=[{"type": "error", "text": "boom"}],
            network=[{"url": "https://example.com/x", "status": 404}],
        )
        MockBS.return_value = sess
        mock_write.return_value = {"ok": True, "result": {"path": "/p/record.json", "blobs": {}}}
        orch = self._orch(["done"])
        rc = orch.handle_brief(self._write_brief(self._observe_step(1)))
        self.assertEqual(rc, 0)
        st = read_state()
        self.assertEqual(st.status, "done")
        self.assertEqual(st.steps[0].status, "done")
        self.assertTrue(st.steps[0].commit)          # commit reached after observe
        # the observation carried the real-shaped console/network through
        obs = mock_write.call_args[0][3]
        self.assertEqual(obs["console"], {"entries": [{"type": "error", "text": "boom"}]})
        self.assertEqual(obs["network"],
                         {"entries": [{"url": "https://example.com/x", "status": 404}]})
        # Step 2: the Haiku digest (the setUp seam mock) reached the single write
        self.assertEqual(mock_write.call_args[1].get("digest"), "Digest: nothing notable.")


class TestObserveSubPhaseHaikuDigest(_ObserveBase):
    """Step 2: the Haiku digest via call_model_for_subtask (the seam's first real
    consumer) — routing, sentinel handling (BAF-2), empty-string handling
    (Q-F4-F1), the cost-shaped summary (Q-F5), and digest-first-single-write
    (BAF-1). The seam is mocked in _ObserveBase.setUp (self.mock_seam)."""

    def _run_observe(self, MockBS, mock_write, surfaces="dom, console, network"):
        MockBS.return_value = _ok_session()
        mock_write.return_value = {"ok": True, "result": {"path": "/p/record.json", "blobs": {}}}
        orch = self._orch(["done"])
        return orch.handle_brief(self._write_brief(self._observe_step(1, surfaces=surfaces)))

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_digest_routes_to_haiku_on_success(self, MockBS, mock_write):
        self.mock_seam.return_value = "Digest summary: 2 console errors found."
        self.assertEqual(self._run_observe(MockBS, mock_write), 0)
        # routed to Haiku with the prompt + a summary
        self.mock_seam.assert_called_once()
        args, _ = self.mock_seam.call_args
        self.assertEqual(args[0], "haiku")
        from anvil.orchestrator import DIGEST_SYSTEM_PROMPT
        self.assertEqual(args[1], DIGEST_SYSTEM_PROMPT)
        # the digest reached the single write
        self.assertEqual(mock_write.call_args[1].get("digest"),
                         "Digest summary: 2 console errors found.")

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_digest_sentinel_handled_as_none(self, MockBS, mock_write):
        self.mock_seam.return_value = "[call_model_for_subtask error: rate limit exceeded]"
        self.assertEqual(self._run_observe(MockBS, mock_write), 0)  # capture-only: proceeds
        self.assertIsNone(mock_write.call_args[1].get("digest"))    # BAF-2 → None

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_digest_empty_string_handled_as_none(self, MockBS, mock_write):
        # Q-F4-F1: Haiku returns "" (no text blocks) → None, step proceeds
        self.mock_seam.return_value = ""
        self.assertEqual(self._run_observe(MockBS, mock_write), 0)
        self.assertIsNone(mock_write.call_args[1].get("digest"))

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_digest_whitespace_only_handled_as_none(self, MockBS, mock_write):
        # Q-F4-F1: whitespace-only is also "no usable digest" → None
        self.mock_seam.return_value = "   \n  \t "
        self.assertEqual(self._run_observe(MockBS, mock_write), 0)
        self.assertIsNone(mock_write.call_args[1].get("digest"))

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observation_summary_excludes_full_dom(self, MockBS, mock_write):
        # Q-F5 cost-shaping: a 50KB DOM blob → the summary carries the LENGTH,
        # not the raw HTML body.
        big_html = "<html><body>" + ("x" * 50_000) + "</body></html>"
        MockBS.return_value = _ok_session(dom=big_html)
        mock_write.return_value = {"ok": True, "result": {"path": "p", "blobs": {}}}
        orch = self._orch(["done"])
        orch.handle_brief(self._write_brief(self._observe_step(1, surfaces="dom")))
        summary = self.mock_seam.call_args[0][2]  # user_message
        self.assertIn(f"DOM: {len(big_html)} chars", summary)
        self.assertNotIn("xxxxxxxxxx", summary)   # the raw body is NOT in the summary

    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_digest_first_single_write(self, MockBS, mock_write):
        # BAF-1: the seam is called BEFORE write_session, and write_session ONCE.
        call_order = []
        self.mock_seam.side_effect = lambda *a, **k: (call_order.append("seam") or "D")
        mock_write.side_effect = lambda *a, **k: (call_order.append("write")
                                                  or {"ok": True, "result": {"path": "p", "blobs": {}}})
        MockBS.return_value = _ok_session()
        orch = self._orch(["done"])
        orch.handle_brief(self._write_brief(self._observe_step(1)))
        self.assertEqual(call_order, ["seam", "write"])   # digest-first
        self.assertEqual(mock_write.call_count, 1)        # single write
        self.assertEqual(mock_write.call_args[1].get("digest"), "D")


class TestObserveCapturedEvent(_ObserveBase):
    """Step 2: the observe.captured event (Q-F2/Q-F3) — emitted with derived
    counts + record_path + digest_chars + ok; NO blobs on the row."""

    def _run_capture_emit(self, MockBS, mock_write, mock_emit, *, console=None,
                          network=None, write_ok=True):
        MockBS.return_value = _ok_session(console=console, network=network)
        mock_write.return_value = ({"ok": True, "result": {"path": "/p/record.json", "blobs": {}}}
                                   if write_ok else {"ok": False, "error": "disk full"})
        orch = self._orch(["done"])
        rc = orch.handle_brief(self._write_brief(self._observe_step(1)))
        # find the observe.captured emit among all emit calls
        captured = [c for c in mock_emit.call_args_list if c[0][0] == "observe.captured"]
        return rc, captured

    @mock.patch("anvil.orchestrator._events.emit")
    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_captured_emits_on_success(self, MockBS, mock_write, mock_emit):
        rc, captured = self._run_capture_emit(MockBS, mock_write, mock_emit)
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        payload = captured[0][0][1]
        self.assertEqual(payload["step_idx"], 0)
        self.assertEqual(payload["target"], "https://example.com")
        self.assertEqual(payload["surfaces"], ["dom", "console", "network"])
        self.assertEqual(payload["record_path"], "/p/record.json")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["digest_chars"], len("Digest: nothing notable."))
        self.assertEqual(captured[0][1].get("step_idx"), 0)  # step_idx kwarg

    @mock.patch("anvil.orchestrator._events.emit")
    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_captured_failure_counts(self, MockBS, mock_write, mock_emit):
        _, captured = self._run_capture_emit(
            MockBS, mock_write, mock_emit,
            console=[{"type": "error", "text": "e1"}, {"type": "log", "text": "l"},
                     {"type": "error", "text": "e2"}],
            network=[{"url": "/a", "status": 200}, {"url": "/b", "status": 404},
                     {"url": "/c", "status": 500}],
        )
        payload = captured[0][0][1]
        self.assertEqual(payload["console_error_count"], 2)    # 2 errors of 3 entries
        self.assertEqual(payload["network_failure_count"], 2)  # 404 + 500

    @mock.patch("anvil.orchestrator._events.emit")
    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_captured_payload_excludes_blobs(self, MockBS, mock_write, mock_emit):
        big_html = "<html>" + ("y" * 30_000) + "</html>"
        MockBS.return_value = _ok_session(dom=big_html)
        mock_write.return_value = {"ok": True, "result": {"path": "p", "blobs": {}}}
        orch = self._orch(["done"])
        orch.handle_brief(self._write_brief(self._observe_step(1, surfaces="dom")))
        captured = [c for c in mock_emit.call_args_list if c[0][0] == "observe.captured"]
        payload = captured[0][0][1]
        # the payload carries only derived/scalar fields — no raw blob arrays
        self.assertEqual(set(payload.keys()),
                         {"step_idx", "target", "surfaces", "record_path",
                          "console_error_count", "network_failure_count",
                          "digest_chars", "ok"})
        self.assertNotIn("yyyyyyyyyy", repr(payload))  # the DOM body is not in the payload

    @mock.patch("anvil.orchestrator._events.emit")
    @mock.patch("anvil.integrations.visibility_session.write_session")
    @mock.patch("anvil.integrations.browser.BrowserSession")
    def test_observe_captured_ok_false_on_write_failure(self, MockBS, mock_write, mock_emit):
        rc, captured = self._run_capture_emit(MockBS, mock_write, mock_emit, write_ok=False)
        self.assertEqual(rc, 0)            # capture-only: step proceeds
        self.assertFalse(captured[0][0][1]["ok"])
        self.assertEqual(captured[0][0][1]["record_path"], "")  # write failed → no path


class TestStep2ValidKindsBump(unittest.TestCase):
    def test_valid_kinds_includes_observe_captured(self):
        from anvil.events import VALID_KINDS
        self.assertIn("observe.captured", VALID_KINDS)
        # 52 → 53 at v4 Phase 3a Step 3 (screen.captured); this guard re-anchors.
        self.assertEqual(len(VALID_KINDS), 53)

    def test_events_module_imports_with_53(self):
        # the events.py `assert len(VALID_KINDS) == 53` fires at import — a clean
        # import means the count-drift guard passed (52 → 53, Phase 3a Step 3).
        import anvil.events  # noqa: F401


if __name__ == "__main__":
    unittest.main()
