"""Run a bounded offscreen technical gate against an installed trial wheel."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


class GateError(RuntimeError):
    """Raised when the installed-wheel technical gate cannot be trusted."""


_RECOVERY_DIALOG_TITLE = "Recover unfinished work"
_RECOVERY_DIALOG_DIAGNOSTIC_STAGES = frozenset(
    {
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-instance-bound",
        "consumer/recovery-dialog-poll-entered",
        "consumer/recovery-dialog-button-resolved",
        "consumer/recovery-dialog-modal-returned",
    }
)
_REVIEW_PRE_DIALOG_TIMEOUT_S = 95.0


class _ScheduledMessageClick:
    """Retain one bounded, Qt-owned message-box interaction."""

    def __init__(
        self,
        *,
        poll_timer: Any,
        deadline_timer: Any,
        pre_dialog_timer: Any | None = None,
        review: bool = False,
    ) -> None:
        self._poll_timer = poll_timer
        self._deadline_timer = deadline_timer
        self._pre_dialog_timer = pre_dialog_timer
        self.review = review
        self.bound_message: Any | None = None
        self.diagnostic_message: Any | None = None
        self._diagnostic_binding: _WorkspaceMessageBinding | None = None
        self._diagnostic_poll_callback: Callable[[], None] | None = None
        self._diagnostic_button_callback: Callable[[], None] | None = None
        self._on_visible: Callable[[], None] | None = None
        self._on_selected: Callable[[], None] | None = None
        self._timers_released = False
        self.diagnostic_selection_succeeded = False
        self.error: BaseException | None = None
        self.handled = False

    def start_pre_dialog(
        self, timeout_s: float, callback: Callable[[], None]
    ) -> None:
        """Start only the bounded wait for the asynchronous Review result."""
        timer = self._pre_dialog_timer
        if timer is None:
            callback()
            return
        timeout_ms = max(1, int(timeout_s * 1000))
        timer.timeout.connect(callback)
        timer.start(timeout_ms)

    def start_interaction(self) -> None:
        """Start the visibility and selection timers after Review binding."""
        self._poll_timer.start()
        self._deadline_timer.start()

    def stop_pre_dialog(self) -> None:
        """Stop the asynchronous-result wait without releasing interaction timers."""
        timer = self._pre_dialog_timer
        if timer is not None:
            try:
                timer.stop()
            except BaseException:
                pass

    def bind(self, message: Any) -> None:
        """Retain the concrete message passed into the workspace dialog boundary."""
        if self.bound_message is not None and self.bound_message is not message:
            raise GateError("technical-gate recovery dialog binding failed")
        self.bound_message = message

    def retain_diagnostic(
        self,
        message: Any,
        binding: _WorkspaceMessageBinding,
        *,
        on_poll: Callable[[], None],
        on_button: Callable[[], None],
    ) -> None:
        """Retain the exact message eligible for fixed recovery diagnostics."""
        self.diagnostic_message = message
        self._diagnostic_binding = binding
        if self._diagnostic_poll_callback is None:
            self._diagnostic_poll_callback = on_poll
        if self._diagnostic_button_callback is None:
            self._diagnostic_button_callback = on_button

    def diagnostic_poll_entered(self) -> None:
        """Run the bound-poll callback only for the retained message identity."""
        if self._diagnostic_binding is not None:
            self._diagnostic_binding._record_diagnostic_stage(
                "consumer/recovery-dialog-poll-entered"
            )
        if self._diagnostic_poll_callback is not None:
            self._diagnostic_poll_callback()

    def diagnostic_button_resolved(self) -> None:
        """Run the button callback only for the retained message identity."""
        if self._diagnostic_binding is not None:
            self._diagnostic_binding._record_diagnostic_stage(
                "consumer/recovery-dialog-button-resolved"
            )
        if self._diagnostic_button_callback is not None:
            self._diagnostic_button_callback()

    def diagnostic_selection_complete(self) -> None:
        """Mark the same scheduler complete after click and selected callback."""
        self.diagnostic_selection_succeeded = True

    def clear_binding(self) -> None:
        """Release Qt and diagnostic references when the binding is restored."""
        self.stop()
        self.bound_message = None
        self.diagnostic_message = None
        self._diagnostic_binding = None
        self._diagnostic_poll_callback = None
        self._diagnostic_button_callback = None
        self._on_visible = None
        self._on_selected = None

    def stop(self) -> None:
        """Stop both owner-bound timers once this interaction is settled."""
        for timer in (
            self._poll_timer,
            self._deadline_timer,
            self._pre_dialog_timer,
        ):
            if timer is None:
                continue
            try:
                stop = getattr(timer, "stop", None)
                if callable(stop):
                    stop()
            except BaseException:
                pass
            if not self._timers_released:
                try:
                    delete_later = getattr(timer, "deleteLater", None)
                    if callable(delete_later):
                        delete_later()
                except BaseException:
                    pass
        self._timers_released = True

    def raise_if_failed(self) -> None:
        """Re-raise a callback failure after its nested Qt modal loop returns."""
        if self.error is not None:
            if isinstance(self.error, GateError):
                raise self.error
            raise GateError("technical-gate message interaction failed") from self.error


class _WorkspaceMessageBinding:
    """Bind locally-created message boxes before their native modal loop starts.

    The optional ``record=`` callback receives only the fixed diagnostic stage
    names defined above. Recovery uses a concrete message instance supplied by
    the workspace dialog boundary; generic dialogs retain their old fallback.
    """

    _FIXED_ERROR = "technical-gate recovery dialog binding failed"
    _REVIEW_FIXED_ERROR = "technical-gate review dialog binding failed"
    _MISSING = object()

    def __init__(
        self,
        *,
        workspace_module: Any,
        message_type: type[Any],
        record: Callable[[str], None] | None = None,
    ) -> None:
        self._workspace_module = workspace_module
        self._message_type = message_type
        self._original = workspace_module.workspace_dialog
        self._scheduled: dict[str, _ScheduledMessageClick] = {}
        self._recovery_scheduler: _ScheduledMessageClick | Any | None = None
        self._recovery_expected_instance: Any = self._MISSING
        self._record = record or (lambda _stage: None)
        self._emitted_stages: set[str] = set()
        self._recovery_attempt = "unarmed"
        self._diagnostic_message: Any | None = None
        self._diagnostic_scheduler: Any | None = None
        self._recovery_modal_active = False
        self._binding_error: GateError | None = None
        self._review_scheduler: _ScheduledMessageClick | Any | None = None
        self._review_expected_instance: Any = self._MISSING
        self._review_command_id: Any = self._MISSING
        self._review_attempt = "unarmed"
        self._review_modal_active = False
        self._review_error: GateError | None = None
        self._review_message: Any | None = None

        def workspace_dialog(
            window: Any,
            execute: Any,
            *args: Any,
            dialog_instance: Any | None = None,
            **kwargs: Any,
        ) -> Any:
            if self._review_attempt != "unarmed":
                return self._review_dialog(
                    window,
                    execute,
                    *args,
                    dialog_instance=dialog_instance,
                    **kwargs,
                )
            diagnostic_attempt = self._recovery_attempt == "armed"
            if not diagnostic_attempt and not self._recovery_modal_active:
                # Keep the ordinary title-bound helper for non-Recovery
                # dialogs. The armed branch below intentionally never reads
                # callable owners or searches application widgets.
                message = (
                    dialog_instance
                    if dialog_instance is not None
                    else getattr(execute, "__self__", None)
                )
                if message is not None and isinstance(message, self._message_type):
                    title = message.windowTitle()
                    scheduled = self._scheduled.get(title)
                    if scheduled is not None:
                        scheduled.bind(message)
                return self._original(window, execute, *args, **kwargs)
            if self._recovery_modal_active:
                self._terminal_failure()
                return None
            self._recovery_modal_active = True
            try:
                try:
                    self._record_diagnostic_stage(
                        "consumer/recovery-dialog-boundary-entered"
                    )
                except BaseException:
                    self._terminal_failure()
                    return None
                if self._binding_error is not None:
                    return None
                if not self._associate_recovery_instance(dialog_instance):
                    return None
                if self._binding_error is not None:
                    return None
                try:
                    # ``dialog_instance`` is binding metadata, not an argument
                    # for either the boundary implementation or the callable.
                    result = self._original(window, execute, *args, **kwargs)
                except BaseException:
                    raise
                scheduler = self._diagnostic_scheduler
                if scheduler is None:
                    self._terminal_failure()
                    return None
                if scheduler.error is not None:
                    return result
                if not scheduler.diagnostic_selection_succeeded:
                    self._terminal_failure()
                    return None
                try:
                    self._record_diagnostic_stage(
                        "consumer/recovery-dialog-modal-returned"
                    )
                except BaseException:
                    self._terminal_failure()
                    return None
                return result
            finally:
                self._recovery_modal_active = False
                self._release_recovery()

        self._replacement = workspace_dialog
        workspace_module.workspace_dialog = workspace_dialog

    def register(self, title: str, scheduled: _ScheduledMessageClick) -> None:
        """Register the one pending interaction for a message title."""
        self._scheduled[title] = scheduled

    def register_review(
        self,
        scheduled: Any,
        *,
        dialog_instance: Any = _MISSING,
        pre_dialog_timeout_s: float = _REVIEW_PRE_DIALOG_TIMEOUT_S,
    ) -> None:
        """Reserve one dormant scheduler for the Review confirmation dialog."""
        if self._review_scheduler is not None or self._review_attempt != "unarmed":
            self._review_terminal_failure(scheduled)
            raise self._review_error or GateError(self._REVIEW_FIXED_ERROR)
        self._review_scheduler = scheduled
        self._review_expected_instance = dialog_instance
        self._review_attempt = "pending"
        start_pre_dialog = getattr(scheduled, "start_pre_dialog", None)
        if not callable(start_pre_dialog):
            self._review_terminal_failure(scheduled)
            raise self._review_error or GateError(self._REVIEW_FIXED_ERROR)
        try:
            start_pre_dialog(pre_dialog_timeout_s, lambda: self._review_timeout())
        except BaseException:
            self._review_terminal_failure(scheduled)
            raise self._review_error or GateError(self._REVIEW_FIXED_ERROR)

    def correlate_review_dispatch(self, command_id: Any) -> None:
        """Associate the dormant Review scheduler with one accepted command."""
        if (
            self._review_attempt != "pending"
            or self._review_scheduler is None
            or command_id is self._MISSING
            or command_id is None
            or (
                self._review_command_id is not self._MISSING
                and command_id != self._review_command_id
            )
        ):
            self._review_terminal_failure()
            return
        self._review_command_id = command_id

    def arm_review(self, *, command_id: Any = _MISSING) -> bool:
        """Arm Review only for its correlated successful command result."""
        if self._review_attempt != "pending" or self._review_scheduler is None:
            return False
        if (
            self._review_command_id is self._MISSING
            or command_id is self._MISSING
            or command_id != self._review_command_id
        ):
            return False
        self._review_attempt = "armed"
        self._review_command_id = self._MISSING
        stop_pre_dialog = getattr(self._review_scheduler, "stop_pre_dialog", None)
        if callable(stop_pre_dialog):
            stop_pre_dialog()
        return True

    def fail_review(self) -> GateError:
        """Leave a terminal tombstone that rejects late Review dialogs."""
        return self._review_terminal_failure()

    @property
    def review_attempt(self) -> str:
        """Return the dedicated Review two-phase state."""
        return self._review_attempt

    def _review_dialog(
        self,
        window: Any,
        execute: Any,
        *args: Any,
        dialog_instance: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run the dedicated Review modal only for its exact QMessageBox."""
        if self._review_modal_active:
            self._review_terminal_failure()
            return None
        scheduler = self._review_scheduler
        if self._review_attempt != "armed" or scheduler is None:
            self._review_terminal_failure()
            return None
        self._review_modal_active = True
        original_boundary_error: BaseException | None = None
        try:
            if dialog_instance is None or not isinstance(
                dialog_instance, self._message_type
            ):
                self._review_terminal_failure(scheduler)
                return None
            if (
                self._review_expected_instance is not self._MISSING
                and dialog_instance is not self._review_expected_instance
            ):
                self._review_terminal_failure(scheduler)
                return None
            scheduler.bind(dialog_instance)
            self._review_message = dialog_instance
            start_interaction = getattr(scheduler, "start_interaction", None)
            if not callable(start_interaction):
                self._review_terminal_failure(scheduler)
                return None
            start_interaction()
            try:
                result = self._original(window, execute, *args, **kwargs)
            except BaseException as exc:
                self._review_terminal_failure(scheduler)
                original_boundary_error = exc
                raise
            if scheduler.error is not None or not scheduler.handled:
                self._review_terminal_failure(scheduler)
                return None
            from PySide6.QtWidgets import QMessageBox

            if result != QMessageBox.StandardButton.Ok:
                self._review_terminal_failure(scheduler)
                return None
            self._review_attempt = "consumed"
            return result
        except BaseException:
            if original_boundary_error is not None:
                raise
            self._review_terminal_failure(scheduler)
            return None
        finally:
            self._review_modal_active = False
            self._release_review()

    def _review_timeout(self) -> None:
        """Tombstone a Review whose result or dialog took too long."""
        if self._review_attempt == "pending":
            self._review_terminal_failure()

    def _review_terminal_failure(self, scheduler: Any | None = None) -> GateError:
        """Fail closed and retain a tombstone for every late Review boundary."""
        if self._review_error is None:
            self._review_error = GateError(self._REVIEW_FIXED_ERROR)
        self._review_attempt = "tombstone"
        candidates = [scheduler, self._review_scheduler]
        seen: set[int] = set()
        for candidate in candidates:
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            try:
                candidate.error = self._review_error
            except BaseException:
                pass
            try:
                stop = getattr(candidate, "stop", None)
                if callable(stop):
                    stop()
            except BaseException:
                pass
            try:
                clear_binding = getattr(candidate, "clear_binding", None)
                if callable(clear_binding):
                    clear_binding()
            except BaseException:
                pass
        message = self._review_message
        if message is not None:
            for method_name in ("reject", "close"):
                try:
                    method = getattr(message, method_name, None)
                except BaseException:
                    continue
                if not callable(method):
                    continue
                try:
                    method()
                except BaseException:
                    continue
                break
        self._review_scheduler = None
        self._review_expected_instance = self._MISSING
        self._review_command_id = self._MISSING
        self._review_message = None
        return self._review_error

    def _release_review(self) -> None:
        """Release all Qt and callback references after Review settles."""
        scheduler = self._review_scheduler
        try:
            if scheduler is not None:
                clear_binding = getattr(scheduler, "clear_binding", None)
                if callable(clear_binding):
                    clear_binding()
        except BaseException:
            pass
        finally:
            self._review_scheduler = None
            self._review_expected_instance = self._MISSING
            self._review_command_id = self._MISSING
            self._review_message = None

    def release_scheduled(self, scheduled: Any) -> None:
        """Remove and clean up one generic scheduler after it settles."""
        self._scheduled = {
            title: item
            for title, item in self._scheduled.items()
            if item is not scheduled
        }
        clear_binding = getattr(scheduled, "clear_binding", None)
        if callable(clear_binding):
            clear_binding()

    def register_recovery(
        self, scheduled: Any, *, dialog_instance: Any = _MISSING
    ) -> None:
        """Reserve one scheduler for the armed Recovery interaction."""
        if self._recovery_scheduler is not None:
            self._terminal_failure(scheduled)
            clear_binding = getattr(scheduled, "clear_binding", None)
            if callable(clear_binding):
                clear_binding()
            raise self._binding_error or GateError(self._FIXED_ERROR)
        self._recovery_scheduler = scheduled
        self._recovery_expected_instance = dialog_instance

    @property
    def error(self) -> GateError | None:
        """Return the fixed error captured for an invalid binding state."""
        return self._binding_error or self._review_error

    @property
    def binding_error(self) -> GateError | None:
        """Alias the fixed binding error for consumer wait predicates."""
        return self._binding_error or self._review_error

    @property
    def recovery_armed(self) -> bool:
        """Return whether the single-use recovery result has armed this binding."""
        return self._recovery_attempt == "armed"

    @property
    def recovery_attempt(self) -> str:
        """Return the bounded recovery attempt state for diagnostics and tests."""
        return self._recovery_attempt

    @property
    def recovery_binding_isolated(self) -> bool:
        """Return whether Recovery owns no binding or active-modal references."""
        return (
            not self._recovery_modal_active
            and self._binding_error is None
            and self._diagnostic_message is None
            and self._diagnostic_scheduler is None
            and self._recovery_scheduler is None
            and self._recovery_expected_instance is self._MISSING
        )

    def arm_recovery(self) -> None:
        """Arm exactly one successful nonempty recovery result."""
        if self._recovery_attempt == "unarmed":
            self._recovery_attempt = "armed"

    def _terminal_failure(self, scheduler: Any | None = None) -> GateError:
        """Consume Recovery and stop every pending scheduler on a binding error."""
        if self._binding_error is None:
            self._binding_error = GateError(self._FIXED_ERROR)
        self._recovery_attempt = "consumed"
        self._reject_bound_dialog()
        candidates = [scheduler, self._diagnostic_scheduler, self._recovery_scheduler]
        seen: set[int] = set()
        for candidate in candidates:
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            try:
                candidate.error = self._binding_error
            except BaseException:
                pass
            try:
                stop = getattr(candidate, "stop", None)
                if callable(stop):
                    stop()
            except BaseException:
                pass
        return self._binding_error

    def _reject_bound_dialog(self) -> None:
        """Close an active bound dialog without leaking Qt-side errors."""
        message = self._diagnostic_message
        if message is None and self._recovery_scheduler is not None:
            try:
                message = self._recovery_scheduler.bound_message
            except BaseException:
                message = None
        if message is None:
            return
        for method_name in ("reject", "close"):
            try:
                method = getattr(message, method_name, None)
            except BaseException:
                continue
            if not callable(method):
                continue
            try:
                method()
            except BaseException:
                continue
            break

    def _associate_recovery_instance(self, dialog_instance: Any | None) -> bool:
        """Bind the explicit message to the dedicated Recovery scheduler."""
        scheduler = self._recovery_scheduler
        if scheduler is None:
            self._terminal_failure()
            return False
        if dialog_instance is None or not isinstance(
            dialog_instance, self._message_type
        ):
            self._terminal_failure()
            return False
        if (
            self._recovery_expected_instance is not self._MISSING
            and dialog_instance is not self._recovery_expected_instance
        ):
            self._terminal_failure()
            return False
        if self._diagnostic_message is not None:
            self._terminal_failure()
            return False
        try:
            scheduler.bind(dialog_instance)
            scheduler.retain_diagnostic(
                dialog_instance,
                self,
                on_poll=lambda: None,
                on_button=lambda: None,
            )
        except BaseException:
            self._terminal_failure(scheduler)
            return False
        self._recovery_attempt = "consumed"
        self._diagnostic_message = dialog_instance
        self._diagnostic_scheduler = scheduler
        try:
            self._record_diagnostic_stage("consumer/recovery-dialog-instance-bound")
        except BaseException:
            self._terminal_failure(scheduler)
            return False
        return True

    def _release_recovery(self) -> None:
        """Release the explicit message and scheduler after modal return."""
        scheduler = self._diagnostic_scheduler or self._recovery_scheduler
        try:
            if scheduler is not None:
                clear_binding = getattr(scheduler, "clear_binding", None)
                if callable(clear_binding):
                    clear_binding()
        except BaseException:
            pass
        finally:
            self._diagnostic_message = None
            self._diagnostic_scheduler = None
            self._recovery_scheduler = None
            self._recovery_expected_instance = self._MISSING

    def _record_diagnostic_stage(self, stage: str) -> None:
        """Record one fixed diagnostic stage and fail closed on recorder errors."""
        if stage not in _RECOVERY_DIALOG_DIAGNOSTIC_STAGES:
            raise GateError("technical-gate diagnostic stage is invalid")
        if stage in self._emitted_stages:
            return
        try:
            self._record(stage)
        except BaseException as exc:
            raise GateError("technical-gate diagnostic stage recorder failed") from exc
        self._emitted_stages.add(stage)

    def restore(self) -> None:
        """Restore the application module after the consumer launcher exits."""
        if self._workspace_module.workspace_dialog is self._replacement:
            self._workspace_module.workspace_dialog = self._original
        scheduled_values = [*self._scheduled.values()]
        if self._recovery_scheduler is not None:
            scheduled_values.append(self._recovery_scheduler)
        if self._review_scheduler is not None:
            scheduled_values.append(self._review_scheduler)
        seen: set[int] = set()
        for scheduled in scheduled_values:
            if id(scheduled) in seen:
                continue
            seen.add(id(scheduled))
            try:
                clear_binding = getattr(scheduled, "clear_binding", None)
                if callable(clear_binding):
                    clear_binding()
            except BaseException:
                pass
        self._scheduled.clear()
        self._recovery_scheduler = None
        self._recovery_expected_instance = self._MISSING
        self._recovery_attempt = "unarmed"
        self._diagnostic_message = None
        self._diagnostic_scheduler = None
        self._recovery_modal_active = False
        self._binding_error = None
        self._review_scheduler = None
        self._review_expected_instance = self._MISSING
        self._review_command_id = self._MISSING
        self._review_attempt = "unarmed"
        self._review_modal_active = False
        self._review_error = None
        self._review_message = None


def _install_workspace_message_binding(
    *, record: Callable[[str], None] | None = None
) -> _WorkspaceMessageBinding:
    """Install a consumer-local binding at the application's dialog boundary."""
    from PySide6.QtWidgets import QMessageBox

    from gwexpy_studio.ui import workspace_window

    return _WorkspaceMessageBinding(
        workspace_module=workspace_window,
        message_type=QMessageBox,
        record=record,
    )


_CHECK_NAMES = (
    "launcher_import",
    "welcome",
    "try_sample",
    "crop",
    "asd",
    "save_project",
    "project_reopen",
    "recovery",
    "worker_exit",
    "shared_memory_cleanup",
    "normal_close_reopen",
    "export_numeric_replay",
    "io_read",
    "io_refusal",
)
_LEGACY_CHECK_NAMES = _CHECK_NAMES[:10]
_MACHINES = frozenset({"x86_64", "aarch64", "arm64"})
_PYTHON_VERSION = re.compile(r"3\.12\.[0-9]+")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_BUILD_ID = re.compile(r"P-[0-9a-f]{7}-[0-9]{8}-r[1-9][0-9]*-a[1-9][0-9]*")
_TRIAL_VERSION = re.compile(
    r"0\.1\.0a1\+trial\.p\.g[0-9a-f]{7}\.[0-9]{8}\.r[1-9][0-9]*\.a[1-9][0-9]*"
)
_GATE_STAGE_FILENAME = "technical-gate-stage.json"
_GATE_STAGE_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PRODUCER_GATE_STAGES = frozenset(
    {
        "bootstrap",
        "launcher",
        "welcome",
        "sample-availability",
        "try-sample",
        "sample-catalog",
        "sample-inspection",
        "sample-read",
        "crop-dialog",
        "crop",
        "asd-dialog",
        "asd",
        "save-project",
        "post-save-input",
        "post-save-crop-dialog",
        "recovery-checkpoint",
        "python-export",
        "export-reference",
        "intentional-crash",
    }
)
_NORMAL_GATE_STAGES = frozenset(
    {
        "bootstrap",
        "launcher",
        "welcome",
        "worker-ready",
        "io-catalog",
        "io-catalog-ready",
        "io-inspection",
        "io-read",
        "io-read-settled",
        "save-project",
        "save-after-refusal",
        "close-project",
        "empty-workspace",
        "open-project",
        "reopened-project",
        "restore-review",
        "restore-review-handled",
        "restored-project",
        "restored-state-settled",
        "io-unavailable",
        "io-refusal",
        "worker-exit",
        "complete",
    }
)
_CONSUMER_GATE_STAGES = frozenset(
    {
        "bootstrap",
        "launcher",
        "project-reopen",
        "recovery-review",
        "recovery-candidate-visible",
        "recovery-candidate-selected",
        "unsaved-project-visible",
        "unsaved-project-discarded",
        "recovery-review-trigger",
        "recovery-review-requested",
        "recovery-list-dispatch-requested",
        "recovery-list-dispatch-accepted",
        "recovery-list-result-received",
        "recovery-list-result-succeeded",
        "recovery-candidates-received",
        "recovery-restored",
        "data-restore",
        "recovery-consumption",
        "restored-project-save",
        "worker-exit",
        "complete",
        "recovery-dialog-boundary-entered",
        "recovery-dialog-instance-bound",
        "recovery-dialog-poll-entered",
        "recovery-dialog-button-resolved",
        "recovery-dialog-modal-returned",
    }
)
_ALLOWED_GATE_STAGE_PAIRS = frozenset(
    {
        *(f"producer/{stage}" for stage in _PRODUCER_GATE_STAGES),
        *(f"normal/{stage}" for stage in _NORMAL_GATE_STAGES),
        *(f"consumer/{stage}" for stage in _CONSUMER_GATE_STAGES),
    }
)
_SEALED_GATE_BOOTSTRAP = (
    "import os as _os\n"
    "import sys as _sys\n"
    "_gate_fd = int(_sys.argv[1])\n"
    "_os.lseek(_gate_fd, 0, _os.SEEK_SET)\n"
    "with _os.fdopen(_gate_fd, 'rb', closefd=False) as _gate_source:\n"
    "    _gate_code = _gate_source.read()\n"
    "_sys.argv = ['<sealed-trial-gate>', *_sys.argv[2:]]\n"
    "_gate_globals = {'__name__': '__main__', '__file__': '<sealed-trial-gate>'}\n"
    "exec(compile(_gate_code, '<sealed-trial-gate>', 'exec'), _gate_globals)\n"
)


def _qapplication_arguments() -> list[str]:
    """Return the concrete argv container accepted by current PySide6 builds."""
    return [sys.argv[0]]


def _record_gate_stage(work_root: Path, phase: str, stage: str) -> None:
    """Persist one bounded, path-free phase marker for failed CI diagnosis."""
    if (
        phase not in {"producer", "normal", "consumer"}
        or _GATE_STAGE_TOKEN.fullmatch(stage) is None
        or f"{phase}/{stage}" not in _ALLOWED_GATE_STAGE_PAIRS
    ):
        raise GateError("technical-gate diagnostic stage is invalid")
    path = work_root / _GATE_STAGE_FILENAME
    if path.is_symlink():
        raise GateError("technical-gate diagnostic stage path is unsafe")
    try:
        path.write_bytes(
            json.dumps(
                {"phase": phase, "stage": stage},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except OSError as exc:
        raise GateError(
            "technical-gate diagnostic stage could not be recorded"
        ) from exc


def read_gate_stage(work_root: Path) -> str:
    """Read one strict phase marker without returning host-specific content."""
    path = work_root / _GATE_STAGE_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            raise GateError("technical-gate diagnostic stage is unavailable")
        content = path.read_bytes()
        document = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("technical-gate diagnostic stage is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"phase", "stage"}
        or document["phase"] not in {"producer", "normal", "consumer"}
        or not isinstance(document["stage"], str)
        or _GATE_STAGE_TOKEN.fullmatch(document["stage"]) is None
        or f"{document['phase']}/{document['stage']}" not in _ALLOWED_GATE_STAGE_PAIRS
    ):
        raise GateError("technical-gate diagnostic stage is invalid")
    return f"{document['phase']}/{document['stage']}"


def verify_installed_module_path(
    *,
    module_path: Path,
    site_roots: Sequence[Path],
    checkout_root: Path,
) -> None:
    """Require an import to resolve from the fresh venv, never P or an overlay."""
    try:
        module = module_path.resolve(strict=True)
        checkout = checkout_root.resolve(strict=True)
    except OSError as exc:
        raise GateError("installed package path cannot be resolved") from exc
    if _is_within(module, checkout):
        raise GateError("Studio imported from the checkout")
    roots: list[Path] = []
    for root in site_roots:
        try:
            resolved_root = root.resolve(strict=True)
        except FileNotFoundError:
            # Ubuntu's system-Python venv reports optional dist-packages paths
            # which do not necessarily exist in the fresh environment.
            continue
        except OSError as exc:
            raise GateError("installed site-packages path cannot be resolved") from exc
        if not resolved_root.is_dir():
            raise GateError("installed site-packages path is not a directory")
        roots.append(resolved_root)
    if not roots or not any(_is_within(module, root) for root in roots):
        raise GateError("Studio did not import from installed site-packages")


def _validate_shm_prefix(shm_prefix: str) -> None:
    """Validate one per-run shared-memory namespace prefix."""
    safe_characters = (
        "-_.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    )
    if not shm_prefix or any(
        character not in safe_characters for character in shm_prefix
    ):
        raise GateError("technical-gate shared-memory prefix is unsafe")
    if sys.platform == "darwin" and len(shm_prefix.encode("ascii")) > 14:
        raise GateError(
            "technical-gate shared-memory prefix exceeds the Darwin 14-byte limit"
        )


def _new_shm_run_prefix() -> str:
    """Return a portable 13-byte run namespace for explicit POSIX SHM names."""
    return f"g{secrets.token_hex(6)}"


def _new_shm_probe_name(prefix: str) -> str:
    """Return a cleanup-probe name within the portable explicit-name budget."""
    name = f"{prefix}p{secrets.token_hex(8)}"
    if sys.platform == "darwin" and len(name.encode("ascii")) > 30:
        raise GateError("technical-gate cleanup probe name exceeds the Darwin limit")
    return name


def gate_environment(
    root: Path,
    inherited: Mapping[str, str],
    shm_prefix: str,
    *,
    native_qt: bool = False,
) -> dict[str, str]:
    """Build the isolated environment used by the external offscreen process."""
    _validate_shm_prefix(shm_prefix)
    environment = dict(inherited)
    for name in tuple(environment):
        if name in {
            "GWEXPY_STUDIO_IO_CAPABILITIES",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PYTHONPATH",
            *(("QT_QPA_PLATFORM",) if native_qt else ()),
        } or name.startswith("PIP_"):
            environment.pop(name, None)
    environment.update(
        {
            "HOME": str(root / "home"),
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(root / "mpl"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "GWEXPY_STUDIO_SHM_PREFIX": shm_prefix,
            "PYTHONNOUSERSITE": "1",
        }
    )
    if not native_qt:
        environment["QT_QPA_PLATFORM"] = "offscreen"
    return environment


def shared_memory_cleanup_probe(prefix: str) -> bool:
    """Prove portable unlink semantics by rejecting a post-unlink reattach."""
    _validate_shm_prefix(prefix)
    name = _new_shm_probe_name(prefix)
    block: shared_memory.SharedMemory | None = None
    try:
        block = shared_memory.SharedMemory(name=name, create=True, size=1)
        block.unlink()
        block.close()
        block = None
        try:
            unexpected = shared_memory.SharedMemory(name=name)
        except FileNotFoundError:
            return True
        unexpected.close()
        return False
    except (FileExistsError, OSError) as exc:
        raise GateError("technical-gate shared-memory probe failed") from exc
    finally:
        if block is not None:
            try:
                block.close()
                block.unlink()
            except FileNotFoundError:
                pass


def gate_result_json(
    checks: Mapping[str, object], *, installed: Mapping[str, object]
) -> bytes:
    """Serialize only named boolean outcomes, never host paths or exception text."""
    names = (
        _CHECK_NAMES
        if set(checks) == set(_CHECK_NAMES)
        else _LEGACY_CHECK_NAMES
        if set(checks) == set(_LEGACY_CHECK_NAMES)
        else ()
    )
    if not names or any(type(checks[name]) is not bool for name in names):
        raise GateError("technical-gate checks are invalid")
    identity = _gate_identity(installed)
    successful = all(checks[name] for name in names)
    schema = 3 if names == _CHECK_NAMES else 2
    return (
        json.dumps(
            {
                "architecture": identity["architecture"],
                "checks": {name: checks[name] for name in sorted(names)},
                "installed": {
                    name: identity[name]
                    for name in ("build_id", "source_sha", "version")
                },
                "python_version": identity["python_version"],
                "schema": schema,
                "status": "passed" if successful else "failed",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def read_gate_result(content: bytes) -> dict[str, object]:
    """Validate the bounded record consumed by resolution capture."""
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("technical-gate result is not valid JSON") from exc
    if type(document) is not dict or set(document) != {
        "architecture",
        "checks",
        "installed",
        "python_version",
        "schema",
        "status",
    }:
        raise GateError("technical-gate result schema is invalid")
    checks = document["checks"]
    schema = document["schema"]
    expected_names = (
        _LEGACY_CHECK_NAMES
        if schema == 2
        else _CHECK_NAMES
        if schema == 3
        else ()
    )
    if not isinstance(checks, dict) or not expected_names or set(checks) != set(
        expected_names
    ):
        raise GateError("technical-gate result checks are invalid")
    if any(type(checks[name]) is not bool for name in expected_names):
        raise GateError("technical-gate result checks are invalid")
    if schema not in {2, 3}:
        raise GateError("technical-gate result schema is unsupported")
    expected_status = "passed" if all(checks.values()) else "failed"
    if document["status"] != expected_status:
        raise GateError("technical-gate result status is inconsistent")
    return {
        "architecture": _gate_identity(document)["architecture"],
        "checks": {name: checks[name] for name in sorted(expected_names)},
        "installed": {
            name: _gate_identity(document)[name]
            for name in ("build_id", "source_sha", "version")
        },
        "python_version": _gate_identity(document)["python_version"],
        "schema": schema,
        "status": expected_status,
    }


def _gate_identity(value: Mapping[str, object]) -> dict[str, str]:
    """Validate path-free installed-wheel provenance captured by the gate."""
    installed = value.get("installed", value)
    if not isinstance(installed, Mapping):
        raise GateError("technical-gate installed identity is invalid")
    fields = {
        "architecture": value.get("architecture"),
        "python_version": value.get("python_version"),
        "build_id": installed.get("build_id"),
        "source_sha": installed.get("source_sha"),
        "version": installed.get("version"),
    }
    if any(not isinstance(item, str) for item in fields.values()):
        raise GateError("technical-gate installed identity is invalid")
    identity = {name: item for name, item in fields.items() if isinstance(item, str)}
    if (
        identity["architecture"] not in _MACHINES
        or _PYTHON_VERSION.fullmatch(identity["python_version"]) is None
        or _BUILD_ID.fullmatch(identity["build_id"]) is None
        or _SOURCE_SHA.fullmatch(identity["source_sha"]) is None
        or _TRIAL_VERSION.fullmatch(identity["version"]) is None
    ):
        raise GateError("technical-gate installed identity is invalid")
    return identity


def _installed_identity() -> dict[str, str]:
    """Read only the identity embedded in the installed package resources."""
    from importlib import resources

    try:
        document = json.loads(
            resources.files("gwexpy_studio")
            .joinpath("assets", "trial-build.json")
            .read_text(encoding="utf-8")
        )
    except (ModuleNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("installed trial identity is unavailable") from exc
    if not isinstance(document, dict):
        raise GateError("installed trial identity is invalid")
    return _gate_identity(
        {
            "architecture": platform.machine().lower(),
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "installed": document,
        }
    )


def technical_gate_command(
    *,
    python: Path,
    gate_fd: int,
    checkout_root: Path,
    work_root: Path,
    result_path: Path,
    replay_python: Path | None = None,
    native_qt: bool = False,
    legacy: bool = False,
) -> tuple[str, ...]:
    """Build an isolated invocation that reads only a retained sealed FD.

    Capture writes the source bytes from public commit P, verifies them, then
    unlinks the temporary name before passing this descriptor through every
    process boundary.  The interpreter bootstrap therefore cannot re-open a
    mutable checkout or workspace path after source verification.
    """
    gate_fd = _validated_gate_fd(gate_fd)
    command = (
        str(python),
        "-I",
        "-c",
        _SEALED_GATE_BOOTSTRAP,
        str(gate_fd),
        "--gate-fd",
        str(gate_fd),
        "--checkout",
        str(checkout_root),
        "--work-root",
        str(work_root),
        "--result",
        str(result_path),
    )
    if replay_python is not None:
        command += ("--replay-python", str(replay_python))
    if native_qt:
        command += ("--native-qt",)
    if legacy:
        command += ("--legacy-gate",)
    return command


def _phase_command(
    *,
    phase: str,
    python: Path,
    gate_fd: int,
    checkout: Path,
    work_root: Path,
    project: Path | None = None,
) -> tuple[str, ...]:
    """Build one fresh installed-wheel producer or consumer invocation."""
    if phase not in {"producer", "normal", "consumer"}:
        raise GateError("technical-gate phase is invalid")
    if (phase == "consumer") != (project is not None):
        raise GateError("technical-gate phase project is invalid")
    gate_fd = _validated_gate_fd(gate_fd)
    command = (
        str(python),
        "-I",
        "-c",
        _SEALED_GATE_BOOTSTRAP,
        str(gate_fd),
        "--phase",
        phase,
        "--gate-fd",
        str(gate_fd),
        "--checkout",
        str(checkout),
        "--work-root",
        str(work_root),
    )
    return command if project is None else (*command, "--project", str(project))


def _replay_reference_series(
    reference: Mapping[str, object], key: str
) -> Mapping[str, object]:
    value = reference.get(key)
    if not isinstance(value, Mapping):
        raise GateError("technical-gate export reference is invalid")
    required = (
        {"object_id", "kind", "values", "unit", "t0", "dt", "times"}
        if key == "time_series"
        else {"object_id", "kind", "values", "unit", "f0", "df", "frequencies"}
    )
    if set(value) != required:
        raise GateError("technical-gate export reference is invalid")
    return value


def _assert_replay_series(
    actual: Any,
    reference: Mapping[str, object],
    *,
    axis_name: str,
    origin_names: tuple[str, str],
    step_names: tuple[str, str],
) -> None:
    """Compare one exported native leaf against the independent oracle."""
    import numpy as np

    kind = reference["kind"]
    if type(actual).__name__ != kind:
        raise GateError("technical-gate export replay target kind mismatch")
    try:
        values = np.asarray(actual.value)
        expected_values = np.asarray(reference["values"])
    except (TypeError, ValueError) as exc:
        raise GateError("technical-gate export replay value mismatch") from exc
    if values.shape != expected_values.shape or not np.isfinite(values).all():
        raise GateError("technical-gate export replay value mismatch")
    scale = max(1.0, float(np.max(np.abs(expected_values))))
    try:
        np.testing.assert_allclose(
            values,
            expected_values,
            rtol=1e-12,
            atol=1e-12 * scale,
        )
    except AssertionError as exc:
        raise GateError("technical-gate export replay value mismatch") from exc
    if str(actual.unit) != reference["unit"]:
        raise GateError("technical-gate export replay unit mismatch")
    try:
        origin, expected_origin_name = origin_names
        first, second = step_names
        axis_unit = "s" if axis_name == "times" else "Hz"
        actual_origin = float(getattr(actual, origin).to_value(axis_unit))
        expected_origin = float(reference[expected_origin_name])
        actual_step = float(getattr(actual, first).to_value(axis_unit))
        expected_step = float(reference[second])
        actual_axis = np.asarray(getattr(actual, axis_name).to_value(axis_unit))
        expected_axis = np.asarray(reference[axis_name])
    except (AttributeError, TypeError, ValueError) as exc:
        raise GateError("technical-gate export replay axis mismatch") from exc
    if not np.isfinite(actual_axis).all() or actual_axis.shape != expected_axis.shape:
        raise GateError("technical-gate export replay axis mismatch")
    try:
        np.testing.assert_allclose(
            actual_axis,
            expected_axis,
            rtol=1e-12,
            atol=1e-12 * max(1.0, float(np.max(np.abs(expected_axis)))),
        )
        np.testing.assert_allclose(
            actual_origin, expected_origin, rtol=1e-12, atol=1e-12
        )
        np.testing.assert_allclose(actual_step, expected_step, rtol=1e-12, atol=1e-12)
    except AssertionError as exc:
        raise GateError("technical-gate export replay axis mismatch") from exc


def validate_export_replay_namespace(
    namespace: Mapping[str, object],
    reference: Mapping[str, object],
    *,
    studio_visible: bool = False,
) -> None:
    """Validate exact exported object variables and scientific metadata."""
    if studio_visible:
        raise GateError("Studio is visible in the export replay environment")
    for key, axis_name, origin_names, step_names in (
        ("time_series", "times", ("t0", "t0"), ("dt", "dt")),
        ("frequency_series", "frequencies", ("f0", "f0"), ("df", "df")),
    ):
        expected = _replay_reference_series(reference, key)
        object_id = expected["object_id"]
        if not isinstance(object_id, str) or not object_id:
            raise GateError("technical-gate export replay target is invalid")
        variable = object_id.replace("-", "_")
        if variable not in namespace:
            raise GateError("technical-gate export replay target variable is missing")
        _assert_replay_series(
            namespace[variable],
            expected,
            axis_name=axis_name,
            origin_names=origin_names,
            step_names=step_names,
        )


def _validate_replay_snapshot(
    observed: Mapping[str, object], reference: Mapping[str, object]
) -> None:
    """Validate path-free snapshots returned by the Studio-free child."""
    for key, axis_name, origin_names, step_names in (
        ("time_series", "times", ("t0", "t0"), ("dt", "dt")),
        ("frequency_series", "frequencies", ("f0", "f0"), ("df", "df")),
    ):
        expected = _replay_reference_series(reference, key)
        actual = observed.get(key)
        if not isinstance(actual, Mapping):
            raise GateError("technical-gate export replay target is missing")
        if actual.get("kind") != expected["kind"] or actual.get("unit") != expected[
            "unit"
        ]:
            raise GateError("technical-gate export replay unit mismatch")
        proxy = type(
            str(actual.get("kind")),
            (),
            {
                "__name__": str(actual.get("kind")),
                "value": actual.get("values"),
                "unit": actual.get("unit"),
                origin_names[0]: type(
                    "ReplayQuantity",
                    (),
                    {
                        "to_value": lambda self, _unit: actual[origin_names[1]]
                    },
                )(),
                step_names[0]: type(
                    "ReplayQuantity",
                    (),
                    {
                        "to_value": lambda self, _unit: actual[step_names[1]]
                    },
                )(),
                axis_name: type(
                    "ReplayQuantity",
                    (),
                    {"to_value": lambda self, _unit: actual[axis_name]},
                )(),
            },
        )()
        _assert_replay_series(
            proxy,
            expected,
            axis_name=axis_name,
            origin_names=origin_names,
            step_names=step_names,
        )


def _run_export_replay(
    *, replay_python: Path, checkout: Path, work_root: Path
) -> None:
    """Run the GUI-produced script in an exact third prefix without Studio."""
    exported = work_root / "python-export.py"
    reference_path = work_root / "export-reference.json"
    observed_path = work_root / "python-replay.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (exported, reference_path)
    ) or observed_path.exists() or observed_path.is_symlink():
        raise GateError("technical-gate export replay inputs are invalid")
    try:
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("technical-gate export reference is invalid") from exc
    if not isinstance(reference, Mapping):
        raise GateError("technical-gate export reference is invalid")
    replay_prefix = replay_python.resolve(strict=True).parent.parent
    if _is_within(replay_prefix, checkout.resolve(strict=True)):
        raise GateError("technical-gate export replay runs inside the checkout")
    child = (
        "import importlib.util as _importlib_util\n"
        "import json as _json\n"
        "import numpy as _np\n"
        "import runpy as _runpy\n"
        "import sys as _sys\n"
        "from pathlib import Path as _Path\n"
        "if _importlib_util.find_spec('gwexpy_studio') is not None:\n"
        "    raise RuntimeError('Studio is visible in the replay environment')\n"
        "_reference = _json.loads(_Path(_sys.argv[2]).read_text())\n"
        "_namespace = _runpy.run_path(_sys.argv[1], run_name='__main__')\n"
        "_observed = {}\n"
        "for _key, _axis, _origin, _step in ("
        "('time_series', 'times', 't0', 'dt'), "
        "('frequency_series', 'frequencies', 'f0', 'df')):\n"
        "    _record = _reference[_key]\n"
        "    _variable = _record['object_id'].replace('-', '_')\n"
        "    if _variable not in _namespace:\n"
        "        raise RuntimeError('export target variable is missing')\n"
        "    _value = _namespace[_variable]\n"
        "    _array = _np.asarray(_value.value)\n"
        "    if not _np.isfinite(_array).all():\n"
        "        raise RuntimeError('export value is not finite')\n"
        "    _unit = 's' if _axis == 'times' else 'Hz'\n"
        "    _origin_value = getattr(_value, _origin).to_value(_unit)\n"
        "    _step_value = getattr(_value, _step).to_value(_unit)\n"
        "    _axis_value = _np.asarray(\n"
        "        getattr(_value, _axis).to_value(_unit)\n"
        "    )\n"
        "    _observed[_key] = {'kind': type(_value).__name__, "
        "'values': _array.tolist(), 'unit': str(_value.unit), "
        "_origin: float(_origin_value), _step: float(_step_value), "
        "_axis: _axis_value.tolist()}\n"
        "_Path(_sys.argv[3]).write_text(_json.dumps(_observed, allow_nan=False, "
        "sort_keys=True, separators=(',', ':')), encoding='utf-8')\n"
    )
    try:
        completed = subprocess.run(
            (
                str(replay_python),
                "-I",
                "-c",
                child,
                str(exported),
                str(reference_path),
                str(observed_path),
            ),
            cwd=replay_prefix,
            capture_output=True,
            check=False,
            env=gate_environment(replay_prefix, os.environ, "gwexpy-replay-"),
            text=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError("technical-gate export replay could not run") from exc
    if completed.returncode != 0:
        raise GateError("technical-gate export replay rejected the replay environment")
    try:
        observed = json.loads(observed_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError("technical-gate export replay result is invalid") from exc
    if not isinstance(observed, Mapping):
        raise GateError("technical-gate export replay result is invalid")
    _validate_replay_snapshot(observed, reference)


def run_external_recovery_gate(
    *,
    checks: dict[str, bool],
    checkout: Path,
    gate_fd: int,
    phase_python: Path,
    work_root: Path,
    replay_python: Path | None = None,
    native_qt: bool = False,
    parent_shm_prefix: str | None = None,
    legacy: bool = False,
) -> None:
    """Require independent launcher sessions for the selected gate generation."""
    gate_fd = _validated_gate_fd(gate_fd)
    if parent_shm_prefix is None:
        parent_shm_prefix = os.environ.get("GWEXPY_STUDIO_SHM_PREFIX")
    if not isinstance(parent_shm_prefix, str):
        raise GateError("technical-gate parent shared-memory prefix is unavailable")
    _validate_shm_prefix(parent_shm_prefix)
    phase_prefixes = {
        "normal": f"{parent_shm_prefix}n",
        "recovery": f"{parent_shm_prefix}r",
    }
    for prefix in phase_prefixes.values():
        _validate_shm_prefix(prefix)
    project = work_root / "trial.gwxproj"
    phase_names = ("producer", "consumer") if legacy else (
        "normal",
        "producer",
        "consumer",
    )
    phases = tuple(
        (
            phase,
            _phase_command(
                phase=phase,
                python=phase_python,
                gate_fd=gate_fd,
                checkout=checkout,
                work_root=work_root,
                project=project if phase == "consumer" else None,
            ),
            -signal.SIGKILL if phase == "producer" else 0,
        )
        for phase in phase_names
    )
    for phase, command, expected_returncode in phases:
        state_root = work_root / (
            ".normal-state" if phase == "normal" else ".recovery-state"
        )
        if phase == "consumer":
            if state_root.is_symlink() or not state_root.is_dir():
                raise GateError("technical-gate recovery state root is unavailable")
        else:
            if state_root.exists() or state_root.is_symlink():
                raise GateError("technical-gate phase state root is not fresh")
            state_root.mkdir()
        for name in ("cache", "config", "data", "state", "home", "mpl"):
            private_root = state_root / name
            if private_root.is_symlink() or (
                private_root.exists() and not private_root.is_dir()
            ):
                raise GateError("technical-gate private state root is unsafe")
            private_root.mkdir(exist_ok=True)
        try:
            completed = subprocess.run(
                command,
                cwd=work_root,
                capture_output=True,
                check=False,
                env=gate_environment(
                    state_root,
                    os.environ,
                    phase_prefixes["normal" if phase == "normal" else "recovery"],
                    native_qt=native_qt,
                ),
                pass_fds=(gate_fd,),
                text=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GateError(f"technical-gate {phase} phase could not run") from exc
        if completed.returncode != expected_returncode:
            raise GateError(f"technical-gate {phase} phase failed")
        if phase == "normal":
            checks["normal_close_reopen"] = True
            checks["io_read"] = True
            checks["io_refusal"] = True
        elif phase == "producer":
            if project.is_symlink() or not project.is_file():
                raise GateError("technical-gate producer did not save a project")
            exported = work_root / "python-export.py"
            if exported.is_symlink() or not exported.is_file():
                raise GateError("technical-gate producer did not export Python")
            if replay_python is not None:
                _run_export_replay(
                    replay_python=replay_python,
                    checkout=checkout,
                    work_root=work_root,
                )
                checks["export_numeric_replay"] = True
            for name in (
                "launcher_import",
                "welcome",
                "try_sample",
                "crop",
                "asd",
                "save_project",
            ):
                checks[name] = True
        else:
            for name in ("project_reopen", "recovery", "worker_exit"):
                checks[name] = True
    if sys.platform == "linux":
        for phase, prefix in phase_prefixes.items():
            if any(Path("/dev/shm").glob(f"{prefix}*")):
                raise GateError(
                    f"technical-gate {phase} shared memory was not cleaned up"
                )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_gate_fd(value: object) -> int:
    """Reject descriptors that could name standard input/output/error streams."""
    if type(value) is not int or value < 3:
        raise GateError("technical-gate descriptor is invalid")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--phase", choices=("normal", "producer", "consumer"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--gate-fd", type=int)
    parser.add_argument("--replay-python", type=Path)
    parser.add_argument("--native-qt", action="store_true")
    parser.add_argument("--legacy-gate", action="store_true")
    return parser


def _run_phase(arguments: argparse.Namespace) -> int:
    """Run one child-only launcher phase without writing the final gate record."""
    try:
        checkout = arguments.checkout.resolve(strict=True)
        work_root = arguments.work_root.resolve(strict=True)
        if not checkout.is_dir() or not work_root.is_dir():
            raise GateError("technical-gate phase paths are invalid")
        if arguments.phase == "normal":
            if arguments.project is not None:
                raise GateError("technical-gate normal phase cannot accept a project")
            _run_normal_launcher(checkout=checkout, work_root=work_root)
        elif arguments.phase == "producer":
            if arguments.project is not None:
                raise GateError("technical-gate producer cannot accept a project")
            _run_producer_launcher(checkout=checkout, work_root=work_root)
        elif arguments.phase == "consumer":
            if arguments.project is None:
                raise GateError("technical-gate consumer requires a project")
            _run_consumer_launcher(
                checkout=checkout,
                project=arguments.project.resolve(strict=True),
                work_root=work_root,
            )
        else:  # pragma: no cover - argparse constrains this internal value.
            raise GateError("technical-gate phase is invalid")
    except (OSError, GateError):
        return 1
    return 0


def _wait(app: object, predicate: object, label: str, timeout_s: float = 30.0) -> None:
    """Process Qt events until one externally observable gate condition holds."""
    from PySide6.QtCore import QEventLoop, QTimer

    if predicate():  # type: ignore[operator]
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    timed_out = [False]

    def check() -> None:
        if predicate():  # type: ignore[operator]
            loop.quit()

    def expire() -> None:
        timed_out[0] = True
        loop.quit()

    poll.timeout.connect(check)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(expire)
    poll.start()
    deadline.start(int(timeout_s * 1000))
    loop.exec()
    poll.stop()
    deadline.stop()
    app.processEvents()  # type: ignore[attr-defined]
    if timed_out[0] or not predicate():  # type: ignore[operator]
        raise GateError(f"technical-gate timed out during {label}")


def _normal_io_read_ready(
    *, window: Any, idle_state: Any, previews: Sequence[Any]
) -> bool:
    """Require the preview callback after the worker reports a resident object."""
    return (
        window.bridge.state is idle_state
        and len(window.project.objects) == 1
        and bool(window._workspace_status.get("resident_object_ids"))
        and bool(previews)
    )


def _record_preview_after_display(
    *,
    previews: list[Any],
    display: Callable[[Any, Any], None],
    preview: Any,
    spec: Any,
) -> None:
    """Count a preview only after the real canvas accepts it."""
    display(preview, spec)
    previews.append(preview)


def _normal_reopen_ready(
    *, window: Any, idle_state: Any, project_path: str
) -> bool:
    """Wait for action state to settle before triggering Review / Restore."""
    return (
        window.bridge.state is idle_state
        and len(window.project.objects) == 1
        and window._workspace_status.get("project_path") == project_path
        and window._workspace_status.get("needs_restore") is True
        and window._workspace_status.get("resident_object_ids") == []
        and not window._command_reserved
        and not window._modal_active
        and window.restore_project_action.isEnabled()
    )


def _wait_for_sample_catalog(
    *, app: Any, window: Any, panel: Any, idle_state: object
) -> None:
    """Wait until the asynchronous Try Sample catalog enables inspection."""
    _wait(
        app,
        lambda: window.bridge.state is idle_state and panel.inspect_button.isEnabled(),
        "sample catalog",
    )


def _select_latest_timeseries_for_crop(
    *, app: Any, window: Any, idle_state: object
) -> str:
    """Select the latest TimeSeries through Sources before the recovery Crop."""
    object_id = next(
        (
            candidate.object_id
            for candidate in reversed(window.project.objects)
            if candidate.kind == "TimeSeries"
        ),
        None,
    )
    if not isinstance(object_id, str) or not object_id:
        raise GateError("technical-gate has no TimeSeries input for post-ASD Crop")
    item = window._find_object_item(window.source_tree, object_id)
    if item is None:
        raise GateError("technical-gate cannot select post-ASD Crop input")
    window.source_tree.setCurrentItem(item)
    _wait(
        app,
        lambda: (
            window.bridge.state is idle_state
            and window._current_object_id == object_id
            and window.crop_action.isEnabled()
        ),
        "post-ASD Crop input",
    )
    return object_id


def _click_dialog(
    dialog_type: type[Any],
    fill: Callable[[Any], None],
    action: Any,
    *,
    timeout_s: float = 15.0,
) -> None:
    """Accept one bounded modal operation dialog through its public controls."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialogButtonBox

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        raise GateError("technical-gate operation dialog has no application")
    poll_timer = QTimer()
    poll_timer.setInterval(10)
    deadline_timer = QTimer()
    deadline_timer.setSingleShot(True)
    handled = [False]
    failure: list[BaseException] = []

    def current_dialog() -> Any | None:
        active = app.activeModalWidget()
        if isinstance(active, dialog_type):
            return active
        candidates = [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, dialog_type) and widget.isVisible()
        ]
        return candidates[0] if len(candidates) == 1 else None

    def stop() -> None:
        poll_timer.stop()
        deadline_timer.stop()

    def fail(error: BaseException) -> None:
        if not failure:
            failure.append(error)
        stop()
        dialog = current_dialog()
        if dialog is not None:
            dialog.reject()

    def accept() -> None:
        if handled[0] or failure:
            return
        dialog = current_dialog()
        if dialog is None:
            return
        try:
            fill(dialog)
            buttons = dialog.findChild(QDialogButtonBox)
            if buttons is None:
                raise GateError("technical-gate operation dialog has no buttons")
            button = buttons.button(QDialogButtonBox.StandardButton.Ok)
            if button is None:
                raise GateError("technical-gate operation dialog cannot be accepted")
            handled[0] = True
            stop()
            # A synthetic mouse event may be ignored by Cocoa while a nested
            # native modal loop is active.  The public button signal is the
            # deterministic cross-platform acceptance boundary.
            button.click()
        except BaseException as exc:
            fail(exc)

    def expire() -> None:
        fail(GateError("technical-gate operation dialog timed out"))

    poll_timer.timeout.connect(accept)
    deadline_timer.timeout.connect(expire)
    poll_timer.start()
    deadline_timer.start(max(1, int(timeout_s * 1000)))
    try:
        action.trigger()
    finally:
        stop()
    if failure:
        raise GateError("technical-gate operation dialog failed") from failure[0]
    if not handled[0]:
        raise GateError("technical-gate operation dialog did not appear")


def _remove_xdg_roots(work_root: Path) -> None:
    """Remove only the private XDG roots created by this gate process."""
    for name in (
        "cache",
        "config",
        "data",
        "state",
        "home",
        "mpl",
        ".normal-state",
        ".recovery-state",
    ):
        candidate = work_root / name
        if candidate.is_symlink():
            raise GateError("technical-gate XDG root is unsafe")
        shutil.rmtree(candidate, ignore_errors=False) if candidate.exists() else None


def _project_contract(project: Any) -> dict[str, object]:
    """Capture only persisted project identity needed for Close/Open proof."""
    objects = [
        {"object_id": item.object_id, "kind": item.kind}
        for item in project.objects
    ]
    operations = [
        {
            "op_id": operation.op_id,
            "operation_id": operation.operation_id,
            "operation_schema": operation.operation_schema,
            "inputs": dict(operation.inputs),
            "params": dict(operation.params),
            "outputs": list(operation.outputs),
        }
        for operation in project.graph.operations
    ]
    return {
        "project_id": project.project_id,
        "objects": objects,
        "operations": operations,
    }


def _write_export_reference(*, window: Any, source: Path, work_root: Path) -> None:
    """Build an independent GWexpy oracle for the two exported leaf objects."""
    import gwexpy
    import numpy as np

    gwexpy.register_all(include_io=True)
    from gwexpy.timeseries import TimeSeries

    raw = TimeSeries.read(str(source), format="csv", skiprows=1)
    cropped = raw.crop(0.125, 0.875)
    frequency = cropped.asd(fftlength=0.125, overlap=0.0625)
    time_leaf = cropped.crop(0.25, 0.75)
    time_id = next(
        (
            item.object_id
            for item in reversed(window.project.objects)
            if item.kind == "TimeSeries"
        ),
        None,
    )
    frequency_id = next(
        (
            item.object_id
            for item in reversed(window.project.objects)
            if item.kind == "FrequencySeries"
        ),
        None,
    )
    if not isinstance(time_id, str) or not isinstance(frequency_id, str):
        raise GateError("technical-gate export reference has no required leaves")

    def snapshot(
        object_id: str, value: Any, *, axis: str, origin: str, step: str, unit: str
    ) -> dict[str, object]:
        values = np.asarray(value.value)
        coordinates = np.asarray(getattr(value, axis).to_value(unit))
        if not np.isfinite(values).all() or not np.isfinite(coordinates).all():
            raise GateError("technical-gate export reference is not finite")
        return {
            "object_id": object_id,
            "kind": type(value).__name__,
            "values": values.tolist(),
            "unit": str(value.unit),
            origin: float(getattr(value, origin).to_value(unit)),
            step: float(getattr(value, step).to_value(unit)),
            axis: coordinates.tolist(),
        }

    reference = {
        "schema": 1,
        "time_series": snapshot(
            time_id,
            time_leaf,
            axis="times",
            origin="t0",
            step="dt",
            unit="s",
        ),
        "frequency_series": snapshot(
            frequency_id,
            frequency,
            axis="frequencies",
            origin="f0",
            step="df",
            unit="Hz",
        ),
    }
    target = work_root / "export-reference.json"
    if target.exists() or target.is_symlink():
        raise GateError("technical-gate export reference path is not fresh")
    target.write_text(
        json.dumps(reference, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _run_normal_launcher(*, checkout: Path, work_root: Path) -> None:
    """Exercise explicit CSV I/O and an independent Save/Close/Open lifecycle."""
    _record_gate_stage(work_root, "normal", "bootstrap")
    import site

    import numpy as np
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import gwexpy_studio
    from gwexpy_studio.runtime.trial import sample_path
    from gwexpy_studio.ui import app as launcher
    from gwexpy_studio.ui.bridge import BridgeState
    from gwexpy_studio.ui.io_panel import DataIOPanel
    from gwexpy_studio.ui.window import MainWindow

    verify_installed_module_path(
        module_path=Path(gwexpy_studio.__file__ or ""),
        site_roots=tuple(Path(path) for path in site.getsitepackages()),
        checkout_root=checkout,
    )
    _installed_identity()
    failure: list[BaseException] = []

    def drive() -> None:
        nonlocal failure
        try:
            _record_gate_stage(work_root, "normal", "launcher")
            app = QApplication.instance()
            if not isinstance(app, QApplication):
                raise GateError("normal launcher did not create QApplication")
            windows = [
                item for item in app.topLevelWidgets() if isinstance(item, MainWindow)
            ]
            if len(windows) != 1:
                raise GateError("normal launcher did not create one main window")
            window = windows[0]
            _record_gate_stage(work_root, "normal", "welcome")
            _wait(
                app,
                lambda: window.isVisible() and window.welcome_panel.isVisible(),
                "normal welcome",
            )
            _record_gate_stage(work_root, "normal", "worker-ready")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and window._io_capability_ready is True
                    and not window._command_reserved
                    and window.open_action.isEnabled()
                ),
                "normal worker readiness",
            )
            window.open_action.trigger()
            _wait(app, lambda: window.open_data_panel is not None, "normal I/O panel")
            panel = window.open_data_panel
            if not isinstance(panel, DataIOPanel):
                raise GateError("normal I/O action did not open a data panel")
            catalogs: list[dict[str, object]] = []
            original_set_catalog = panel.set_catalog

            def capture_catalog(catalog: dict[str, object]) -> None:
                catalogs.append(dict(catalog))
                original_set_catalog(catalog)

            panel.set_catalog = capture_catalog  # type: ignore[method-assign]
            source = sample_path().resolve(strict=True)
            panel.paths_edit.setPlainText(str(source))
            panel.format_combo.setEditText("csv")
            panel.kwargs_edit.setPlainText('{"skiprows": 1}')
            _record_gate_stage(work_root, "normal", "io-catalog")
            _wait_for_sample_catalog(
                app=app,
                window=window,
                panel=panel,
                idle_state=BridgeState.IDLE,
            )
            _record_gate_stage(work_root, "normal", "io-catalog-ready")
            if not catalogs:
                raise GateError("normal CSV catalog response was not captured")
            catalog = catalogs[-1]
            entry = next(
                (
                    item
                    for item in catalog.get("formats", [])
                    if isinstance(item, dict) and item.get("format") == "csv"
                ),
                None,
            )
            read_capability = (
                entry.get("capabilities", {}).get("read")
                if isinstance(entry, dict)
                and isinstance(entry.get("capabilities"), dict)
                else None
            )
            if not (
                isinstance(entry, dict)
                and entry.get("read") is True
                and isinstance(read_capability, dict)
                and read_capability.get("status") == "verified"
                and read_capability.get("available") is True
            ):
                raise GateError(
                    "normal CSV read capability is not verified: "
                    f"entry={entry!r}"
                )
            _record_gate_stage(work_root, "normal", "io-inspection")
            QTest.mouseClick(panel.inspect_button, Qt.MouseButton.LeftButton)
            _wait(
                app,
                lambda: panel.confirm_button.isEnabled(),
                "normal I/O inspection",
            )
            previews: list[Any] = []
            original_set_preview = window.plot_canvas.set_preview

            def capture_preview(preview: Any, spec: Any) -> None:
                _record_preview_after_display(
                    previews=previews,
                    display=original_set_preview,
                    preview=preview,
                    spec=spec,
                )

            window.plot_canvas.set_preview = capture_preview  # type: ignore[method-assign]
            _record_gate_stage(work_root, "normal", "io-read")
            QTest.mouseClick(panel.confirm_button, Qt.MouseButton.LeftButton)
            _wait(
                app,
                lambda: _normal_io_read_ready(
                    window=window,
                    idle_state=BridgeState.IDLE,
                    previews=previews,
                ),
                "normal I/O read",
            )
            _record_gate_stage(work_root, "normal", "io-read-settled")
            object_id = window.project.objects[0].object_id
            if not previews:
                raise GateError("normal CSV read preview was not captured")
            actual = previews[-1]
            from gwexpy.timeseries import TimeSeries

            oracle = TimeSeries.read(str(source), format="csv", skiprows=1)
            values = np.asarray(actual.values)
            expected = np.asarray(oracle.value)
            if values.shape != expected.shape:
                raise GateError("normal CSV read value shape mismatch")
            if not np.isfinite(values).all() or not np.isfinite(expected).all():
                raise GateError("normal CSV read values are not finite")
            np.testing.assert_allclose(
                values,
                expected,
                rtol=1e-12,
                atol=1e-12 * max(1.0, float(np.max(np.abs(expected)))),
            )
            if actual.unit != str(oracle.unit):
                raise GateError("normal CSV read unit mismatch")
            axes = actual.ref.axes
            if (
                float(axes["t0"]["value"]) != float(oracle.t0.to_value("s"))
                or float(axes["dt"]["value"]) != float(oracle.dt.to_value("s"))
            ):
                raise GateError("normal CSV read axis mismatch")
            np.testing.assert_allclose(
                np.asarray(oracle.times.to_value("s")),
                float(axes["t0"]["value"])
                + np.arange(values.size) * float(axes["dt"]["value"]),
                rtol=1e-12,
                atol=1e-12,
            )

            saved_contract = _project_contract(window.project)
            project = work_root / "normal-project.gwxproj"
            _record_gate_stage(work_root, "normal", "save-project")
            if project.exists() or not window.save_project_to(str(project)):
                raise GateError("normal Save Project could not start")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and project.is_file()
                    and window._workspace_status.get("project_path")
                    == str(project.resolve())
                    and not window._workspace_status.get("dirty")
                    and not window._command_reserved
                ),
                "normal save",
            )
            _record_gate_stage(work_root, "normal", "close-project")
            window.close_project_action.trigger()
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and not window.project.objects
                    and window._workspace_status.get("project_path") is None
                    and window.welcome_panel.isVisible()
                ),
                "normal close",
            )
            _record_gate_stage(work_root, "normal", "empty-workspace")
            if not window._request_document_change(
                "open_project", {"path": str(project.resolve())}
            ):
                raise GateError("normal Open Project could not start")
            _record_gate_stage(work_root, "normal", "open-project")
            _wait(
                app,
                lambda: _normal_reopen_ready(
                    window=window,
                    idle_state=BridgeState.IDLE,
                    project_path=str(project.resolve()),
                ),
                "normal reopen",
            )
            if _project_contract(window.project) != saved_contract:
                raise GateError("normal Close/Open project contract changed")
            _record_gate_stage(work_root, "normal", "reopened-project")

            message_binding = _install_workspace_message_binding()
            restore_boundaries = _instrument_recovery_boundaries(
                window=window,
                record=lambda _stage: None,
                message_binding=message_binding,
            )
            review_message = _schedule_review_confirmation(
                app=app,
                owner=window,
                required=True,
                binding=message_binding,
            )
            _record_gate_stage(work_root, "normal", "restore-review")
            window.restore_project_action.trigger()
            _wait(
                app,
                lambda: review_message.handled or review_message.error is not None,
                "normal restore review",
                timeout_s=_REVIEW_PRE_DIALOG_TIMEOUT_S,
            )
            review_message.raise_if_failed()
            _record_gate_stage(work_root, "normal", "restore-review-handled")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and window._workspace_status.get("needs_restore") is False
                    and object_id
                    in window._workspace_status.get("resident_object_ids", [])
                ),
                "normal restore",
            )
            _record_gate_stage(work_root, "normal", "restored-state-settled")
            restore_boundaries()
            message_binding.restore()
            _record_gate_stage(work_root, "normal", "restored-project")

            # Keep the UI-level unavailable display separate from the direct
            # worker refusal.  The policy must block both paths.
            window.export_data_action.trigger()
            _wait(
                app,
                lambda: window.export_data_panel is not None,
                "normal unavailable I/O panel",
            )
            export_panel = window.export_data_panel
            if not isinstance(export_panel, DataIOPanel):
                raise GateError("normal export action did not open a data panel")
            _wait(
                app,
                lambda: (
                    export_panel.format_combo.findData("csv") >= 0
                    and not export_panel._format_capabilities["csv"].available
                ),
                "normal unavailable catalog",
            )
            index = export_panel.format_combo.findData("csv")
            if index < 0 or export_panel.format_combo.itemData(index) != "csv":
                raise GateError("normal unavailable CSV item is not explicit")
            export_panel.format_combo.setCurrentIndex(index)
            item_text = export_panel.format_combo.itemText(index)
            if "Unavailable" not in item_text:
                raise GateError("normal unavailable CSV item is not visibly labeled")
            if (
                not export_panel.capability_label.isVisible()
                or "Unavailable" not in export_panel.capability_label.text()
            ):
                raise GateError("normal unavailable CSV status is not visible")
            if export_panel.format_combo.model().item(index).isEnabled():
                raise GateError("normal unavailable CSV item is enabled")
            target = work_root / "unavailable.csv"
            if target.exists() or target.is_symlink():
                raise GateError("normal unavailable CSV target is not fresh")
            export_panel.paths_edit.setPlainText(str(target))
            inspected: list[object] = []
            confirmed: list[object] = []
            export_panel.inspect_requested.connect(inspected.append)
            export_panel.write_requested.connect(confirmed.append)
            QTest.mouseClick(export_panel.inspect_button, Qt.MouseButton.LeftButton)
            if inspected or confirmed or export_panel.confirm_button.isEnabled():
                raise GateError("normal unavailable UI dispatched a write")
            if "[io_capability_unavailable]" not in export_panel.error_label.text():
                raise GateError("normal unavailable UI did not show the root error")
            _record_gate_stage(work_root, "normal", "io-unavailable")

            before_project = _project_contract(window.project)
            before_sources = tuple(window.project.sources)
            before_source_bindings = dict(window.project.source_bindings)
            before_activities = tuple(window.project.activities)
            if not window._dispatch_command(
                "write_data",
                {
                    "object_id": object_id,
                    "request": {
                        "datatype": "TimeSeries",
                        "format": "csv",
                        "paths": [str(target)],
                        "args": [],
                        "kwargs": {},
                        "combine": "individual",
                        "max_bytes": 512 * 1024 * 1024,
                        "max_entries": 10000,
                    },
                },
                pending_action="signal_write_data",
            ):
                raise GateError("normal worker refusal could not be dispatched")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and not window._command_reserved
                ),
                "normal worker refusal",
            )
            if window._last_error_code != "data_write_failed":
                raise GateError("normal worker refusal returned the wrong wrapper code")
            after_activities = tuple(window.project.activities)
            if len(after_activities) != len(before_activities) + 1:
                raise GateError(
                    "normal worker refusal did not append one failure activity"
                )
            failure_activity = after_activities[len(before_activities)]
            invalid_activity_fields: list[str] = []
            if failure_activity.action != "write_data":
                invalid_activity_fields.append("action")
            if failure_activity.status != "failed":
                invalid_activity_fields.append("status")
            if failure_activity.object_id != object_id:
                invalid_activity_fields.append("object")
            if failure_activity.target != str(target.absolute()):
                invalid_activity_fields.append("target")
            if not isinstance(failure_activity.error, dict):
                invalid_activity_fields.append("error-shape")
            elif failure_activity.error.get("code") != "io_capability_unavailable":
                invalid_activity_fields.append("root-code")
            if invalid_activity_fields:
                raise GateError(
                    "normal worker refusal failure activity invalid fields: "
                    + ",".join(invalid_activity_fields)
                )
            if (
                target.exists()
                or target.is_symlink()
                or _project_contract(window.project) != before_project
                or tuple(window.project.sources) != before_sources
                or dict(window.project.source_bindings) != before_source_bindings
            ):
                raise GateError("normal unavailable CSV write mutated project state")
            _record_gate_stage(work_root, "normal", "io-refusal")
            _record_gate_stage(work_root, "normal", "save-after-refusal")
            if not window.save_project_to(str(project.resolve())):
                raise GateError("normal refusal audit could not be saved")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and project.is_file()
                    and window._workspace_status.get("project_path")
                    == str(project.resolve())
                    and not window._workspace_status.get("dirty")
                    and not window._command_reserved
                    and not window._checkpoint_pending
                    and not window.plot_canvas._view_timer.isActive()
                    and window._queued_plot_spec is None
                    and window._after_view_save is None
                ),
                "normal refusal save",
            )
            _record_gate_stage(work_root, "normal", "worker-exit")
            window.close()
            _wait(
                app,
                lambda: not window.isVisible()
                and not window.bridge.worker_thread.isRunning(),
                "normal worker exit",
            )
            _record_gate_stage(work_root, "normal", "complete")
        except BaseException as exc:
            failure.append(exc)
            QApplication.quit()

    app = QApplication.instance()
    if app is None:
        QApplication(_qapplication_arguments())
    elif not isinstance(app, QApplication):
        raise GateError("normal launcher cannot use the existing Qt application")
    QTimer.singleShot(0, drive)
    status = launcher.main(())
    if status != 0:
        raise GateError("normal launcher did not exit cleanly")
    if failure:
        raise GateError("normal Save/Close/Open or I/O workflow failed") from failure[0]


def _run_producer_launcher(*, checkout: Path, work_root: Path) -> None:
    """Create a newer recovery checkpoint, then intentionally kill its launcher."""
    _record_gate_stage(work_root, "producer", "bootstrap")
    import site

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import gwexpy_studio
    from gwexpy_studio.ui import app as launcher
    from gwexpy_studio.ui.bridge import BridgeState
    from gwexpy_studio.ui.dialogs import AsdDialog, CropDialog
    from gwexpy_studio.ui.io_panel import DataIOPanel
    from gwexpy_studio.ui.window import MainWindow

    verify_installed_module_path(
        module_path=Path(gwexpy_studio.__file__ or ""),
        site_roots=tuple(Path(path) for path in site.getsitepackages()),
        checkout_root=checkout,
    )
    _installed_identity()

    failure: list[BaseException] = []

    def drive() -> None:
        try:
            _record_gate_stage(work_root, "producer", "launcher")
            app = QApplication.instance()
            if not isinstance(app, QApplication):
                raise GateError("normal launcher did not create QApplication")
            windows = [
                item for item in app.topLevelWidgets() if isinstance(item, MainWindow)
            ]
            if len(windows) != 1:
                raise GateError("normal launcher did not create one main window")
            window = windows[0]
            _record_gate_stage(work_root, "producer", "welcome")
            _wait(
                app,
                lambda: window.isVisible() and window.welcome_panel.isVisible(),
                "welcome",
            )
            _record_gate_stage(work_root, "producer", "sample-availability")
            _wait(
                app,
                lambda: window.welcome_panel.try_sample_button.isEnabled(),
                "sample availability",
            )
            _record_gate_stage(work_root, "producer", "try-sample")
            QTest.mouseClick(
                window.welcome_panel.try_sample_button, Qt.MouseButton.LeftButton
            )
            _wait(app, lambda: window.open_data_panel is not None, "Try Sample")
            panel = window.open_data_panel
            if not isinstance(panel, DataIOPanel):
                raise GateError("Try Sample did not open data panel")
            _record_gate_stage(work_root, "producer", "sample-catalog")
            _wait_for_sample_catalog(
                app=app,
                window=window,
                panel=panel,
                idle_state=BridgeState.IDLE,
            )
            _record_gate_stage(work_root, "producer", "sample-inspection")
            QTest.mouseClick(panel.inspect_button, Qt.MouseButton.LeftButton)
            _wait(app, lambda: panel.confirm_button.isEnabled(), "sample inspection")
            QTest.mouseClick(panel.confirm_button, Qt.MouseButton.LeftButton)
            _record_gate_stage(work_root, "producer", "sample-read")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 1
                ),
                "sample read",
            )

            def crop(dialog: CropDialog) -> None:
                dialog.start_edit.setText("0.125")
                dialog.end_edit.setText("0.875")

            _record_gate_stage(work_root, "producer", "crop-dialog")
            _click_dialog(CropDialog, crop, window.crop_action)
            _record_gate_stage(work_root, "producer", "crop")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 2
                ),
                "crop",
            )

            def asd(dialog: AsdDialog) -> None:
                dialog.fftlength_edit.setText("0.125")
                dialog.overlap_edit.setText("0.0625")

            _record_gate_stage(work_root, "producer", "asd-dialog")
            _click_dialog(AsdDialog, asd, window.asd_action)
            _record_gate_stage(work_root, "producer", "asd")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 3
                ),
                "ASD",
            )
            project = work_root / "trial.gwxproj"
            _record_gate_stage(work_root, "producer", "save-project")
            if project.exists() or not window.save_project_to(str(project)):
                raise GateError("Save Project could not start")
            _wait(
                app,
                lambda: window.bridge.state is BridgeState.IDLE and project.is_file(),
                "Save Project",
            )
            checkpoint_before = window._workspace_status.get("last_checkpoint_at")
            revision_before = window._workspace_status.get("revision")

            def post_save_crop(dialog: CropDialog) -> None:
                dialog.start_edit.setText("0.25")
                dialog.end_edit.setText("0.75")

            _record_gate_stage(work_root, "producer", "post-save-input")
            _select_latest_timeseries_for_crop(
                app=app,
                window=window,
                idle_state=BridgeState.IDLE,
            )
            _record_gate_stage(work_root, "producer", "post-save-crop-dialog")
            _click_dialog(CropDialog, post_save_crop, window.crop_action)
            _record_gate_stage(work_root, "producer", "recovery-checkpoint")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 4
                    and window._workspace_status.get("revision") != revision_before
                    and window._workspace_status.get("last_checkpoint_at")
                    not in {None, checkpoint_before}
                    and window._workspace_status.get("recovery_error") is None
                ),
                "post-save recovery checkpoint",
            )
            if not project.is_file():
                raise GateError("technical-gate producer did not save a project")
            exported = work_root / "python-export.py"
            if exported.exists():
                raise GateError("Python export path is not fresh")
            _record_gate_stage(work_root, "producer", "python-export")
            window.export_to_file(str(exported))
            _wait(
                app,
                lambda: window.bridge.state is BridgeState.IDLE and exported.is_file(),
                "Python export",
            )
            source = next(
                (
                    Path(item.uri).resolve(strict=True)
                    for item in window.project.sources
                    if isinstance(item.uri, str)
                ),
                None,
            )
            if source is None:
                raise GateError(
                    "technical-gate producer has no source for export oracle"
                )
            _record_gate_stage(work_root, "producer", "export-reference")
            _write_export_reference(
                window=window,
                source=source,
                work_root=work_root,
            )
            _record_gate_stage(work_root, "producer", "intentional-crash")
            os.kill(os.getpid(), signal.SIGKILL)
        except BaseException as exc:
            failure.append(exc)
            QApplication.quit()

    app = QApplication.instance()
    if app is None:
        QApplication(_qapplication_arguments())
    elif not isinstance(app, QApplication):
        raise GateError("normal launcher cannot use the existing Qt application")
    QTimer.singleShot(0, drive)
    if launcher.main(()) != 0:
        raise GateError("normal launcher did not exit cleanly")
    if failure:
        raise GateError("technical-gate UI workflow failed") from failure[0]
    raise GateError("technical-gate producer exited before its intentional crash")


def _schedule_message_box_button(
    *,
    app: Any,
    owner: Any,
    label: str,
    title: str,
    required: bool,
    timeout_s: float = 15.0,
    binding: _WorkspaceMessageBinding | None = None,
    message_binding: _WorkspaceMessageBinding | None = None,
    recovery: bool = False,
    on_visible: Callable[[], None] | None = None,
    on_selected: Callable[[], None] | None = None,
    on_diagnostic_poll: Callable[[], None] | None = None,
    on_diagnostic_button: Callable[[], None] | None = None,
    review: bool = False,
    pre_dialog_timeout_s: float = _REVIEW_PRE_DIALOG_TIMEOUT_S,
) -> _ScheduledMessageClick:
    """Click a title-bound message button without racing another modal.

    ``QTest.mouseClick`` is appropriate for ordinary visible controls, but an
    offscreen nested ``QMessageBox.exec()`` requires the native button signal
    to close reliably.  The timers are owned by the main window and bounded so
    a missing or malformed dialog cannot poll forever.
    """
    if review:
        if binding is not None and message_binding is not None:
            if binding is not message_binding:
                raise GateError("technical-gate message binding is ambiguous")
        review_binding = message_binding or binding
        if review_binding is None:
            raise GateError("technical-gate review dialog binding failed")
        return _schedule_review_confirmation(
            app=app,
            owner=owner,
            required=required,
            timeout_s=timeout_s,
            pre_dialog_timeout_s=pre_dialog_timeout_s,
            binding=review_binding,
            on_visible=on_visible,
            on_selected=on_selected,
        )
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    if (
        binding is not None
        and message_binding is not None
        and binding is not message_binding
    ):
        raise GateError("technical-gate message binding is ambiguous")
    binding = message_binding or binding
    poll_timer = QTimer(owner)
    poll_timer.setInterval(10)
    deadline_timer = QTimer(owner)
    deadline_timer.setSingleShot(True)
    state = _ScheduledMessageClick(
        poll_timer=poll_timer,
        deadline_timer=deadline_timer,
    )
    state._on_visible = on_visible
    state._on_selected = on_selected
    state._diagnostic_poll_callback = on_diagnostic_poll
    state._diagnostic_button_callback = on_diagnostic_button
    if binding is not None:
        if recovery:
            binding.register_recovery(state)
        else:
            binding.register(title, state)

    def current_message() -> Any | None:
        if state.bound_message is not None:
            return state.bound_message
        if recovery:
            return None
        active = app.activeModalWidget()
        if isinstance(active, QMessageBox) and active.windowTitle() == title:
            return active
        if active is not None:
            return None
        candidates = [
            widget
            for widget in app.topLevelWidgets()
            if (
                isinstance(widget, QMessageBox)
                and widget.isVisible()
                and widget.windowTitle() == title
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    def fail(error: BaseException) -> None:
        if state.error is None:
            state.error = error
        state.stop()
        try:
            message = current_message()
            if message is not None:
                message.reject()
        except BaseException:
            pass
        finally:
            if binding is not None and not recovery:
                binding.release_scheduled(state)

    def expire() -> None:
        if not state.handled and required:
            fail(GateError("technical-gate expected message did not appear"))
        else:
            state.stop()
            if binding is not None and not recovery:
                binding.release_scheduled(state)

    def poll() -> None:
        if state.handled or state.error is not None:
            return
        try:
            message = current_message()
            if message is None:
                return
            if state.diagnostic_message is message:
                state.diagnostic_poll_entered()
            if state._on_visible is not None:
                state._on_visible()
            if message is None:
                return
            button = next(
                (
                    candidate
                    for candidate in message.buttons()
                    if candidate.text().replace("&", "") == label
                ),
                None,
            )
            if button is None:
                fail(GateError("technical-gate expected message button is missing"))
                return
            if state.diagnostic_message is message:
                state._diagnostic_button_callback = on_diagnostic_button
                state.diagnostic_button_resolved()
            state.handled = True
            state.stop()
            button.click()
            if state._on_selected is not None:
                state._on_selected()
            if state.diagnostic_message is message:
                state.diagnostic_selection_complete()
            if binding is not None and not recovery:
                binding.release_scheduled(state)
        except BaseException as exc:
            fail(exc)

    poll_timer.timeout.connect(poll)
    deadline_timer.timeout.connect(expire)

    def owner_deleted(*_args: object) -> None:
        if not state.handled and state.error is None:
            fail(GateError("technical-gate message interaction failed"))

    destroyed = getattr(owner, "destroyed", None)
    connect_destroyed = getattr(destroyed, "connect", None)
    if callable(connect_destroyed):
        connect_destroyed(owner_deleted)
    poll_timer.start()
    deadline_timer.start(max(1, int(timeout_s * 1000)))
    return state


def _schedule_review_confirmation(
    *,
    app: Any,
    owner: Any,
    required: bool,
    timeout_s: float = 15.0,
    pre_dialog_timeout_s: float = _REVIEW_PRE_DIALOG_TIMEOUT_S,
    binding: _WorkspaceMessageBinding,
    on_visible: Callable[[], None] | None = None,
    on_selected: Callable[[], None] | None = None,
) -> _ScheduledMessageClick:
    """Reserve a dormant Review scheduler and bind only an explicit QMessageBox."""
    del app, required
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    poll_timer = QTimer(owner)
    poll_timer.setInterval(10)
    deadline_timer = QTimer(owner)
    deadline_timer.setSingleShot(True)
    deadline_timer.setInterval(max(1, int(timeout_s * 1000)))
    pre_dialog_timer = QTimer(owner)
    pre_dialog_timer.setSingleShot(True)
    state = _ScheduledMessageClick(
        poll_timer=poll_timer,
        deadline_timer=deadline_timer,
        pre_dialog_timer=pre_dialog_timer,
        review=True,
    )
    state._on_visible = on_visible
    state._on_selected = on_selected

    def fail(_error: BaseException | None = None) -> None:
        if state.error is None:
            state.error = GateError(
                _WorkspaceMessageBinding._REVIEW_FIXED_ERROR
            )
        try:
            binding.fail_review()
        finally:
            state.stop()

    def expire() -> None:
        if not state.handled:
            fail()

    def poll() -> None:
        if state.handled or state.error is not None:
            return
        message = state.bound_message
        if message is None:
            fail()
            return
        try:
            if state._on_visible is not None:
                state._on_visible()
            button = message.button(QMessageBox.StandardButton.Ok)
            if button is None:
                fail()
                return
            state.handled = True
            state.stop()
            button.click()
            if state._on_selected is not None:
                state._on_selected()
        except BaseException:
            fail()

    poll_timer.timeout.connect(poll)
    deadline_timer.timeout.connect(expire)

    def owner_deleted(*_args: object) -> None:
        if not state.handled and state.error is None:
            fail()

    destroyed = getattr(owner, "destroyed", None)
    connect_destroyed = getattr(destroyed, "connect", None)
    if callable(connect_destroyed):
        connect_destroyed(owner_deleted)
    try:
        binding.register_review(
            state,
            pre_dialog_timeout_s=pre_dialog_timeout_s,
        )
    except BaseException:
        state.stop()
        raise
    return state


def _instrument_recovery_boundaries(
    *,
    window: Any,
    record: Callable[[str], None],
    message_binding: _WorkspaceMessageBinding | None = None,
) -> Callable[[], None]:
    """Trace recovery dispatch boundaries without changing application behavior.

    Callers pass the optional ``message_binding=`` diagnostic state owner.
    """
    original_dispatch = window._dispatch_command
    original_handle_workspace_result = window._handle_workspace_result

    def dispatch(
        kind: str,
        payload: dict[str, Any],
        *,
        pending_action: str,
        pending_params: dict[str, Any] | None = None,
    ) -> bool:
        traces_recovery = kind == "list_recoveries"
        if traces_recovery:
            record("recovery-list-dispatch-requested")
        try:
            accepted = original_dispatch(
                kind,
                payload,
                pending_action=pending_action,
                pending_params=pending_params,
            )
        except BaseException:
            if kind == "review_restore" and message_binding is not None:
                message_binding.fail_review()
            raise
        if traces_recovery and accepted:
            record("recovery-list-dispatch-accepted")
        if accepted and kind == "review_restore" and message_binding is not None:
            message_binding.correlate_review_dispatch(
                getattr(window, "_pending_command_id", message_binding._MISSING)
            )
        elif kind == "review_restore" and message_binding is not None:
            message_binding.fail_review()
        return accepted

    def handle_workspace_result(result: Any) -> Any:
        review_armed = False
        if window._pending_action == "review_restore" and message_binding is not None:
            command_id = getattr(result, "command_id", message_binding._MISSING)
            if result.success:
                review_armed = message_binding.arm_review(command_id=command_id)
                if not review_armed:
                    message_binding.fail_review()
            else:
                message_binding.fail_review()
        if window._pending_action == "list_recoveries":
            record("recovery-list-result-received")
            if result.success:
                record("recovery-list-result-succeeded")
                payload = result.payload if isinstance(result.payload, Mapping) else {}
                recoveries = payload.get("recoveries")
                if isinstance(recoveries, list) and recoveries:
                    record("recovery-candidates-received")
                    if message_binding is not None:
                        message_binding.arm_recovery()
        try:
            return original_handle_workspace_result(result)
        except BaseException:
            if review_armed and message_binding is not None:
                message_binding.fail_review()
            raise

    window._dispatch_command = dispatch
    window._handle_workspace_result = handle_workspace_result

    def restore() -> None:
        """Restore only wrappers still owned by this instrumentation."""
        if getattr(window, "_dispatch_command", None) is dispatch:
            window._dispatch_command = original_dispatch
        if getattr(window, "_handle_workspace_result", None) is handle_workspace_result:
            window._handle_workspace_result = original_handle_workspace_result

    return restore


def _run_consumer_launcher(*, checkout: Path, project: Path, work_root: Path) -> None:
    """Open a saved project in a fresh launcher and explicitly restore recovery."""
    _record_gate_stage(work_root, "consumer", "bootstrap")
    import site

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    import gwexpy_studio
    from gwexpy_studio.ui import app as launcher
    from gwexpy_studio.ui.bridge import BridgeState
    from gwexpy_studio.ui.window import MainWindow

    verify_installed_module_path(
        module_path=Path(gwexpy_studio.__file__ or ""),
        site_roots=tuple(Path(path) for path in site.getsitepackages()),
        checkout_root=checkout,
    )
    _installed_identity()

    failure: list[BaseException] = []

    def restore_boundaries() -> None:
        """Keep launcher cleanup safe if instrumentation never installs."""
        return None

    def drive() -> None:
        nonlocal restore_boundaries
        try:
            _record_gate_stage(work_root, "consumer", "launcher")
            app = QApplication.instance()
            if not isinstance(app, QApplication):
                raise GateError("normal launcher did not create QApplication")
            windows = [
                item for item in app.topLevelWidgets() if isinstance(item, MainWindow)
            ]
            if len(windows) != 1:
                raise GateError("normal launcher did not create one main window")
            window = windows[0]
            _record_gate_stage(work_root, "consumer", "project-reopen")
            _wait(app, lambda: window.isVisible(), "consumer welcome")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 3
                    and bool(window._workspace_status.get("needs_restore"))
                ),
                "project reopen",
            )
            _wait(
                app,
                lambda: window.recovery_notice.isVisible(),
                "recovery notice",
            )
            _record_gate_stage(work_root, "consumer", "recovery-review")
            restore_message = _schedule_message_box_button(
                app=app,
                owner=window,
                label="Restore",
                title=_RECOVERY_DIALOG_TITLE,
                required=True,
                binding=message_binding,
                recovery=True,
                on_visible=lambda: _record_gate_stage(
                    work_root, "consumer", "recovery-candidate-visible"
                ),
                on_selected=lambda: _record_gate_stage(
                    work_root, "consumer", "recovery-candidate-selected"
                ),
            )
            discard_unsaved_message = _schedule_message_box_button(
                app=app,
                owner=window,
                label="Discard",
                title="Unsaved project",
                required=False,
                binding=message_binding,
                on_visible=lambda: _record_gate_stage(
                    work_root, "consumer", "unsaved-project-visible"
                ),
                on_selected=lambda: _record_gate_stage(
                    work_root, "consumer", "unsaved-project-discarded"
                ),
            )
            restore_boundaries = _instrument_recovery_boundaries(
                window=window,
                record=lambda stage: _record_gate_stage(
                    work_root, "consumer", stage
                ),
                message_binding=message_binding,
            )
            _record_gate_stage(work_root, "consumer", "recovery-review-trigger")
            QTest.mouseClick(
                window.review_recovery_notice_button, Qt.MouseButton.LeftButton
            )
            _record_gate_stage(work_root, "consumer", "recovery-review-requested")
            _wait(
                app,
                lambda: restore_message.handled or restore_message.error is not None,
                "recovery dialog",
            )
            restore_message.raise_if_failed()
            discard_unsaved_message.raise_if_failed()
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 4
                    and bool(window._workspace_status.get("needs_restore"))
                    and not window._workspace_status.get("project_path")
                ),
                "recovery restore",
            )
            _record_gate_stage(work_root, "consumer", "recovery-restored")
            _record_gate_stage(work_root, "consumer", "data-restore")
            if not message_binding.recovery_binding_isolated:
                raise GateError("technical-gate recovery dialog isolation failed")
            review_message = _schedule_review_confirmation(
                app=app,
                owner=window,
                required=True,
                binding=message_binding,
            )
            window.restore_project_action.trigger()
            try:
                _wait(
                    app,
                    lambda: review_message.handled
                    or review_message.error is not None,
                    "review dialog",
                    timeout_s=_REVIEW_PRE_DIALOG_TIMEOUT_S,
                )
            except GateError:
                message_binding.fail_review()
                raise
            review_message.raise_if_failed()
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 4
                    and not window._workspace_status.get("needs_restore")
                    and window._restore_progress is None
                ),
                "reviewed data restore",
            )
            _record_gate_stage(work_root, "consumer", "recovery-consumption")
            window.recoveries_action.trigger()
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and not window._command_reserved
                ),
                "recovery consumption",
            )
            restored = work_root / "restored.gwxproj"
            _record_gate_stage(work_root, "consumer", "restored-project-save")
            if restored.exists() or not window.save_project_to(str(restored)):
                raise GateError("restored project could not be saved for clean exit")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and restored.is_file()
                    and window._workspace_status.get("project_path") == str(restored)
                    and not window._workspace_status.get("dirty")
                ),
                "restored project save",
            )
            _record_gate_stage(work_root, "consumer", "worker-exit")
            window.close()
            _wait(
                app,
                lambda: (
                    not window.isVisible()
                    and not window.bridge.worker_thread.isRunning()
                ),
                "worker exit",
            )
            _record_gate_stage(work_root, "consumer", "complete")
        except BaseException as exc:
            failure.append(exc)
            QApplication.quit()

    app = QApplication.instance()
    if app is None:
        QApplication(_qapplication_arguments())
    elif not isinstance(app, QApplication):
        raise GateError("normal launcher cannot use the existing Qt application")
    message_binding = _install_workspace_message_binding(
        record=lambda stage: _record_gate_stage(
            work_root, *stage.split("/", 1)
        )
    )
    QTimer.singleShot(0, drive)
    try:
        launcher_status = launcher.main((str(project),))
    finally:
        restore_boundaries()
        message_binding.restore()
    if launcher_status != 0:
        raise GateError("normal project launcher did not exit cleanly")
    if failure:
        raise GateError("technical-gate recovery workflow failed") from failure[0]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the isolated installed-wheel gate and persist a bounded outcome."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.phase is not None:
        if arguments.replay_python is not None or arguments.legacy_gate:
            parser.error("replay and legacy options require the master gate")
        return _run_phase(arguments)
    if arguments.project is not None:
        parser.error("--project requires --phase consumer")
    if arguments.result is None:
        parser.error("--result is required without --phase")
    if arguments.gate_fd is None:
        parser.error("--gate-fd is required without --phase")
    if arguments.legacy_gate and arguments.replay_python is not None:
        parser.error("--legacy-gate cannot be combined with --replay-python")
    check_names = _LEGACY_CHECK_NAMES if arguments.legacy_gate else _CHECK_NAMES
    checks = {name: False for name in check_names}
    installed: dict[str, str] | None = None
    try:
        work_root = arguments.work_root.resolve(strict=True)
        result = arguments.result.resolve(strict=False)
        if result.parent != work_root or result.name != "technical-gate.json":
            raise GateError("technical-gate result path is outside its workspace")
        if result.exists() or result.is_symlink():
            raise GateError("technical-gate result path is not fresh")
        if sys.platform not in {"linux", "darwin"}:
            raise GateError("technical-gate recovery qualification requires POSIX")
        run_external_recovery_gate(
            checks=checks,
            checkout=arguments.checkout.resolve(strict=True),
            gate_fd=_validated_gate_fd(arguments.gate_fd),
            phase_python=Path(sys.executable),
            work_root=work_root,
            replay_python=arguments.replay_python.resolve(strict=True)
            if arguments.replay_python is not None
            else None,
            native_qt=arguments.native_qt,
            parent_shm_prefix=os.environ.get("GWEXPY_STUDIO_SHM_PREFIX"),
            legacy=arguments.legacy_gate,
        )
        installed = _installed_identity()
        prefix = os.environ.get("GWEXPY_STUDIO_SHM_PREFIX")
        if not prefix:
            raise GateError("technical-gate shared-memory prefix is unavailable")
        portable_cleanup = shared_memory_cleanup_probe(prefix)
        linux_namespace_clean = sys.platform != "linux" or not any(
            Path("/dev/shm").glob(f"{prefix}*")
        )
        checks["shared_memory_cleanup"] = portable_cleanup and linux_namespace_clean
        if not checks["shared_memory_cleanup"]:
            raise GateError("technical-gate shared memory was not cleaned up")
        _remove_xdg_roots(work_root)
        result.write_bytes(gate_result_json(checks, installed=installed))
        return 0
    except BaseException:
        try:
            _remove_xdg_roots(arguments.work_root)
        except (OSError, GateError):
            pass
        try:
            if installed is not None:
                arguments.result.write_bytes(
                    gate_result_json(checks, installed=installed)
                )
        except (OSError, GateError):
            pass
        return 1


if __name__ == "__main__":  # pragma: no cover - direct CLI invocation.
    raise SystemExit(main())
