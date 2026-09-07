"""Worker bridge coordinating asynchronous controller calls across QThread."""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Any, Literal, cast

from PySide6.QtCore import QEventLoop, QObject, Qt, QThread, QTimer, Signal, Slot

from ..application.alpha import AlphaController
from ..errors import CodedStudioError, OperationError
from ..worker.client import WorkerClient
from .bridge_snapshot import operation_failure_payload as _operation_failure_payload
from .bridge_snapshot import operation_success_payload
from .signal_bridge import (
    SIGNAL_OPTIONAL,
    SIGNAL_REQUIRED,
    execute_signal_command,
)
from .workspace_commands import (
    WORKSPACE_GESTURES,
    WORKSPACE_OPTIONAL,
    WORKSPACE_REQUIRED,
)

CommandKind = Literal[
    "start",
    "inspect",
    "load",
    "apply",
    "preview",
    "export",
    "catalog_io",
    "inspect_io",
    "read_io",
    "write_data",
    "list_members",
    "member_preview",
    "apply_multi",
    "filter_preview",
    "set_plot",
    "new_project",
    "open_project",
    "save_project",
    "close_project",
    "set_ui_state",
    "undo_analysis",
    "redo_analysis",
    "review_restore",
    "restore_project",
    "list_recoveries",
    "recover_project",
    "discard_recovery",
]


class BridgeState(StrEnum):
    """Allowed states of the worker bridge."""

    IDLE = "idle"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


_ALLOWED_KINDS: frozenset[str] = frozenset(
    {"start", "inspect", "load", "apply", "preview", "export"}
    | SIGNAL_REQUIRED.keys()
    | WORKSPACE_REQUIRED.keys()
)

_REQUIRED_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "start": frozenset(),
    "inspect": frozenset({"uri"}),
    "load": frozenset({"inspection"}),
    "apply": frozenset({"op_name", "input_id"}),
    "preview": frozenset({"object_id"}),
    "export": frozenset({"target_path"}),
    **SIGNAL_REQUIRED,
    **WORKSPACE_REQUIRED,
}

_OPTIONAL_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "start": frozenset(),
    "inspect": frozenset(),
    "load": frozenset(),
    "apply": frozenset({"params"}),
    "preview": frozenset({"preview_stride"}),
    "export": frozenset({"targets", "include_data_writes"}),
    **SIGNAL_OPTIONAL,
    **WORKSPACE_OPTIONAL,
}
for _gesture in WORKSPACE_GESTURES:
    _OPTIONAL_PAYLOAD_KEYS[_gesture] |= {"ui_state"}


@dataclass(frozen=True, slots=True)
class BridgeCommand:
    """Immutable command passed from UI main thread to bridge worker."""

    command_id: str
    kind: CommandKind
    payload: Mapping[str, Any]
    deadline_monotonic: float

    def __post_init__(self) -> None:
        """Validate command shape and kind-specific payload parameters."""
        try:
            uuid.UUID(self.command_id)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid command_id UUID: {self.command_id!r}") from exc
        if self.kind not in _ALLOWED_KINDS:
            raise ValueError(f"Invalid command kind: {self.kind!r}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a Mapping")

        required = _REQUIRED_PAYLOAD_KEYS[self.kind]
        allowed = required | _OPTIONAL_PAYLOAD_KEYS[self.kind]
        payload_keys = set(self.payload)
        missing = required - payload_keys
        if missing:
            raise ValueError(
                f"Missing required keys for command {self.kind!r}: {sorted(missing)}"
            )
        unknown = payload_keys - allowed
        if unknown:
            raise ValueError(
                f"Unknown keys for command {self.kind!r}: {sorted(unknown)}"
            )
        if (
            isinstance(self.deadline_monotonic, bool)
            or not isinstance(self.deadline_monotonic, (int, float))
            or not math.isfinite(self.deadline_monotonic)
            or self.deadline_monotonic <= 0
        ):
            raise ValueError("deadline_monotonic must be a positive finite float")


@dataclass(frozen=True, slots=True)
class BridgeResult:
    """Immutable result emitted from bridge worker to UI main thread."""

    command_id: str
    success: bool
    payload: Any = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _CloseOutcome:
    """Result of the sole worker-owned cleanup attempt."""

    success: bool
    error_message: str | None
    finished_monotonic: float


_MAX_CLEANUP_TIMEOUT_S = 15.0
_MAX_QT_TIMER_MSEC = 2_147_483_647
_FATAL_ERROR_CODES = frozenset(
    {
        "timeout",
        "worker_timeout",
        "worker_crashed",
        "cleanup_failed",
        "deadline_propagation_failed",
    }
)


def _remaining_seconds(deadline_monotonic: float) -> float:
    return max(deadline_monotonic - time.monotonic(), 0.0)


def _timer_msec(deadline_monotonic: float) -> int:
    remaining = _remaining_seconds(deadline_monotonic)
    if remaining <= 0:
        return 0
    return min(max(math.ceil(remaining * 1000), 1), _MAX_QT_TIMER_MSEC)


def _deadline_failure(stage: str, error: BaseException | None = None) -> OperationError:
    detail = ""
    if error is not None:
        try:
            detail = f": {error}"
        except BaseException:
            detail = f": {type(error).__name__}"
    return OperationError(
        f"Worker deadline scope {stage} failed{detail}",
        code="deadline_propagation_failed",
    )


@contextmanager
def _client_deadline(client: Any, deadline_monotonic: float) -> Iterator[None]:
    """Enter only the client's public deadline scope and normalize its defects."""
    try:
        factory = getattr(client, "deadline_scope", None)
    except BaseException as exc:
        raise _deadline_failure("capability lookup", exc) from exc
    if not callable(factory):
        raise _deadline_failure("capability lookup")
    try:
        manager = factory(deadline_monotonic)
    except BaseException as exc:
        raise _deadline_failure("creation", exc) from exc
    try:
        enter = getattr(manager, "__enter__", None)
        exit_scope = getattr(manager, "__exit__", None)
    except BaseException as exc:
        raise _deadline_failure("context validation", exc) from exc
    if not callable(enter) or not callable(exit_scope):
        raise _deadline_failure("context validation")
    try:
        enter()
    except BaseException as exc:
        raise _deadline_failure("entry", exc) from exc

    body_error: BaseException | None = None
    body_traceback = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        body_traceback = exc.__traceback__

    try:
        suppressed = bool(
            exit_scope(
                type(body_error) if body_error is not None else None,
                body_error,
                body_traceback,
            )
        )
    except BaseException as exc:
        raise _deadline_failure("exit", exc) from exc
    if body_error is not None and suppressed:
        raise _deadline_failure("exit suppression")
    if body_error is not None:
        raise body_error.with_traceback(body_traceback)


@contextmanager
def _controller_deadline(
    controller: AlphaController, deadline_monotonic: float
) -> Iterator[None]:
    try:
        session = getattr(controller, "session", None)
        client = getattr(session, "client", None)
    except BaseException as exc:
        raise _deadline_failure("client lookup", exc) from exc
    if client is None:
        yield
        return
    with _client_deadline(client, deadline_monotonic):
        yield


class BridgeWorker(QObject):
    """Worker object living on the bridge QThread."""

    result_ready = Signal(object)
    close_finished = Signal(object)
    progress_ready = Signal(object)

    def __init__(
        self,
        *,
        controller: AlphaController | None = None,
        client: WorkerClient | None = None,
    ) -> None:
        """Store exactly one optional owner without starting work."""
        super().__init__()
        self._controller = controller
        self._client = client
        self._close_outcome: _CloseOutcome | None = None
        self.cancel_event = Event()
        self.clean_workspace = False
        self._resumed = False

    def _get_controller(self) -> AlphaController:
        if self._controller is None:
            from ..application.workspace_controller import WorkspaceController

            self._controller = WorkspaceController(client=self._client)
        if not self._resumed:
            resume = getattr(self._controller, "resume_workspace", None)
            if callable(resume):
                resume()
            self._resumed = True
        return self._controller

    @Slot(object)
    def handle_command(self, command: BridgeCommand) -> None:
        """Execute one command under its immutable public client deadline."""
        if time.monotonic() >= command.deadline_monotonic:
            self.result_ready.emit(
                BridgeResult(
                    command_id=command.command_id,
                    success=False,
                    error_code="timeout",
                    error_message="Command deadline expired before execution",
                )
            )
            return
        controller: AlphaController | None = None
        operation_dispatched = False
        try:
            controller = self._get_controller()
            with _controller_deadline(controller, command.deadline_monotonic):
                operation_dispatched = True
                self._command_deadline = command.deadline_monotonic
                self._command_id = command.command_id
                payload = self._execute(controller, command.kind, command.payload)
            if time.monotonic() >= command.deadline_monotonic:
                result = BridgeResult(
                    command_id=command.command_id,
                    success=False,
                    error_code="timeout",
                    error_message="Command deadline expired during execution",
                )
            else:
                payload = operation_success_payload(controller, command.kind, payload)
                result = BridgeResult(
                    command_id=command.command_id, success=True, payload=payload
                )
        except CodedStudioError as exc:
            timed_out = (
                exc.code == "worker_timeout"
                or time.monotonic() >= command.deadline_monotonic
            )
            result = BridgeResult(
                command_id=command.command_id,
                success=False,
                payload=_operation_failure_payload(
                    controller,
                    command.kind,
                    operation_dispatched=operation_dispatched,
                    timed_out=timed_out,
                ),
                error_code="timeout" if timed_out else exc.code,
                error_message=(
                    "Command deadline expired during execution"
                    if timed_out
                    else str(exc)
                ),
            )
        except Exception as exc:
            timed_out = time.monotonic() >= command.deadline_monotonic
            result = BridgeResult(
                command_id=command.command_id,
                success=False,
                payload=_operation_failure_payload(
                    controller,
                    command.kind,
                    operation_dispatched=operation_dispatched,
                    timed_out=timed_out,
                ),
                error_code="timeout" if timed_out else "operation_failed",
                error_message=(
                    "Command deadline expired during execution"
                    if timed_out
                    else str(exc)
                ),
            )
        self.result_ready.emit(result)

    def _execute(
        self,
        controller: AlphaController,
        kind: CommandKind,
        payload: Mapping[str, Any],
    ) -> Any:
        if (
            kind in WORKSPACE_GESTURES
            and "ui_state" in payload
            and hasattr(controller, "set_ui_state")
        ):
            controller.set_ui_state(payload["ui_state"])
        if kind in WORKSPACE_REQUIRED:
            from ..application.workspace_bridge import execute_workspace_command

            return execute_workspace_command(
                cast(Any, controller),
                kind,
                payload,
                progress=lambda value: self.progress_ready.emit(
                    {**value, "command_id": self._command_id}
                ),
                cancel_event=self.cancel_event,
                deadline_monotonic=getattr(self, "_command_deadline", None),
            )
        if kind == "start":
            controller.start()
            client = getattr(getattr(controller, "session", None), "client", None)
            snapshot = getattr(client, "capability_snapshot", None)
            document = getattr(snapshot, "document", None)
            return {
                "ready": True,
                **({"io_capabilities": document()} if callable(document) else {}),
            }
        if kind == "inspect":
            return {"inspection": controller.inspect_source(payload["uri"])}
        if kind == "load":
            source, object_ref = controller.load_source(payload["inspection"])
            return {
                "source": source,
                "object": object_ref,
            }
        if kind == "apply":
            object_ref = controller.apply(
                payload["op_name"], payload["input_id"], payload.get("params")
            )
            return {"object": object_ref}
        if kind == "preview":
            preview_payload: Any
            if payload.get("preview_stride") is not None:
                preview_payload = controller.fetch_preview_payload(
                    payload["object_id"], preview_stride=payload["preview_stride"]
                )
            else:
                preview_payload = controller.fetch_member_preview_payload(
                    payload["object_id"]
                )
            return {"preview_payload": preview_payload}
        if kind == "export":
            options: dict[str, Any] = {"targets": payload.get("targets")}
            if "include_data_writes" in payload:
                options["include_data_writes"] = payload["include_data_writes"]
            controller.export_script(payload["target_path"], **options)
            return {"exported": True}
        signal_result = execute_signal_command(controller, kind, payload)
        if signal_result is not None:
            return signal_result
        raise OperationError(f"Unknown command kind: {kind}", code="invalid_command")

    def _close_owned_once(self, deadline_monotonic: float) -> _CloseOutcome:
        if self._close_outcome is not None:
            return self._close_outcome
        error_message: str | None = None
        try:
            if self._controller is not None:
                with _controller_deadline(self._controller, deadline_monotonic):
                    self._controller.close()
                    if self.clean_workspace:
                        finalize = getattr(self._controller, "finalize_workspace", None)
                        if callable(finalize):
                            finalize(clean=True)
            elif self._client is not None:
                with _client_deadline(self._client, deadline_monotonic):
                    self._client.shutdown()
        except BaseException as exc:
            try:
                error_message = f"Controller cleanup failed: {exc}"
            except BaseException:
                error_message = f"Controller cleanup failed: {type(exc).__name__}"
        finished = time.monotonic()
        if error_message is None and finished > deadline_monotonic:
            error_message = "Controller cleanup exceeded its deadline"
        self._close_outcome = _CloseOutcome(
            success=error_message is None,
            error_message=error_message,
            finished_monotonic=finished,
        )
        return self._close_outcome

    @Slot(float)
    def close_owned(self, deadline_monotonic: float) -> None:
        """Close the owned controller or direct client exactly once."""
        self.close_finished.emit(self._close_owned_once(deadline_monotonic))


class WorkerBridge(QObject):
    """UI-thread facade managing one worker QThread and command at a time."""

    command_dispatched = Signal(object)
    close_requested = Signal(float)
    result_received = Signal(object)
    state_changed = Signal(str)
    cleanup_failed = Signal(str)
    safe_to_destroy = Signal()
    progress_received = Signal(object)

    def __init__(
        self,
        *,
        controller: AlphaController | None = None,
        client: WorkerClient | None = None,
        parent: QObject | None = None,
    ) -> None:
        """Validate ownership, wire the worker thread, and start its event loop."""
        if controller is not None and client is not None:
            raise ValueError("controller and client cannot both be supplied")
        super().__init__(parent)
        self._thread = QThread()
        self._worker = BridgeWorker(controller=controller, client=client)
        self._worker.moveToThread(self._thread)
        self._state = BridgeState.IDLE
        self._current_command_id: str | None = None
        self._current_deadline: float | None = None
        self._close_started = False
        self._close_in_progress = False
        self._close_completed = False
        self._cleanup_deadline: float | None = None
        self._cleanup_timed_out = False
        self._cleanup_error: str | None = None
        self._safe_emitted = False

        self._command_timer = QTimer(self)
        self._command_timer.setSingleShot(True)
        self._command_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._command_timer.timeout.connect(self._on_command_timeout)
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.setSingleShot(True)
        self._cleanup_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._cleanup_timer.timeout.connect(self._on_cleanup_timeout)

        self.command_dispatched.connect(
            self._worker.handle_command, Qt.ConnectionType.QueuedConnection
        )
        self.close_requested.connect(
            self._worker.close_owned, Qt.ConnectionType.QueuedConnection
        )
        self._worker.result_ready.connect(
            self._on_result_ready, Qt.ConnectionType.QueuedConnection
        )
        self._worker.progress_ready.connect(self._on_progress)
        self._worker.close_finished.connect(
            self._on_close_finished, Qt.ConnectionType.QueuedConnection
        )
        self._thread.finished.connect(
            self._on_thread_finished, Qt.ConnectionType.QueuedConnection
        )
        self._thread.start()

    @property
    def state(self) -> BridgeState:
        """Return the facade lifecycle state."""
        return self._state

    @property
    def worker_thread(self) -> QThread:
        """Return the QThread that owns the controller and worker."""
        return self._thread

    @property
    def cleanup_error(self) -> str | None:
        """Return the first cleanup failure, if one occurred."""
        return self._cleanup_error

    def _set_state(self, state: BridgeState) -> None:
        if self._state == state:
            return
        self._state = state
        self.state_changed.emit(state.value)

    @staticmethod
    def _validate_timeout(timeout_s: float) -> float:
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s < 0
        ):
            raise ValueError("timeout_s must be a non-negative finite float")
        return float(timeout_s)

    @staticmethod
    def _start_timer(timer: QTimer, deadline_monotonic: float) -> None:
        timer.start(_timer_msec(deadline_monotonic))

    def _clear_current_command(self) -> None:
        self._command_timer.stop()
        self._current_command_id = None
        self._current_deadline = None

    def send_command(
        self,
        kind: CommandKind,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_s: float = 90.0,
    ) -> str:
        """Queue one non-terminal command for worker-thread execution."""
        if kind not in _ALLOWED_KINDS:
            raise ValueError(f"Invalid command kind: {kind!r}; close is terminal-only")
        if self._close_started:
            raise OperationError(
                "Cannot dispatch command after terminal cleanup was reserved",
                code="bridge_unavailable",
            )
        if self._state is not BridgeState.IDLE:
            raise OperationError(
                f"Cannot dispatch command in state {self._state.value!r}",
                code=(
                    "bridge_busy"
                    if self._state is BridgeState.RUNNING
                    else "bridge_unavailable"
                ),
            )
        timeout = self._validate_timeout(timeout_s)
        command_id = str(uuid.uuid4())
        deadline = time.monotonic() + timeout
        command = BridgeCommand(
            command_id=command_id,
            kind=kind,
            payload=payload or {},
            deadline_monotonic=deadline,
        )
        self._current_command_id = command_id
        self._worker.cancel_event.clear()
        self._current_deadline = deadline
        self._set_state(BridgeState.RUNNING)
        if self._close_started:
            raise OperationError(
                "Cannot dispatch command after terminal cleanup was reserved",
                code="bridge_unavailable",
            )
        self._start_timer(self._command_timer, deadline)
        self.command_dispatched.emit(command)
        return command_id

    def _reserve_close(self, deadline_monotonic: float) -> bool:
        """Reserve cleanup fully and queue it before any lifecycle callback."""
        if self._close_started:
            return False
        self._close_started = True
        self._close_in_progress = True
        self._cleanup_deadline = deadline_monotonic
        self._clear_current_command()
        self._start_timer(self._cleanup_timer, deadline_monotonic)
        self.close_requested.emit(deadline_monotonic)
        return True

    def _begin_fatal_close(self, result: BridgeResult, deadline: float) -> None:
        if not self._reserve_close(deadline):
            return
        self._set_state(BridgeState.FAILED)
        self.result_received.emit(result)
        self._set_state(BridgeState.CLOSING)

    @Slot(object)
    def _on_result_ready(self, result: BridgeResult) -> None:
        if result.command_id != self._current_command_id:
            return
        deadline = self._current_deadline
        if deadline is None:
            return
        if time.monotonic() >= deadline:
            result = BridgeResult(
                command_id=result.command_id,
                success=False,
                error_code="timeout",
                error_message="Command deadline expired before response application",
            )
        if not result.success and result.error_code in _FATAL_ERROR_CODES:
            self._begin_fatal_close(result, deadline)
            return
        self._clear_current_command()
        self._set_state(BridgeState.IDLE)
        self.result_received.emit(result)

    @Slot()
    def _on_command_timeout(self) -> None:
        command_id = self._current_command_id
        deadline = self._current_deadline
        if (
            self._state is not BridgeState.RUNNING
            or command_id is None
            or deadline is None
        ):
            return
        if time.monotonic() < deadline:
            self._start_timer(self._command_timer, deadline)
            return
        self._begin_fatal_close(
            BridgeResult(
                command_id=command_id,
                success=False,
                error_code="timeout",
                error_message="Command deadline expired",
            ),
            deadline,
        )

    def _report_cleanup_failure(self, message: str) -> None:
        if self._cleanup_error is not None:
            return
        self._cleanup_error = message
        self.cleanup_failed.emit(message)

    @Slot()
    def _on_cleanup_timeout(self) -> None:
        if not self._close_in_progress or self._cleanup_deadline is None:
            return
        if time.monotonic() < self._cleanup_deadline:
            self._start_timer(self._cleanup_timer, self._cleanup_deadline)
            return
        self._cleanup_timed_out = True
        self._set_state(BridgeState.FAILED)
        self._report_cleanup_failure("Controller cleanup deadline expired")

    def _emit_safe_if_stopped(self) -> None:
        if self._safe_emitted or self._thread.isRunning():
            return
        self._safe_emitted = True
        self.safe_to_destroy.emit()

    @Slot()
    def _on_thread_finished(self) -> None:
        self._emit_safe_if_stopped()

    @Slot(object)
    def _on_close_finished(self, outcome: _CloseOutcome) -> None:
        if not self._close_in_progress or self._close_completed:
            return
        self._close_completed = True
        self._close_in_progress = False
        self._cleanup_timer.stop()
        self._thread.quit()
        deadline = self._cleanup_deadline
        stopped = self._thread.wait(0 if deadline is None else _timer_msec(deadline))
        finished_late = deadline is not None and (
            outcome.finished_monotonic > deadline or time.monotonic() > deadline
        )
        if self._cleanup_timed_out or finished_late:
            self._set_state(BridgeState.FAILED)
            self._report_cleanup_failure("Controller cleanup deadline expired")
        elif not outcome.success:
            self._set_state(BridgeState.FAILED)
            self._report_cleanup_failure(
                outcome.error_message or "Controller cleanup failed"
            )
        elif not stopped:
            self._set_state(BridgeState.FAILED)
            self._report_cleanup_failure("Worker thread did not stop before deadline")
        else:
            self._set_state(BridgeState.CLOSED)
        self._emit_safe_if_stopped()

    def _wait_for_initial_close(self) -> None:
        """Process UI events until initial cleanup reaches a terminal state."""
        if self._state is not BridgeState.CLOSING:
            return
        loop = QEventLoop()

        def stop_on_terminal(state: str) -> None:
            if state in {BridgeState.CLOSED.value, BridgeState.FAILED.value}:
                loop.quit()

        self.state_changed.connect(stop_on_terminal)
        try:
            if self._state is BridgeState.CLOSING:
                loop.exec()
        finally:
            self.state_changed.disconnect(stop_on_terminal)

    def request_cancel(self) -> None:
        """Request cancellation through thread-safe flags, retaining the document."""
        self._worker.cancel_event.set()
        controller = self._worker._controller
        cancel = getattr(controller, "request_cancel", None)
        if callable(cancel):
            cancel()

    @Slot(object)
    def _on_progress(self, value: Mapping[str, Any]) -> None:
        if value.get("command_id") == self._current_command_id:
            self.progress_received.emit(value)

    def take_controller(self) -> AlphaController | None:
        """Transfer a controller only after its former owner thread has stopped."""
        if self._thread.isRunning():
            raise OperationError(
                "Wait for the old worker thread to stop", code="bridge_busy"
            )
        controller = self._worker._controller
        self._worker._controller = None
        return controller

    def close(self, timeout_s: float = 15.0, *, clean_workspace: bool = False) -> None:
        """Queue terminal cleanup once and boundedly observe its initial outcome."""
        if self._close_started:
            return
        self._worker.clean_workspace = clean_workspace
        timeout = min(self._validate_timeout(timeout_s), _MAX_CLEANUP_TIMEOUT_S)
        deadline = time.monotonic() + timeout
        if self._reserve_close(deadline):
            self._set_state(BridgeState.CLOSING)
            self._wait_for_initial_close()
