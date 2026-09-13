"""Run one bounded, observer-only NORMAL technical-gate diagnostic.

The formal gate is loaded from a separately checked-out source file and is
never modified on disk.  This module wraps only in-memory observer boundaries
around that gate, then writes a deliberately closed JSON snapshot.  The
snapshot is diagnostic evidence and cannot be consumed as a qualification
result.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


class DiagnosticSchemaError(ValueError):
    """Raised when a diagnostic snapshot is outside its closed schema."""


_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA64 = re.compile(r"[0-9a-f]{64}")
_OUTCOMES = frozenset({"running", "passed", "failed", "watchdog", "process_exit"})
_FAILURE_CATEGORIES = frozenset(
    {
        "none",
        "wait_expired",
        "phase_watchdog",
        "process_exit",
        "callback_failure",
        "dispatch_rejected",
        "result_failure",
        "observer_failure",
        "gate_failure",
    }
)
_STAGES = frozenset(
    {
        "bootstrap",
        "launcher",
        "welcome",
        "worker_ready",
        "io_catalog",
        "io_catalog_ready",
        "io_inspection",
        "io_read",
        "io_read_settled",
        "save_project",
        "save_after_refusal",
        "close_project",
        "empty_workspace",
        "open_project",
        "reopened_project",
        "restore_review",
        "restore_review_handled",
        "restored_state_settled",
        "restored_project",
        "io_unavailable",
        "io_refusal",
        "worker_exit",
        "complete",
        "unknown",
    }
)
_BRIDGE_STATES = frozenset(
    {"idle", "running", "closing", "closed", "failed", "unknown"}
)
_AUTOPREVIEW_RESULTS = frozenset({"unknown", "success", "failure", "not_applicable"})

OBSERVATION_KEYS = (
    "schema",
    "provenance",
    "outcome",
    "stage",
    "failure_category",
    "observations",
)
_PROVENANCE_KEYS = ("branch_sha", "original_gate_sha", "source_sha", "wheel_sha")
_OBSERVATION_FIELDS = (
    "confirm_enabled",
    "click_invoked",
    "click_signal_observed",
    "read_dispatch_accepted",
    "read_dispatch_returned",
    "read_result_success",
    "read_result_observed",
    "object_present",
    "object_resident",
    "needs_restore",
    "needs_restore_state",
    "preview_entered",
    "preview_completed",
    "bridge_state",
    "worker_running",
    "pending_action_allowlisted",
    "pending_action_class",
    "command_reserved",
    "modal_active",
    "review_modal_returned_ok",
    "restore_dispatch_requested",
    "restore_dispatch_accepted",
    "restore_dispatch_returned",
    "restore_result_success",
    "restore_result_observed",
    "autopreview_result",
)
_PENDING_ACTIONS = frozenset(
    {
        "worker_capability_start",
        "open_inspect",
        "open_load",
        "preview",
        "apply_op",
        "export",
        "save_project",
        "open_project",
        "close_project",
        "new_project",
        "set_ui_state",
        "list_recoveries",
        "signal_apply_multi",
        "signal_catalog_io",
        "signal_inspect_io",
        "signal_read_io",
        "signal_write_data",
        "signal_set_plot",
        "signal_list_members",
        "review_restore",
        "restore_project",
        "signal_member_preview",
    }
)
_PENDING_ACTION_CLASSES = frozenset({"none", "read", "preview", "restore", "other"})


def _empty_observations() -> dict[str, object]:
    return {
        "confirm_enabled": False,
        "click_invoked": False,
        "click_signal_observed": False,
        "read_dispatch_accepted": False,
        "read_dispatch_returned": False,
        "read_result_success": False,
        "read_result_observed": False,
        "object_present": False,
        "object_resident": False,
        "needs_restore": False,
        "needs_restore_state": "unknown",
        "preview_entered": False,
        "preview_completed": False,
        "bridge_state": "unknown",
        "worker_running": False,
        "pending_action_allowlisted": False,
        "pending_action_class": "none",
        "command_reserved": False,
        "modal_active": False,
        "review_modal_returned_ok": False,
        "restore_dispatch_requested": False,
        "restore_dispatch_accepted": False,
        "restore_dispatch_returned": False,
        "restore_result_success": False,
        "restore_result_observed": False,
        "autopreview_result": "unknown",
    }


def _safe_stage(value: str) -> str:
    return value if value in _STAGES else "unknown"


@dataclass(slots=True)
class DiagnosticObservation:
    """Collect fixed boolean observations without retaining application data."""

    branch_sha: str
    original_gate_sha: str
    source_sha: str
    wheel_sha: str
    outcome: str = "running"
    stage: str = "unknown"
    failure_category: str = "none"
    observations: dict[str, object] = field(default_factory=_empty_observations)
    snapshot_path: Path | None = None
    _expected_object_id: str | None = field(default=None, repr=False)
    _awaiting_restore_preview: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        """Validate immutable provenance and the initial closed snapshot."""
        if _SHA40.fullmatch(self.branch_sha) is None or _SHA40.fullmatch(
            self.source_sha
        ) is None:
            raise DiagnosticSchemaError("diagnostic provenance is invalid")
        if _SHA64.fullmatch(self.original_gate_sha) is None or _SHA64.fullmatch(
            self.wheel_sha
        ) is None:
            raise DiagnosticSchemaError("diagnostic provenance is invalid")
        self._validate_document(self._document())

    def _document(self) -> dict[str, object]:
        return {
            "schema": 1,
            "provenance": {
                "branch_sha": self.branch_sha,
                "original_gate_sha": self.original_gate_sha,
                "source_sha": self.source_sha,
                "wheel_sha": self.wheel_sha,
            },
            "outcome": self.outcome,
            "stage": self.stage,
            "failure_category": self.failure_category,
            "observations": dict(self.observations),
        }

    @staticmethod
    def _validate_document(document: Mapping[str, object]) -> None:
        if set(document) != set(OBSERVATION_KEYS) or document.get("schema") != 1:
            raise DiagnosticSchemaError("diagnostic snapshot keys are invalid")
        provenance = document.get("provenance")
        if not isinstance(provenance, Mapping) or set(provenance) != set(
            _PROVENANCE_KEYS
        ):
            raise DiagnosticSchemaError("diagnostic provenance keys are invalid")
        for name in ("branch_sha", "source_sha"):
            if not isinstance(provenance[name], str) or _SHA40.fullmatch(
                provenance[name]
            ) is None:
                raise DiagnosticSchemaError("diagnostic provenance is invalid")
        for name in ("original_gate_sha", "wheel_sha"):
            if not isinstance(provenance[name], str) or _SHA64.fullmatch(
                provenance[name]
            ) is None:
                raise DiagnosticSchemaError("diagnostic provenance is invalid")
        outcome = document.get("outcome")
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise DiagnosticSchemaError("diagnostic outcome is invalid")
        stage = document.get("stage")
        if not isinstance(stage, str) or stage not in _STAGES:
            raise DiagnosticSchemaError("diagnostic stage is invalid")
        category = document.get("failure_category")
        if not isinstance(category, str) or category not in _FAILURE_CATEGORIES:
            raise DiagnosticSchemaError("diagnostic failure category is invalid")
        observations = document.get("observations")
        if not isinstance(observations, Mapping) or set(observations) != set(
            _OBSERVATION_FIELDS
        ):
            raise DiagnosticSchemaError("diagnostic observation keys are invalid")
        for name in _OBSERVATION_FIELDS:
            value = observations[name]
            if name in {
                "bridge_state",
                "autopreview_result",
                "needs_restore_state",
                "pending_action_class",
            }:
                if not isinstance(value, str):
                    raise DiagnosticSchemaError("diagnostic enum is invalid")
            elif type(value) is not bool:
                raise DiagnosticSchemaError("diagnostic boolean is invalid")
        if observations["bridge_state"] not in _BRIDGE_STATES:
            raise DiagnosticSchemaError("diagnostic bridge state is invalid")
        if observations["autopreview_result"] not in _AUTOPREVIEW_RESULTS:
            raise DiagnosticSchemaError("diagnostic preview result is invalid")
        if observations["needs_restore_state"] not in {"unknown", "true", "false"}:
            raise DiagnosticSchemaError("diagnostic restore state is invalid")
        if observations["pending_action_class"] not in _PENDING_ACTION_CLASSES:
            raise DiagnosticSchemaError("diagnostic pending action class is invalid")

    def to_json(self) -> bytes:
        """Return canonical JSON bytes after validating every closed field."""
        document = self._document()
        self._validate_document(document)
        return (
            json.dumps(
                document,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def write(self) -> None:
        """Persist the latest snapshot, replacing only its own regular file."""
        if self.snapshot_path is None:
            return
        target = self.snapshot_path
        if target.is_symlink():
            raise DiagnosticSchemaError("diagnostic snapshot path is unsafe")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        if temporary.is_symlink():
            raise DiagnosticSchemaError("diagnostic temporary path is unsafe")
        temporary.write_bytes(self.to_json())
        os.replace(temporary, target)

    def record_stage(self, stage: str) -> None:
        """Record one fixed formal-gate stage after normalizing its name."""
        self.stage = _safe_stage(stage)
        self.write()

    def record_wait(self, label: str, success: bool) -> None:
        """Record a bounded wait and classify an expired predicate without text."""
        del label
        if not success:
            if self.failure_category == "none":
                self.failure_category = "wait_expired"
            self.outcome = "failed"
        self.write()

    def record_failure(self, category: str, *, stage: str | None = None) -> None:
        """Record a fixed failure class while discarding exception content."""
        self.failure_category = (
            category if category in _FAILURE_CATEGORIES else "observer_failure"
        )
        self.outcome = "failed"
        if stage is not None:
            self.stage = _safe_stage(stage)
        self.write()

    def finish(self, outcome: str, category: str = "none") -> None:
        """Set one fixed terminal outcome and persist it."""
        if outcome not in _OUTCOMES or category not in _FAILURE_CATEGORIES:
            raise DiagnosticSchemaError("diagnostic terminal state is invalid")
        self.outcome = outcome
        self.failure_category = category
        self.write()

    def forward_callback(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Forward one application callback transparently and classify failures."""
        try:
            return callback(*args, **kwargs)
        except BaseException:
            self.record_failure("callback_failure")
            raise

    def observe_click(self, *, signal_observed: bool = False) -> None:
        """Record an invoked click and, when seen, its clicked signal."""
        self.observations["click_invoked"] = True
        if signal_observed:
            self.observations["click_signal_observed"] = True
        self.write()

    def observe_dispatch(self, kind: str, accepted: bool) -> None:
        """Record only the fixed read and restore dispatch boundaries."""
        if kind == "read_io":
            self.observations["read_dispatch_returned"] = True
            self.observations["read_dispatch_accepted"] = accepted
            if not accepted and self.failure_category == "none":
                self.failure_category = "dispatch_rejected"
        elif kind == "restore_project":
            self.observations["restore_dispatch_requested"] = True
            self.observations["restore_dispatch_returned"] = True
            self.observations["restore_dispatch_accepted"] = accepted
            if not accepted and self.failure_category == "none":
                self.failure_category = "dispatch_rejected"
        self.write()

    def observe_dispatch_requested(self, kind: str) -> None:
        """Record entry into the restore dispatch boundary before it can raise."""
        if kind == "restore_project":
            self.observations["restore_dispatch_requested"] = True
            self.write()

    def observe_result(self, action: str | None, success: bool) -> None:
        """Record fixed result outcomes without retaining payloads or IDs."""
        if action == "signal_read_io":
            self.observations["read_result_observed"] = True
            self.observations["read_result_success"] = success
            if not success and self.failure_category == "none":
                self.failure_category = "result_failure"
        elif action == "restore_project":
            self.observations["restore_result_observed"] = True
            self.observations["restore_result_success"] = success
            if not success and self.failure_category == "none":
                self.failure_category = "result_failure"
            self._awaiting_restore_preview = success
            self.observations["autopreview_result"] = (
                "unknown" if success else "not_applicable"
            )
        elif action in {"preview", "signal_member_preview"}:
            if self._awaiting_restore_preview:
                self.observations["autopreview_result"] = (
                    "success" if success else "failure"
                )
                self._awaiting_restore_preview = False
        self.write()

    def observe_window(self, window: Any) -> None:
        """Read only fixed UI state booleans and enums from one live window."""
        try:
            bridge = window.bridge
            state = getattr(bridge.state, "value", bridge.state)
            self.observations["bridge_state"] = (
                state if state in _BRIDGE_STATES else "unknown"
            )
            worker = getattr(bridge, "worker_thread", None)
            running = getattr(worker, "isRunning", None)
            self.observations["worker_running"] = (
                bool(running()) if callable(running) else False
            )
            pending = getattr(window, "_pending_action", None)
            self.observations["pending_action_allowlisted"] = (
                pending in _PENDING_ACTIONS
            )
            if pending is None:
                pending_class = "none"
            elif pending in {"preview", "signal_member_preview"}:
                pending_class = "preview"
            elif pending in {
                "review_restore",
                "restore_project",
                "open_project",
                "close_project",
            }:
                pending_class = "restore"
            elif pending in {
                "signal_read_io",
                "signal_inspect_io",
                "signal_catalog_io",
                "open_inspect",
                "open_load",
            }:
                pending_class = "read"
            else:
                pending_class = "other"
            self.observations["pending_action_class"] = pending_class
            self.observations["command_reserved"] = bool(
                getattr(window, "_command_reserved", False)
            )
            self.observations["modal_active"] = bool(
                getattr(window, "_modal_active", False)
            )
            project = getattr(window, "project", None)
            status = getattr(window, "_workspace_status", {})
            objects = getattr(project, "objects", ())
            if self.stage == "io_read_settled" and objects:
                candidate = getattr(objects[-1], "object_id", None)
                if isinstance(candidate, str):
                    self._expected_object_id = candidate
            object_present = isinstance(self._expected_object_id, str) and any(
                getattr(item, "object_id", None) == self._expected_object_id
                for item in objects
            )
            resident_ids = (
                status.get("resident_object_ids", ())
                if isinstance(status, Mapping)
                else ()
            )
            resident = object_present and isinstance(
                self._expected_object_id, str
            ) and self._expected_object_id in resident_ids
            self.observations["object_present"] = object_present
            self.observations["object_resident"] = resident
            if isinstance(status, Mapping) and isinstance(
                status.get("needs_restore"), bool
            ):
                needs_restore = bool(status["needs_restore"])
                self.observations["needs_restore"] = needs_restore
                self.observations["needs_restore_state"] = (
                    "true" if needs_restore else "false"
                )
            panel = getattr(window, "open_data_panel", None)
            button = getattr(panel, "confirm_button", None)
            enabled = getattr(button, "isEnabled", None)
            self.observations["confirm_enabled"] = (
                bool(enabled()) if callable(enabled) else False
            )
        except BaseException:
            return
        self.write()

    def observe_preview_entered(self) -> None:
        """Record entry into the real preview display callback."""
        self.observations["preview_entered"] = True
        self.write()

    def observe_preview_completed(self) -> None:
        """Record successful return from the real preview display callback."""
        self.observations["preview_completed"] = True
        self.write()

    def observe_review_ok(self) -> None:
        """Record a Review modal that returned the required Ok result."""
        self.observations["review_modal_returned_ok"] = True
        self.write()


def parse_snapshot(payload: bytes) -> dict[str, object]:
    """Validate a snapshot and return only its strict JSON object."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticSchemaError("diagnostic snapshot is not JSON") from exc
    if not isinstance(document, dict):
        raise DiagnosticSchemaError("diagnostic snapshot is not an object")
    DiagnosticObservation._validate_document(document)
    return document


def classify_process_outcome(
    *, returncode: int | None, timed_out: bool
) -> tuple[str, str]:
    """Return fixed parent outcomes, preserving watchdog/process-exit distinction."""
    if timed_out:
        return "watchdog", "phase_watchdog"
    if returncode is None or returncode != 0:
        return "process_exit", "process_exit"
    return "passed", "none"


def _result_is_current(
    pending_command_id: object, result_command_id: object
) -> bool:
    """Match the production handler's stale-result acceptance condition."""
    return pending_command_id is None or result_command_id == pending_command_id


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise DiagnosticSchemaError("diagnostic source file is unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_gate(path: Path, expected_sha: str) -> Any:
    if _sha256(path) != expected_sha:
        raise DiagnosticSchemaError("original gate hash does not match")
    spec = importlib.util.spec_from_file_location("_original_trial_gate", path)
    if spec is None or spec.loader is None:
        raise DiagnosticSchemaError("original gate cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _QTestObserver:
    """Proxy QTest while leaving every call and argument unchanged."""

    def __init__(
        self,
        original: Any,
        observation: DiagnosticObservation,
        confirm_button: Callable[[], Any],
    ) -> None:
        self._original = original
        self._observation = observation
        self._confirm_button = confirm_button

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)

    def mouseClick(self, widget: Any, button: Any, *args: Any, **kwargs: Any) -> Any:
        if widget is not self._confirm_button():
            return self._original.mouseClick(widget, button, *args, **kwargs)
        signal_observed = False
        clicked = getattr(widget, "clicked", None)
        connect = getattr(clicked, "connect", None)
        if callable(connect):
            try:
                connect(
                    lambda *_values: self._observation.observe_click(
                        signal_observed=True
                    )
                )
            except BaseException:
                self._observation.record_failure("observer_failure")
        self._observation.observe_click(signal_observed=signal_observed)
        return self._observation.forward_callback(
            self._original.mouseClick, widget, button, *args, **kwargs
        )


def _install_in_memory_observers(
    gate: Any, observation: DiagnosticObservation
) -> Callable[[], None]:
    """Patch only imported application symbols and formal helper boundaries."""
    original_record_stage = gate._record_gate_stage
    original_wait = gate._wait
    original_review_dialog = gate._WorkspaceMessageBinding._review_dialog
    window_module = importlib.import_module("gwexpy_studio.ui.window")
    plot_module = importlib.import_module("gwexpy_studio.ui.plot_canvas")
    qt_test_module = importlib.import_module("PySide6.QtTest")
    window_type = window_module.MainWindow
    plot_type = plot_module.PlotCanvas
    original_window_init = window_type.__init__
    original_dispatch = window_type._dispatch_command
    original_result = window_type._on_bridge_result
    original_preview = plot_type.set_preview
    original_qtest = qt_test_module.QTest
    _latest_window: list[Any | None] = [None]

    def record_stage(work_root: Path, phase: str, stage: str) -> None:
        result = observation.forward_callback(
            original_record_stage, work_root, phase, stage
        )
        if phase == "normal":
            observation.record_stage(stage.replace("-", "_"))
            if _latest_window[0] is not None:
                observation.observe_window(_latest_window[0])
        return result

    def wait(app: Any, predicate: Any, label: str, timeout_s: float = 30.0) -> None:
        try:
            result = original_wait(app, predicate, label, timeout_s)
        except BaseException as error:
            timeout = isinstance(error, gate.GateError) and str(error).startswith(
                "technical-gate timed out during"
            )
            if timeout:
                observation.record_wait(label, False)
            else:
                observation.record_failure("callback_failure")
            if _latest_window[0] is not None:
                observation.observe_window(_latest_window[0])
            raise
        observation.record_wait(label, True)
        if _latest_window[0] is not None:
            observation.observe_window(_latest_window[0])
        return result

    def window_init(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = observation.forward_callback(
            original_window_init, self, *args, **kwargs
        )
        _latest_window[0] = self
        observation.observe_window(self)
        return result

    def dispatch(self: Any, kind: str, payload: dict[str, Any], **kwargs: Any) -> bool:
        observation.observe_dispatch_requested(kind)
        accepted = observation.forward_callback(
            original_dispatch, self, kind, payload, **kwargs
        )
        observation.observe_dispatch(kind, bool(accepted))
        observation.observe_window(self)
        return bool(accepted)

    def result(self: Any, bridge_result: Any) -> Any:
        action = getattr(self, "_pending_action", None)
        success = bool(getattr(bridge_result, "success", False))
        pending_command_id = getattr(self, "_pending_command_id", None)
        result_command_id = getattr(bridge_result, "command_id", None)
        if _result_is_current(pending_command_id, result_command_id):
            observation.observe_result(action, success)
        result_value = observation.forward_callback(
            original_result, self, bridge_result
        )
        observation.observe_window(self)
        return result_value

    def preview(self: Any, *args: Any, **kwargs: Any) -> Any:
        observation.observe_preview_entered()
        result_value = observation.forward_callback(
            original_preview, self, *args, **kwargs
        )
        observation.observe_preview_completed()
        return result_value

    def review_dialog(self: Any, *args: Any, **kwargs: Any) -> Any:
        result_value = observation.forward_callback(
            original_review_dialog, self, *args, **kwargs
        )
        from PySide6.QtWidgets import QMessageBox

        if result_value == QMessageBox.StandardButton.Ok:
            observation.observe_review_ok()
        return result_value

    gate._record_gate_stage = record_stage
    gate._wait = wait
    gate._WorkspaceMessageBinding._review_dialog = review_dialog
    window_type.__init__ = window_init
    window_type._dispatch_command = dispatch
    window_type._on_bridge_result = result
    plot_type.set_preview = preview
    qt_test_module.QTest = _QTestObserver(
        original_qtest,
        observation,
        lambda: (
            getattr(
                getattr(_latest_window[0], "open_data_panel", None),
                "confirm_button",
                None,
            )
            if _latest_window[0] is not None
            else None
        ),
    )

    def restore() -> None:
        gate._record_gate_stage = original_record_stage
        gate._wait = original_wait
        gate._WorkspaceMessageBinding._review_dialog = original_review_dialog
        window_type.__init__ = original_window_init
        window_type._dispatch_command = original_dispatch
        window_type._on_bridge_result = original_result
        plot_type.set_preview = original_preview
        qt_test_module.QTest = original_qtest

    return restore


def _run_child(arguments: argparse.Namespace) -> int:
    observation = DiagnosticObservation(
        branch_sha=arguments.branch_sha,
        original_gate_sha=arguments.original_gate_sha,
        source_sha=arguments.source_sha,
        wheel_sha=arguments.wheel_sha,
        snapshot_path=arguments.snapshot,
    )
    observation.write()
    restore: Callable[[], None] | None = None
    cleanup_failed = False
    gate: Any | None = None
    try:
        checkout = arguments.original_checkout.resolve(strict=True)
        gate_path = arguments.original_gate.resolve(strict=True)
        gate = _load_gate(gate_path, arguments.original_gate_sha)
        for name in ("cache", "config", "data", "state", "home", "mpl"):
            (arguments.work_root / name).mkdir(parents=True, exist_ok=True)
        inherited = gate.gate_environment(
            arguments.work_root,
            os.environ,
            gate._new_shm_run_prefix(),
            native_qt=arguments.native_qt,
        )
        os.environ.clear()
        os.environ.update(inherited)
        sys.path[:] = [
            item
            for item in sys.path
            if Path(item or os.curdir).resolve() not in {
                Path(__file__).resolve().parent,
                Path(__file__).resolve().parent.parent,
                checkout,
                checkout / "src",
            }
        ]
        restore = _install_in_memory_observers(gate, observation)
        gate._run_normal_launcher(checkout=checkout, work_root=arguments.work_root)
    except BaseException as error:
        if observation.failure_category == "none":
            if gate is not None and isinstance(error, gate.GateError):
                observation.record_failure("gate_failure")
            else:
                observation.record_failure("observer_failure")
        return 1
    finally:
        if restore is not None:
            try:
                restore()
            except BaseException:
                observation.record_failure("observer_failure")
                cleanup_failed = True
    if cleanup_failed:
        return 1
    observation.finish("passed", "none")
    return 0


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _finalize_parent_snapshot(
    path: Path,
    *,
    provenance: Mapping[str, str],
    outcome: str,
    category: str,
) -> None:
    try:
        document = parse_snapshot(path.read_bytes())
    except (OSError, DiagnosticSchemaError):
        document = {
            "schema": 1,
            "provenance": dict(provenance),
            "outcome": "running",
            "stage": "unknown",
            "failure_category": "none",
            "observations": _empty_observations(),
        }
    document["outcome"] = outcome
    document["failure_category"] = category
    checked = (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    DiagnosticObservation._validate_document(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise DiagnosticSchemaError("diagnostic snapshot path is unsafe")
    path.write_bytes(checked)


def _run_parent(arguments: argparse.Namespace) -> int:
    provenance = {
        "branch_sha": arguments.branch_sha,
        "original_gate_sha": arguments.original_gate_sha,
        "source_sha": arguments.source_sha,
        "wheel_sha": arguments.wheel_sha,
    }
    arguments.work_root.mkdir(parents=True, exist_ok=True)
    arguments.snapshot.parent.mkdir(parents=True, exist_ok=True)
    child_command = [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--child",
        "--branch-sha",
        arguments.branch_sha,
        "--original-gate-sha",
        arguments.original_gate_sha,
        "--source-sha",
        arguments.source_sha,
        "--wheel-sha",
        arguments.wheel_sha,
        "--original-checkout",
        str(arguments.original_checkout),
        "--original-gate",
        str(arguments.original_gate),
        "--work-root",
        str(arguments.work_root),
        "--snapshot",
        str(arguments.snapshot),
    ]
    if arguments.native_qt:
        child_command.append("--native-qt")
    try:
        process = subprocess.Popen(
            child_command,
            cwd=arguments.work_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": ""},
        )
    except OSError:
        _finalize_parent_snapshot(
            arguments.snapshot,
            provenance=provenance,
            outcome="process_exit",
            category="process_exit",
        )
        return 1
    timed_out = False
    try:
        process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        process.communicate()
    returncode = process.returncode
    if timed_out:
        outcome, category = classify_process_outcome(returncode=None, timed_out=True)
    elif returncode == 0:
        try:
            parse_snapshot(arguments.snapshot.read_bytes())
        except (OSError, DiagnosticSchemaError):
            outcome, category = "process_exit", "process_exit"
        else:
            outcome, category = "passed", "none"
    else:
        try:
            current = parse_snapshot(arguments.snapshot.read_bytes())
        except (OSError, DiagnosticSchemaError):
            outcome, category = classify_process_outcome(
                returncode=returncode, timed_out=False
            )
        else:
            current_category = str(current["failure_category"])
            if current_category == "none":
                outcome, category = classify_process_outcome(
                    returncode=returncode, timed_out=False
                )
            else:
                outcome, category = "process_exit", current_category
    if timed_out:
        try:
            current = parse_snapshot(arguments.snapshot.read_bytes())
        except (OSError, DiagnosticSchemaError):
            pass
        else:
            observed_category = str(current["failure_category"])
            if observed_category != "none":
                category = observed_category
    _finalize_parent_snapshot(
        arguments.snapshot,
        provenance=provenance,
        outcome=outcome,
        category=category,
    )
    return 0 if outcome == "passed" else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch-sha", required=True)
    parser.add_argument("--original-gate-sha", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--wheel-sha", required=True)
    parser.add_argument("--original-checkout", required=True, type=Path)
    parser.add_argument("--original-gate", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--native-qt", action="store_true")
    parser.add_argument(
        "--init",
        action="store_true",
        help="write a running snapshot before setup so upload remains bounded",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the parent watchdog or its direct observer child."""
    arguments = _parser().parse_args(argv)
    if arguments.init:
        DiagnosticObservation(
            branch_sha=arguments.branch_sha,
            original_gate_sha=arguments.original_gate_sha,
            source_sha=arguments.source_sha,
            wheel_sha=arguments.wheel_sha,
            snapshot_path=arguments.snapshot,
        ).write()
        return 0
    if arguments.child:
        return _run_child(arguments)
    return _run_parent(arguments)


if __name__ == "__main__":  # pragma: no cover - exercised by workflow.
    raise SystemExit(main())
