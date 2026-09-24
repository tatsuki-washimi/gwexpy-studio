# macOS recovery dialog stage diagnostics

## Background

macOS ARM64 diagnostic run `34594170555` reached
`consumer/recovery-candidates-received` and then exhausted the bounded consumer
subprocess without producing qualification evidence. The call-boundary binding
introduced in PR #15 therefore did not make the recovery dialog observable and
selectable, but the current evidence cannot distinguish a missed module hook, a
missing `QMessageBox` instance, or a suspended Qt timer.

This change adds canonical stage observations only. It does not change product
behavior, dialog modality, timeout values, or release eligibility.

## Diagnostic boundary

The installed-wheel technical gate records five new stages for the recovery
candidate dialog:

1. `consumer/recovery-dialog-boundary-entered`: the first patched
   `workspace_dialog` call boundary was entered while an explicit recovery
   attempt was armed.
2. `consumer/recovery-dialog-instance-bound`: the invocation exposed a concrete
   `QMessageBox` instance and the scheduler retained it.
3. `consumer/recovery-dialog-poll-entered`: the scheduler ran after an instance
   had been bound.
4. `consumer/recovery-dialog-button-resolved`: the scheduler found the expected
   Restore button on that instance.
5. `consumer/recovery-dialog-modal-returned`: the wrapped original
   `workspace_dialog` call returned after the bound recovery interaction.

Each stage is emitted at most once. Boundary entry is scoped to the explicit
recovery attempt. The remaining stages are scoped to the fixed
`Recover unfinished work` title and its bound instance. Unrelated dialogs retain
their existing behavior and do not emit diagnostic stages when no recovery
attempt is armed.

## Data flow

The recovery-boundary instrumentation arms one recovery attempt immediately
before delegating a successful, nonempty `list_recoveries` result to the original
handler. The first patched `workspace_dialog` invocation consumes that token and
records boundary entry before inspecting the bound callable. When the callable
exposes a `QMessageBox` with the fixed recovery title, the binding records
successful binding and passes the concrete instance to the existing scheduler.
The scheduler records its first bound-instance poll and successful button
resolution before it uses the existing click path. The patched boundary records
modal return only after the original `workspace_dialog` invocation returns for
that same bound recovery instance.

No exception text, widget text other than the already fixed title and button
contract, filesystem path, username, or hostname is written to evidence.

## Interpretation

- `consumer/recovery-candidates-received` remains last: the successful nonempty
  result armed the attempt, but delegation did not reach the patched module
  boundary.
- Boundary only: the callable did not expose a compatible `QMessageBox` instance.
- Instance bound only: the native modal loop did not service the scheduler timer.
- Poll entered only: the expected Restore button could not be resolved.
- Button resolved without the existing selected stage: the click invocation did
  not complete.
- Existing selected stage without modal returned: the native modal loop did not
  unwind after the button click.
- Modal returned: the native dialog interaction completed and returned control to
  the application recovery handler.

These results determine whether the next change belongs in module binding,
scheduler integration, or the product dialog architecture. No functional fix is
included in this diagnostic change.

## Verification

Tests exercise each diagnostic boundary without depending on global modal-widget
discovery. They prove that a successful nonempty recovery result arms the token
before delegation. They cover an unbound callable, unrelated dialogs outside an
armed attempt, and an armed invocation whose bound `QMessageBox` has a different
title. That different-title case emits boundary entry only. Tests also cover
ordered modal return, once-only emission, and the fixed canonical stage names.
Existing recovery boundary tests, release tests, full pytest, GUI tests, ruff,
mypy, workflow checks, and public source policy remain required before the
diagnostic branch can be merged.

After merge, a new final source commit is fixed and one macOS ARM64 diagnostic
Build is dispatched from that commit. Its last canonical stage is the only new
runtime evidence accepted from the failed installed gate.
