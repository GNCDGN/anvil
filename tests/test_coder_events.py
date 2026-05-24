"""v2 Phase 1 Step 2 — Coder instrumentation tests.

Mock the Claude subprocess call only — git calls inside _git_files_touched
delegate to _real_run (captured at import time before any mock fires).
Hermetic git repos back the Layer 2 git-diff verification.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from anvil import coder
from anvil import events
from anvil.coder import Coder

_real_run = subprocess.run


def _init_repo(repo: Path, files: dict[str, str] | None = None) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _real_run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    _real_run(["git", "config", "user.email", "t@t"], cwd=repo, check=True,
              capture_output=True)
    _real_run(["git", "config", "user.name", "t"], cwd=repo, check=True,
              capture_output=True)
    for name, content in (files or {".keep": ""}).items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _real_run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _real_run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
              capture_output=True)


def _plan(**over):
    base = dict(
        step_number=1,
        step_name="t",
        files_to_touch=["a.py"],
        operations=["write"],
        approach="do",
        expected_outcome="ok",
        escalation_triggers=[],
    )
    base.update(over)
    return SimpleNamespace(**base, model_dump=lambda: dict(base))


def _brief(repo_path: Path):
    return SimpleNamespace(target_repo_path=repo_path)


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class _CoderEventsBase(unittest.TestCase):

    def setUp(self) -> None:
        events._run_id = None
        events._anchor_monotonic = None
        events._drop_count = 0
        events._logged_unknown_kinds = set()

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(
            os.environ, {"ANVIL_ROOT": str(self.tmp_path)}
        )
        self._env_patch.start()
        events.begin_run("coder-events-test")

        self.repo = self.tmp_path / "repo"
        _init_repo(self.repo, {"a.py": "x = 1\n"})
        self.coder = Coder(
            claude_binary=Path("/usr/bin/true"),
            timeout=30,
            system_prompt="(system)",
        )

    def tearDown(self) -> None:
        events.end_run()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _events(self) -> list[dict]:
        path = (
            self.tmp_path / "state" / "runs"
            / "coder-events-test" / "events.jsonl"
        )
        if not path.is_file():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]


class TestCoderEvents(_CoderEventsBase):

    def test_clean_path_emits_full_sequence(self) -> None:
        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            # claude --print invocation: edit a.py so git diff sees it.
            (self.repo / "a.py").write_text("x = 2\n", encoding="utf-8")
            return _proc(0, "done", "")

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            out = self.coder.execute_step(_plan(), _brief(self.repo))

        self.assertEqual(out["exit_code"], 0)
        kinds = [e["kind"] for e in self._events()]
        coder_kinds = [k for k in kinds if k.startswith("coder.")]
        self.assertEqual(coder_kinds, [
            "coder.preflight.start",
            "coder.preflight.reconciled",
            "coder.subprocess.start",
            "coder.subprocess.end",
            "coder.scope_verify",
        ])

    def test_preflight_escalation_skips_subprocess(self) -> None:
        # v2 Phase 2 Step 4: operations=["read"] (not the default
        # ["write"]) so the unresolved path still escalates. With "write"
        # the V2P2-4 carve-out treats it as a 'new-file' and the
        # subprocess runs — the new-file path is covered in test_coder's
        # ReconcileWriteNewTests.
        plan = _plan(files_to_touch=["does/not/exist/nowhere.py"],
                     operations=["read"])
        with mock.patch.object(coder.subprocess, "run") as run_mock:
            out = self.coder.execute_step(plan, _brief(self.repo))
        self.assertTrue(out.get("escalate"))
        run_mock.assert_not_called()
        kinds = [e["kind"] for e in self._events()]
        coder_kinds = [k for k in kinds if k.startswith("coder.")]
        self.assertEqual(coder_kinds, [
            "coder.preflight.start",
            "coder.preflight.reconciled",
            "coder.preflight.escalate",
        ])

    def test_subprocess_end_carries_exit_duration_chars(self) -> None:
        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            (self.repo / "a.py").write_text("touched\n", encoding="utf-8")
            return _proc(0, "hello world", "some err")

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            self.coder.execute_step(_plan(), _brief(self.repo))
        sub_end = next(
            e for e in self._events() if e["kind"] == "coder.subprocess.end"
        )
        self.assertEqual(sub_end["data"]["exit_code"], 0)
        self.assertEqual(sub_end["data"]["stdout_chars"], len("hello world"))
        self.assertEqual(sub_end["data"]["stderr_chars"], len("some err"))
        self.assertGreaterEqual(sub_end["data"]["duration_ms"], 0)

    def test_subprocess_end_on_timeout_exit_negative_one(self) -> None:
        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            raise subprocess.TimeoutExpired(
                cmd=["true"], timeout=1, output="", stderr=""
            )

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            self.coder.execute_step(_plan(), _brief(self.repo))
        sub_end = next(
            e for e in self._events() if e["kind"] == "coder.subprocess.end"
        )
        self.assertEqual(sub_end["data"]["exit_code"], -1)

    def test_scope_verify_reports_out_of_scope(self) -> None:
        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            # Plan declares a.py only; the subprocess "creates" b.py too.
            (self.repo / "a.py").write_text("touched\n", encoding="utf-8")
            (self.repo / "b.py").write_text("oops\n", encoding="utf-8")
            return _proc(0, "", "")

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            self.coder.execute_step(_plan(files_to_touch=["a.py"]),
                                    _brief(self.repo))
        scope = next(
            e for e in self._events() if e["kind"] == "coder.scope_verify"
        )
        self.assertIn("b.py", scope["data"]["out_of_scope"])
        self.assertEqual(scope["data"]["out_of_scope_count"], 1)

    def test_prompt_chars_on_subprocess_start_matches_real_prompt(self) -> None:
        captured = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            captured["text"] = kw.get("input", "")
            (self.repo / "a.py").write_text("touched\n", encoding="utf-8")
            return _proc(0, "", "")

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            self.coder.execute_step(_plan(), _brief(self.repo))

        sub_start = next(
            e for e in self._events() if e["kind"] == "coder.subprocess.start"
        )
        self.assertEqual(sub_start["data"]["prompt_chars"],
                         len(captured["text"]))


class TestCoderRoutingObservability(_CoderEventsBase):
    """v3 Phase 0 Step 1 (V3P0-1) + Phase 2b Step 1 (fix): coder.subprocess.end
    carries the five routing fields. route_actual is the model the CLI ran,
    derived from the envelope's modelUsage key; "no-envelope" when there's no
    JSON envelope (every mock-mode row, by construction — Finding M); "unknown"
    only when an envelope parsed but the model wasn't derivable (diagnostic)."""

    def test_non_json_stdout_route_actual_no_envelope(self) -> None:
        # Non-JSON stdout (the MockedCoder shape) → env is None → "no-envelope"
        # (structural: no JSON envelope to derive a model from — Finding M),
        # distinct from "unknown" (envelope present, model not derivable).
        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            (self.repo / "a.py").write_text("x = 2\n", encoding="utf-8")
            return _proc(0, "[anvil-coder] mocked execution\n", "")

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            self.coder.execute_step(
                _plan(files_to_touch=["a.py"]), _brief(self.repo)
            )
        end = next(e for e in self._events()
                   if e["kind"] == "coder.subprocess.end")
        d = end["data"]
        self.assertEqual(d["route_actual"], "no-envelope")
        self.assertEqual(d["route_candidate"], "no-envelope")
        self.assertFalse(d["route_fallback_fired"])
        self.assertEqual(d["policy_version"], "v3-phase-0-passive")
        fs = d["features_seen"]
        self.assertEqual(fs["stage"], "coder")
        # context_paths_count = len(plan.files_to_touch).
        self.assertEqual(fs["context_paths_count"], 1)
        # No usage envelope → token sum is 0 (not None — the Coder sums
        # three usage lines, each coalesced to 0 when absent).
        self.assertEqual(fs["observed_prompt_token_count"], 0)

    def test_json_envelope_route_actual_is_model(self) -> None:
        # A real-shaped JSON envelope carries model + usage → route_actual
        # is that model, observed token count is the three-line sum.
        # v3 Phase 2b Step 1: the real envelope has NO top-level `model` key —
        # the model is the KEY of `modelUsage` (the V3P0-1 root cause).
        # route_actual now derives from there (max-costUSD key).
        envelope = json.dumps({
            "result": "done",
            "modelUsage": {
                "claude-sonnet-4-6": {
                    "costUSD": 0.012, "inputTokens": 100, "outputTokens": 20,
                },
            },
            "total_cost_usd": 0.012,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 5,
            },
        })

        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            (self.repo / "a.py").write_text("x = 2\n", encoding="utf-8")
            return _proc(0, envelope, "")

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            self.coder.execute_step(
                _plan(files_to_touch=["a.py"]), _brief(self.repo)
            )
        end = next(e for e in self._events()
                   if e["kind"] == "coder.subprocess.end")
        d = end["data"]
        self.assertEqual(d["route_actual"], "claude-sonnet-4-6")
        self.assertEqual(d["route_candidate"], "claude-sonnet-4-6")
        self.assertEqual(d["policy_version"], "v3-phase-0-passive")
        # observed_prompt_token_count = 100 + 30 + 5 = 135.
        self.assertEqual(d["features_seen"]["observed_prompt_token_count"], 135)
        self.assertEqual(d["features_seen"]["stage"], "coder")

    def test_json_envelope_no_modelusage_route_actual_unknown(self) -> None:
        # Envelope parses (has "result") but has no derivable modelUsage →
        # route_actual="unknown" (the DIAGNOSTIC branch: envelope present, model
        # not derivable — a real-mode signal post-Phase-2b, distinct from the
        # structural "no-envelope"). Finding M three-way split.
        envelope = json.dumps({
            "result": "done", "modelUsage": {},
            "total_cost_usd": 0.0, "usage": {},
        })

        def fake_run(cmd, **kw):
            if cmd[:2] == ["git", "-C"]:
                return _real_run(cmd, **kw)
            (self.repo / "a.py").write_text("x = 2\n", encoding="utf-8")
            return _proc(0, envelope, "")

        with mock.patch.object(coder.subprocess, "run", side_effect=fake_run):
            self.coder.execute_step(
                _plan(files_to_touch=["a.py"]), _brief(self.repo)
            )
        d = next(e for e in self._events()
                 if e["kind"] == "coder.subprocess.end")["data"]
        self.assertEqual(d["route_actual"], "unknown")
        self.assertEqual(d["route_candidate"], "unknown")


if __name__ == "__main__":
    unittest.main()
