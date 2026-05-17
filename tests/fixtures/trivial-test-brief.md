---
brief_version: 1
project: anvil-test
build_name: "Phase 0 — trivial round-trip"
target_repo: /tmp/anvil-test-repo
target_repo_path: /tmp/anvil-test-repo
vps_deploy: no
---

## Goal
Verify the orchestrator runs end-to-end without doing anything real.

## Context
(none)

## Steps

### Step 1 — Create a file
- **scope.files:** test.txt
- **scope.operations:** write, smoke-test, commit
- **smoke:** `test -f test.txt && echo pass`
- **confirm:** explicit
- **notes:** Write "hello anvil" to test.txt.

### Step 2 — Modify the file
- **scope.files:** test.txt
- **scope.operations:** write, smoke-test, commit
- **smoke:** `grep -q world test.txt && echo pass`
- **confirm:** auto
- **notes:** Append " world" to test.txt.

### Step 3 — Verify and finish
- **scope.files:** test.txt
- **scope.operations:** read, smoke-test, commit
- **smoke:** `grep -q "hello anvil world" test.txt && echo pass`
- **confirm:** explicit
- **notes:** Final verification step. Commit a no-op (touch test.txt to bump mtime).
