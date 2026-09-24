# macOS Recovery Dialog Stage Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, privacy-safe stages that identify which native macOS recovery-dialog boundary stops without changing application behavior.

**Architecture:** Extend the gate-local workspace dialog binding with one explicitly armed recovery attempt and five once-only stage observations. Arm it immediately before a successful nonempty recovery result is delegated, then observe the concrete dialog, scheduler, button, and modal return while preserving the original callable, return value, exception behavior, and native dialog path.

**Tech Stack:** Python 3.12, PySide6, pytest, ruff, mypy, GitHub Actions release qualification.

---

### Task 1: Specify recovery-attempt scoping and stage order with failing tests

**Files:**
- Modify: `tests/release/test_trial_technical_gate.py`
- Reference: `scripts/run_trial_technical_gate.py:27-91`
- Reference: `scripts/run_trial_technical_gate.py:949-1089`

- [ ] **Step 1: Extend the native-dialog binding regression with canonical stages**

Update `test_workspace_dialog_binding_drives_message_without_global_discovery`
to install the binding with a recorder, arm one recovery attempt, and supply
bound-poll and button-resolved callbacks to the scheduler. Keep all global
module patches in `try/finally`, restore the binding, and close widgets. Assert
this exact order around the existing visible and selected callbacks:

```python
assert boundaries == [
    "recovery-dialog-boundary-entered",
    "recovery-dialog-instance-bound",
    "recovery-dialog-poll-entered",
    "visible",
    "recovery-dialog-button-resolved",
    "selected",
    "recovery-dialog-modal-returned",
]
```

- [ ] **Step 2: Add scoping and fallback regressions**

Add tests proving:

```python
binding = gate._install_workspace_message_binding(record=stages.append)
workspace_window.workspace_dialog(logical_window, lambda: "ok")
assert stages == []

binding.arm_recovery_attempt()
workspace_window.workspace_dialog(logical_window, lambda: "ok")
assert stages == ["recovery-dialog-boundary-entered"]
```

Add an armed different-title `QMessageBox` invocation using a nonblocking bound
method such as `dialog.show`. Assert it adds only
`recovery-dialog-boundary-entered`; it must not emit instance, poll, button, or
modal-return stages.

Add an unarmed, registered non-recovery dialog test. Assert the concrete
instance is still retained and handled through the existing title lookup while
none of the five new stages is emitted. Add the same regression for an unarmed,
registered `Recover unfinished work` dialog: the existing interaction succeeds,
but no diagnostic stage is emitted. Extend the existing global-discovery test
with the two new scheduler callbacks and assert neither callback runs when
`current_message()` did not return the separately retained diagnostic instance.

- [ ] **Step 3: Require arming before nonempty-result delegation**

Extend the successful nonempty recovery-result test with a binding spy:

```python
events: list[str] = []
binding = SimpleNamespace(
    arm_recovery_attempt=lambda: events.append("armed")
)

def handle_workspace_result(value: object) -> str:
    events.append("delegated")
    return "handled"
```

Pass the spy to `_instrument_recovery_boundaries` and assert
`events == ["armed", "delegated"]`. Empty and failed results must not arm it.

- [ ] **Step 4: Add once-only and delegation-semantics regressions**

Add a test that re-arms and re-enters the recovery boundary, then exercises the
scheduler observation callbacks more than once. Assert every fixed diagnostic
stage occurs exactly once for the binding lifetime.

Add a forwarding test that passes positional arguments through the patched
boundary and asserts identity of the original return object. Add a fixed-title,
bound-instance exception test whose original callable raises a sentinel
exception. Assert the same exception object propagates and diagnostic stages end
at `recovery-dialog-instance-bound`, without modal return. Restore every module
patch and close every widget in `finally`.

Extend the production-wiring source regression to require all five exact stage
strings, `record=` during binding installation, and `message_binding=` when
recovery boundaries are instrumented.

- [ ] **Step 5: Run the focused tests and confirm RED**

Run:

```bash
rtk env PYTHONPATH=/home/washimi/work/studio/worktrees/gwexpy-studio-cross-platform-trial/src QT_QPA_PLATFORM=offscreen /home/washimi/work/studio/worktrees/gwexpy-studio-m2-preflight/.venv/bin/python -m pytest -q tests/release/test_trial_technical_gate.py -k 'workspace_dialog_binding or recovery_dialog or recovery_dispatch_diagnostics'
```

Expected: every new test is selected, and the run FAILS because the binding has
no recorder or arming API, the scheduler has no diagnostic callbacks, and
recovery instrumentation does not receive the binding.

### Task 2: Implement the minimal gate-local diagnostic state

**Files:**
- Modify: `scripts/run_trial_technical_gate.py:27-91`
- Modify: `scripts/run_trial_technical_gate.py:949-1089`
- Modify: `scripts/run_trial_technical_gate.py:1092-1268`
- Test: `tests/release/test_trial_technical_gate.py`

- [ ] **Step 1: Add an armed, once-only workspace binding**

Extend `_WorkspaceMessageBinding` with a `Callable[[str], None]` recorder, a
boolean recovery-attempt token, and a binding-lifetime set of emitted canonical
stage names. A private helper accepts only hard-coded stage names and emits each
name at most once. Add `arm_recovery_attempt()` and consume the token on the
first patched `workspace_dialog` call. Record boundary entry before inspecting
`execute`.

Keep the existing generic `message_type` and title lookup active regardless of
recovery arming, so registered non-recovery dialogs still receive
`scheduled.bind(message)`. Gate only the five new diagnostic records on the
consumed recovery attempt and fixed `Recover unfinished work` title. For that
diagnostic instance, also retain it separately from the generic
`bound_message`, then record `recovery-dialog-instance-bound`. Call the original
function with unchanged arguments. Record
`recovery-dialog-modal-returned` only after that original call returns for the
same bound instance. Do not record modal return if the original raises.

Keep `restore()` responsible for restoring the original module function and
clearing any armed attempt.

- [ ] **Step 2: Observe the bound scheduler without changing its click path**

Add optional `on_bound_poll` and `on_button_resolved` zero-argument callbacks to
`_schedule_message_box_button`. Invoke them only when the current message is
identical to the separately retained diagnostic instance; neither an unarmed
generic binding nor fallback discovery may emit either diagnostic. Route both
callbacks through the binding's same lifetime-idempotent stage recorder. Invoke
bound-poll after resolving the diagnostic instance and button-resolved after
resolving the expected button but before the existing `button.click()` call.

Do not alter timer intervals, deadlines, global-discovery fallback, button
matching, or failure handling.

- [ ] **Step 3: Arm at the verified recovery-result boundary**

Add `message_binding: _WorkspaceMessageBinding | None = None` to
`_instrument_recovery_boundaries`. For a successful nonempty `list_recoveries`
result, record `recovery-candidates-received`, arm the binding, and immediately
delegate to the original result handler.

Install `_WorkspaceMessageBinding` with the existing canonical recorder in
`_run_consumer_launcher`, pass it to recovery instrumentation, and connect the
two scheduler callbacks to:

```python
"recovery-dialog-poll-entered"
"recovery-dialog-button-resolved"
```

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run the command from Task 1, Step 5.

Expected: all selected tests PASS.

- [ ] **Step 5: Run the complete release gate test file**

Run:

```bash
rtk env PYTHONPATH=/home/washimi/work/studio/worktrees/gwexpy-studio-cross-platform-trial/src QT_QPA_PLATFORM=offscreen /home/washimi/work/studio/worktrees/gwexpy-studio-m2-preflight/.venv/bin/python -m pytest -q tests/release/test_trial_technical_gate.py
```

Expected: PASS with no new warnings or hangs.

### Task 3: Prepare and verify the exact public-only commit

**Files:**
- Modify: `scripts/run_trial_technical_gate.py`
- Modify: `tests/release/test_trial_technical_gate.py`
- Local-only: `docs/superpowers/specs/2026-09-11-macos-recovery-dialog-stage-diagnostics-design.md`
- Local-only: `docs/superpowers/plans/2026-09-11-macos-recovery-dialog-stage-diagnostics.md`

- [ ] **Step 1: Build a clean public branch history**

Create the final PR commit directly on top of `origin/main` with only the gate
script and release test changes. Do not include either `docs/superpowers/**`
file because `packaging/release-source-allowlist.txt` explicitly denies that
tree. Preserve the local documentation commits outside the branch sent to
GitHub.

Assert the final diff contains exactly:

```text
scripts/run_trial_technical_gate.py
tests/release/test_trial_technical_gate.py
```

Require a clean worktree after creating the final commit.

- [ ] **Step 2: Run static validation on the final PR commit**

```bash
rtk /home/washimi/work/studio/worktrees/gwexpy-studio-m2-preflight/.venv/bin/python -m ruff check scripts/run_trial_technical_gate.py tests/release/test_trial_technical_gate.py
rtk /home/washimi/work/studio/worktrees/gwexpy-studio-m2-preflight/.venv/bin/python -m mypy scripts/run_trial_technical_gate.py
rtk git diff --check origin/main
```

Expected: all commands PASS.

- [ ] **Step 3: Run release and GUI regression suites on the final PR commit**

```bash
rtk env PYTHONPATH=/home/washimi/work/studio/worktrees/gwexpy-studio-cross-platform-trial/src QT_QPA_PLATFORM=offscreen /home/washimi/work/studio/worktrees/gwexpy-studio-m2-preflight/.venv/bin/python -m pytest -q tests/release
rtk env PYTHONPATH=/home/washimi/work/studio/worktrees/gwexpy-studio-cross-platform-trial/src QT_QPA_PLATFORM=offscreen /home/washimi/work/studio/worktrees/gwexpy-studio-m2-preflight/.venv/bin/python -m pytest -q gui_tests
```

Expected: both suites PASS.

- [ ] **Step 4: Run the full test suite on the final PR commit**

```bash
rtk env PYTHONPATH=/home/washimi/work/studio/worktrees/gwexpy-studio-cross-platform-trial/src QT_QPA_PLATFORM=offscreen /home/washimi/work/studio/worktrees/gwexpy-studio-m2-preflight/.venv/bin/python -m pytest -q
```

Expected: PASS with the installed import root still bound to this worktree's
`src` directory.

- [ ] **Step 5: Verify public source policy and manifest on the final PR commit**

```bash
rtk python3.12 scripts/verify_public_source.py --root . --allowlist packaging/release-source-allowlist.txt --json
rtk python3.12 scripts/release_source_manifest.py build --public-checkout --root . --allowlist packaging/release-source-allowlist.txt --output /tmp/gwexpy-macos-dialog-diagnostics-SOURCE-MANIFEST.json --json
rtk sha256sum /tmp/gwexpy-macos-dialog-diagnostics-SOURCE-MANIFEST.json
```

Expected: policy PASS and a recorded manifest SHA-256.

- [ ] **Step 6: Review and GitHub-write gate**

Run an independent code review. Then scan the exact commit, branch, PR title,
PR body, and file/path payload with the manual secret patterns and gitleaks.
Preview the complete GitHub write payload and obtain explicit approval before
push or PR creation.

### Task 4: Merge and rerun the bounded macOS diagnostic

**Files:**
- No additional source changes expected.

- [ ] **Step 1: Push the approved branch and create the approved PR**

Wait for all public CI checks and confirm the PR head SHA did not change.

- [ ] **Step 2: Approve and perform the exact merge write**

After CI succeeds and the immutable PR head SHA is confirmed, scan and preview
the exact repository, PR number, head SHA, base SHA, and merge method. Obtain
explicit approval for that merge payload, then merge normally without deleting
the branch.

- [ ] **Step 3: Fix the new source commit**

Verify the merge commit equals remote `main`, rerun public-source policy and the
source manifest in a fresh checkout, and record the new final `P`.

- [ ] **Step 4: Prepare the exact diagnostic dispatch payload**

Use:

```text
workflow: build-trial-wheel.yml
ref: main
source_sha: <new P>
trial_target: macos15-arm64
```

Scan and preview this new GitHub write payload, then obtain explicit approval.
Immediately before dispatch, fetch remote `main` again and require it to equal
the approved new `P`.

- [ ] **Step 5: Run and interpret one diagnostic Build**

Fix the returned run ID. Verify `workflow_dispatch`, attempt 1, and
`headSha=<new P>`. If the installed gate fails, extract only the last canonical
`phase/stage` token. Do not publish raw paths, usernames, hostnames, or exception
text. Do not publish a Release or treat this run as formal qualification.
