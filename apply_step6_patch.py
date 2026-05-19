#!/usr/bin/env python3
"""Phase 3 Step 6 patch — exam harness Phase 3 dimensions + completion message deploy block.

Applies:
  1. tools/exam_harness.py:
     - PHASE3_DIMENSIONS const (additive, alongside PHASE2_DIMENSIONS)
     - _ESCALATION_BINS extended with Phase 3 deploy reasons (framework bin)
     - Capture gains deploy_outcomes, e2e_outcomes, vps_head_shas fields
     - Capture.poll() reads state.deploy when present
     - render() emits a Deploy verification section
  2. anvil/voice.py format_completion: appends a Deploy block when state.deploy
     is populated.
  3. tools/fixtures/probe-deploy-state.json: synthetic state with state.deploy
     populated, for --self-check.
  4. tests/test_voice_completion.py: new file, tests completion message with
     and without deploy block.

Idempotent.

Run from ~/Downloads/anvil:
    .venv/bin/python apply_step6_patch.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
HARNESS_PY = REPO / "tools" / "exam_harness.py"
VOICE_PY = REPO / "anvil" / "voice.py"
FIXTURE = REPO / "tools" / "fixtures" / "probe-deploy-state.json"
TEST_FILE = REPO / "tests" / "test_voice_completion.py"


def fail(msg: str) -> None:
    print(f"[step6-patch] FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[step6-patch] {msg}")


def _backup(p: Path) -> None:
    bak = p.with_suffix(p.suffix + ".pre-phase-3-step-6.bak")
    if not bak.exists():
        shutil.copy2(p, bak)
        info(f"backed up {p.name} -> {bak.name}")


def _apply_unique(text: str, old: str, new: str, label: str) -> str:
    occurrences = text.count(old)
    if occurrences == 0:
        fail(f"{label}: anchor not found")
    if occurrences > 1:
        fail(f"{label}: anchor found {occurrences} times, expected 1")
    return text.replace(old, new, 1)


def patch_harness() -> bool:
    text = HARNESS_PY.read_text()
    if "PHASE3_DIMENSIONS" in text:
        info("exam_harness.py already has PHASE3_DIMENSIONS — skipping")
        return False

    _backup(HARNESS_PY)

    # Edit 1: add PHASE3_DIMENSIONS after PHASE2_DIMENSIONS
    old_dims_end = '''    ("Phase-1-retroactive",
     "Planner escalation calibration on Step 3 (judgment call); Step 4 conditional-skip discipline"),
]

# Escalation-source bins for Phase 2 scoring. Matched against the'''
    new_dims_end = '''    ("Phase-1-retroactive",
     "Planner escalation calibration on Step 3 (judgment call); Step 4 conditional-skip discipline"),
]

PHASE3_DIMENSIONS = [
    ("Deploy chain correctness",
     "state.deploy.stage == 'complete'; vps_head_sha matches; service_status == 'active'"),
    ("Sub-stage escalation routing",
     "any deploy-{stage}-failed escalation names the right stage; unit-evidenced if no live failure"),
    ("E2E ordering",
     "Mac-resident e2e gates deploy; VPS-resident e2e runs post-deploy per design 2.7"),
    ("Service health verification",
     "post-restart systemctl is-active returns active within 3s settle; negative case unit-evidenced"),
    ("Phase-2-retroactive (P2-10 closure)",
     "Planner Step 1 threshold judgment: escalate or grounded plan (pass); confabulate (fail). "
     "Step 2 conditional: decline-when-unmet or grounded-when-met (pass); invent (fail)."),
    ("Total Genco reply count",
     "under 20% of Phase 1 baseline; same metric as Phase 2"),
]

# Escalation-source bins for Phase 2 scoring. Matched against the'''
    text = _apply_unique(text, old_dims_end, new_dims_end, "edit 1: PHASE3_DIMENSIONS")

    # Edit 2: extend framework bin with Phase 3 deploy reasons
    old_framework = '''    "framework": (
        "planner-validation-failure", "smoke test failed",
        "coder-out-of-scope", "coder-path-reconciliation-failed",
        "coder-failed",
    ),'''
    new_framework = '''    "framework": (
        "planner-validation-failure", "smoke test failed",
        "coder-out-of-scope", "coder-path-reconciliation-failed",
        "coder-failed",
        # Phase 3 Step 6: deploy and e2e escalation reasons
        "deploy-config-missing", "deploy-push-failed", "deploy-pull-failed",
        "deploy-restart-failed", "deploy-health-check-failed",
        "deploy-e2e-failed", "e2e-failed", "e2e-script-not-found",
    ),'''
    text = _apply_unique(text, old_framework, new_framework, "edit 2: framework bin extension")

    # Edit 3: Capture gains deploy fields
    old_capture_init = '''        self.escalation_bin_counts: dict[str, int] = {
            "planner-self": 0, "framework": 0,
            "genco-initiated": 0, "other": 0,
        }

    def poll(self, state: dict):'''
    new_capture_init = '''        self.escalation_bin_counts: dict[str, int] = {
            "planner-self": 0, "framework": 0,
            "genco-initiated": 0, "other": 0,
        }
        # Phase 3 Step 6: deploy-stage outcomes and post-deploy e2e
        self.deploy_outcomes: list[dict] = []
        self.e2e_outcomes: list[dict] = []
        self.vps_head_shas: list[str] = []

    def poll(self, state: dict):'''
    text = _apply_unique(text, old_capture_init, new_capture_init, "edit 3: Capture fields")

    # Edit 4: Capture.poll() reads state.deploy. Inject after the prev = self.prev or {} line.
    old_poll_start = '''    def poll(self, state: dict):
        self.last_state = state
        prev = self.prev or {}
        if state.get("status") != prev.get("status"):'''
    new_poll_start = '''    def poll(self, state: dict):
        self.last_state = state
        prev = self.prev or {}
        # Phase 3 Step 6: capture deploy outcome when first observed.
        deploy = state.get("deploy")
        prev_deploy = prev.get("deploy")
        if deploy and deploy != prev_deploy:
            self.deploy_outcomes.append({
                "captured_at": _now_iso(),
                "stage": deploy.get("stage"),
                "ok": deploy.get("ok"),
                "vps_head_sha": deploy.get("vps_head_sha"),
                "service_status": deploy.get("service_status"),
                "output_truncated": (deploy.get("output", "")[:300] if deploy.get("output") else ""),
            })
            if deploy.get("vps_head_sha"):
                self.vps_head_shas.append(deploy["vps_head_sha"])
        if state.get("status") != prev.get("status"):'''
    text = _apply_unique(text, old_poll_start, new_poll_start, "edit 4: poll captures deploy")

    # Edit 5: render() emits Deploy verification section. Find the dimensions loop
    # and inject before it.
    old_render_loop = '''    for i, (name, feeds) in enumerate(PHASE2_DIMENSIONS, 1):'''
    new_render_loop = '''    # Phase 3 Step 6: Deploy verification section (only when deploy ran)
    if cap.deploy_outcomes:
        lines.append("## Deploy verification (Phase 3)")
        for d in cap.deploy_outcomes:
            lines.append(
                f"- {d['captured_at']}: stage={d['stage']} ok={d['ok']} "
                f"sha={d.get('vps_head_sha') or '-'} status={d.get('service_status') or '-'}"
            )
            if d.get("output_truncated"):
                lines.append(f"  output: {d['output_truncated']!r}")
        lines.append("")

    for i, (name, feeds) in enumerate(PHASE2_DIMENSIONS, 1):'''
    text = _apply_unique(text, old_render_loop, new_render_loop, "edit 5: render deploy section")

    HARNESS_PY.write_text(text)
    info("patched exam_harness.py")
    return True


def patch_voice() -> bool:
    text = VOICE_PY.read_text()
    if "Deploy:" in text and "vps_head_sha" in text:
        info("voice.py already has deploy block — skipping")
        return False

    _backup(VOICE_PY)

    old_completion = '''def format_completion(brief, state) -> str:
    done = sum(1 for s in state.steps if s.status == "done")
    return (
        f"[ANVIL] Build complete — {brief.build_name}\\n"
        f"- Steps: {done}/{len(state.steps)} done\\n"
        f"- Status: {state.status}\\n"
        f"- Run log: {Path(state.run_log).name if state.run_log else '(none)'}"
    )'''

    new_completion = '''def format_completion(brief, state) -> str:
    done = sum(1 for s in state.steps if s.status == "done")
    msg = (
        f"[ANVIL] Build complete — {brief.build_name}\\n"
        f"- Steps: {done}/{len(state.steps)} done\\n"
        f"- Status: {state.status}\\n"
        f"- Run log: {Path(state.run_log).name if state.run_log else '(none)'}"
    )
    # Phase 3 Step 6: deploy verification block when state.deploy populated
    deploy = getattr(state, "deploy", None)
    if deploy:
        sha = deploy.get("vps_head_sha") or ""
        sha_short = sha[:7] if sha else "-"
        status = deploy.get("service_status") or "-"
        stage = deploy.get("stage", "?")
        ok = deploy.get("ok", False)
        msg += (
            f"\\n\\nDeploy:\\n"
            f"- Stage: {stage} ({'ok' if ok else 'failed'})\\n"
            f"- VPS HEAD: {sha_short}\\n"
            f"- Service: {brief.service_name or '-'} ({status})"
        )
    return msg'''

    text = _apply_unique(text, old_completion, new_completion, "voice.format_completion deploy block")
    VOICE_PY.write_text(text)
    info("patched voice.py (added deploy block to format_completion)")
    return True


def create_fixture() -> bool:
    if FIXTURE.exists():
        info("probe-deploy-state.json already exists — skipping")
        return False

    fixture_data = {
        "schema_version": 2,
        "brief_path": "/tmp/test/brief.md",
        "started_at": "2026-05-19T10:00:00+01:00",
        "finished_at": "2026-05-19T10:15:00+01:00",
        "status": "done",
        "current_step": 1,
        "steps": [{
            "n": 1, "name": "trivial", "status": "done",
            "commit": "abc1234", "smoke": "pass",
        }],
        "deploy": {
            "stage": "complete",
            "ok": True,
            "output": "",
            "vps_head_sha": "fed987654321abc",
            "service_status": "active",
        },
        "coder_mode": "auto",
        "run_log": "/tmp/test/run.log",
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(fixture_data, indent=2))
    info("created tools/fixtures/probe-deploy-state.json")
    return True


def create_voice_test() -> bool:
    if TEST_FILE.exists():
        info("tests/test_voice_completion.py already exists — skipping")
        return False

    content = '''"""Phase 3 Step 6 tests — completion message deploy block."""
from __future__ import annotations

import unittest
from pathlib import Path

from anvil.brief import Brief, Step
from anvil.state import State, StepState
from anvil import voice


def _make_brief(service_name=None) -> Brief:
    return Brief(
        brief_version=1, project="test", build_name="test-build",
        target_repo="github.com/test/test", target_repo_path=Path("/tmp/test"),
        vps_deploy="yes" if service_name else "no",
        service_name=service_name,
        vps_target_path="/home/test/test" if service_name else None,
        steps=[Step(
            number=1, name="trivial", scope_files=[],
            scope_operations=["read"], smoke="true", confirm="auto",
        )],
    )


def _make_state(deploy=None) -> State:
    return State(
        brief_path="/tmp/test/brief.md",
        started_at="2026-05-19T10:00:00+01:00",
        finished_at="2026-05-19T10:15:00+01:00",
        status="done",
        steps=[StepState(n=1, name="trivial", status="done", commit="abc", smoke="pass")],
        run_log="/tmp/test/run.log",
        deploy=deploy,
    )


class TestCompletionMessage(unittest.TestCase):
    def test_no_deploy_no_block(self) -> None:
        """state.deploy is None -> no Deploy: block in message."""
        brief = _make_brief()
        state = _make_state(deploy=None)
        msg = voice.format_completion(brief, state)
        self.assertIn("Build complete", msg)
        self.assertNotIn("Deploy:", msg)

    def test_deploy_complete_renders_block(self) -> None:
        """state.deploy populated with stage=complete -> block rendered."""
        brief = _make_brief(service_name="test.service")
        state = _make_state(deploy={
            "stage": "complete", "ok": True, "output": "",
            "vps_head_sha": "fed987654321abc", "service_status": "active",
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("Deploy:", msg)
        self.assertIn("complete", msg)
        self.assertIn("fed9876", msg)  # first 7 chars of sha
        self.assertIn("active", msg)
        self.assertIn("test.service", msg)

    def test_deploy_failed_renders_block_with_failed(self) -> None:
        """state.deploy with ok=False renders 'failed' marker."""
        brief = _make_brief(service_name="test.service")
        state = _make_state(deploy={
            "stage": "pull", "ok": False, "output": "non-fast-forward",
            "vps_head_sha": None, "service_status": None,
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("pull", msg)
        self.assertIn("failed", msg)

    def test_deploy_sha_truncated_to_7(self) -> None:
        """vps_head_sha is rendered as first 7 chars."""
        brief = _make_brief(service_name="test.service")
        state = _make_state(deploy={
            "stage": "complete", "ok": True, "output": "",
            "vps_head_sha": "abcdef1234567890123456789",
            "service_status": "active",
        })
        msg = voice.format_completion(brief, state)
        self.assertIn("abcdef1", msg)


if __name__ == "__main__":
    unittest.main()
'''
    TEST_FILE.write_text(content)
    info("created tests/test_voice_completion.py")
    return True


def main() -> int:
    if not HARNESS_PY.exists():
        fail(f"exam_harness.py not found at {HARNESS_PY}")
    if not VOICE_PY.exists():
        fail(f"voice.py not found at {VOICE_PY}")

    changed_h = patch_harness()
    changed_v = patch_voice()
    changed_f = create_fixture()
    changed_t = create_voice_test()

    if not (changed_h or changed_v or changed_f or changed_t):
        info("nothing to do — patch already fully applied")
        return 0

    import py_compile
    try:
        py_compile.compile(str(HARNESS_PY), doraise=True)
        py_compile.compile(str(VOICE_PY), doraise=True)
        py_compile.compile(str(TEST_FILE), doraise=True)
        info("compile-check passed")
    except py_compile.PyCompileError as e:
        fail(f"compile-check failed: {e}")

    info("Step 6 patch applied. Next: run smoke")
    info("  .venv/bin/python tools/exam_harness.py --self-check")
    info("  .venv/bin/python -m unittest tests.test_voice_completion -v")
    info("  (then full discover suite)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
