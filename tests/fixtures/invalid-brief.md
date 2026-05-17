---
brief_version: 2
target_repo: ""
target_repo_path: /tmp/anvil-definitely-not-a-repo-xyzzy
vps_deploy: yes
---

## Goal
Deliberately invalid brief for validator tests. Multiple simultaneous
violations so test_brief can assert the validator lists ALL of them.

## Context
- [[does/not/exist-xyzzy]]

## Steps

### Step 1 — Bad step
- **scope.files:** ../outside.txt
- **scope.operations:** write, teleport
- **smoke:** `echo pass`
- **confirm:** explicit
- **notes:** Violates rule 7 (../ escape) and rule 8 (unknown operation 'teleport').
