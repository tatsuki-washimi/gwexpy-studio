"""Contracts for the source-tree-independent installed-wheel technical gate.

A last-success prefix cannot distinguish a next-operation exception from a
failure to record its successor; the prefix only identifies the last confirmed
boundary.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _gate():
    return importlib.import_module("scripts.run_trial_technical_gate")


class _BindingScheduler:
    """Scheduler double exposing only the binding lifecycle contract."""

    diagnostic_selection_succeeded = True

    def __init__(self) -> None:
        self.bound: object | None = None
        self.error: BaseException | None = None
        self.stops = 0
        self.cleared = 0

    def bind(self, message: object) -> None:
        self.bound = message

    def retain_diagnostic(
        self,
        _message: object,
        _binding: object,
        *,
        on_poll: object,
        on_button: object,
    ) -> None:
        del on_poll, on_button

    def stop(self) -> None:
        self.stops += 1

    def clear_binding(self) -> None:
        self.cleared += 1


class _ReviewScheduler(_BindingScheduler):
    """Deterministic Review scheduler double for the two-phase contract."""

    def __init__(self) -> None:
        super().__init__()
        self.pre_dialog_started = False
        self.pre_dialog_timeout = None
        self.interaction_started = False
        self.pre_dialog_callback = None

    def start_pre_dialog(self, timeout_s: float, callback: object) -> None:
        self.pre_dialog_started = True
        self.pre_dialog_timeout = timeout_s
        self.pre_dialog_callback = callback

    def start_interaction(self) -> None:
        self.interaction_started = True
        self.handled = True


class _HostileStopScheduler(_BindingScheduler):
    """Scheduler double whose stop method must not abort cleanup."""

    def stop(self) -> None:
        self.stops += 1
        raise RuntimeError("hostile scheduler stop")


class _HostileReleaseScheduler(_BindingScheduler):
    """Scheduler double hostile to both stop lookup and binding release."""

    @property
    def stop(self) -> object:
        raise RuntimeError("hostile scheduler stop lookup")

    def clear_binding(self) -> None:
        self.cleared += 1
        raise RuntimeError("hostile scheduler release")


def _identity() -> dict[str, str]:
    return {
        "architecture": "x86_64",
        "build_id": "P-abcdef0-20260907-r1-a1",
        "python_version": "3.12.12",
        "source_sha": "abcdef0123456789abcdef0123456789abcdef01",
        "version": "0.1.0a1+trial.p.gabcdef0.20260907.r1.a1",
    }


def test_gate_qapplication_arguments_use_a_concrete_list() -> None:
    """Qt 6.11 must receive a concrete list rather than a tuple proxy."""
    arguments = _gate()._qapplication_arguments()

    assert type(arguments) is list
    assert arguments == [sys.argv[0]]


def test_gate_stage_record_is_canonical_and_path_free(tmp_path: Path) -> None:
    gate = _gate()

    gate._record_gate_stage(tmp_path, "producer", "crop-dialog")

    stage_path = tmp_path / "technical-gate-stage.json"
    assert stage_path.read_bytes() == (
        b'{"phase":"producer","stage":"crop-dialog"}\n'
    )
    assert gate.read_gate_stage(tmp_path) == "producer/crop-dialog"
    with pytest.raises(gate.GateError, match="stage"):
        gate._record_gate_stage(tmp_path, "producer", "not/a-stage")


def test_gate_recorder_and_capture_collector_share_the_complete_stage_allowlist(
    tmp_path: Path,
) -> None:
    gate = _gate()
    capture = importlib.import_module("scripts.capture_trial_resolution")

    assert gate._ALLOWED_GATE_STAGE_PAIRS == capture._ALLOWED_GATE_STAGE_PAIRS
    with pytest.raises(gate.GateError, match="stage"):
        gate._record_gate_stage(tmp_path, "consumer", "syntactically-valid")


def test_gate_stage_reader_rejects_a_syntactically_valid_unallowlisted_pair(
    tmp_path: Path,
) -> None:
    gate = _gate()
    (tmp_path / "technical-gate-stage.json").write_bytes(
        b'{"phase":"consumer","stage":"syntactically-valid"}\n'
    )

    with pytest.raises(gate.GateError, match="stage"):
        gate.read_gate_stage(tmp_path)


def test_gate_stage_reader_accepts_closed_normal_checkpoints_only(
    tmp_path: Path,
) -> None:
    gate = _gate()
    for stage in (
        "io-read-settled",
        "restore-review-handled",
        "restored-state-settled",
    ):
        gate._record_gate_stage(tmp_path, "normal", stage)
        assert gate.read_gate_stage(tmp_path) == f"normal/{stage}"

    (tmp_path / "technical-gate-stage.json").write_bytes(
        b'{"phase":"normal","stage":"io-read-settled-extra"}\n'
    )
    with pytest.raises(gate.GateError, match="stage"):
        gate.read_gate_stage(tmp_path)


def test_gate_waits_for_sample_catalog_before_requesting_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Try Sample cannot inspect until its asynchronous format catalog is ready."""
    gate = _gate()
    idle = object()
    bridge = SimpleNamespace(state=object())
    inspect_button = SimpleNamespace(enabled=False)
    inspect_button.isEnabled = lambda: inspect_button.enabled
    window = SimpleNamespace(bridge=bridge)
    panel = SimpleNamespace(inspect_button=inspect_button)

    def assert_catalog_predicate(
        _app: object, predicate: object, label: str, timeout_s: float = 30.0
    ) -> None:
        del timeout_s
        assert label == "sample catalog"
        assert not predicate()  # type: ignore[operator]
        bridge.state = idle
        assert not predicate()  # type: ignore[operator]
        inspect_button.enabled = True
        assert predicate()  # type: ignore[operator]

    monkeypatch.setattr(gate, "_wait", assert_catalog_predicate)

    gate._wait_for_sample_catalog(
        app=object(), window=window, panel=panel, idle_state=idle
    )


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 is unavailable",
)
def test_normal_io_read_waits_for_queued_preview_event() -> None:
    """A queued preview can arrive after the worker reports a resident object."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    gate = _gate()
    idle = object()
    previews: list[object] = []
    window = SimpleNamespace(
        bridge=SimpleNamespace(state=idle),
        project=SimpleNamespace(objects=[object()]),
        _workspace_status={"resident_object_ids": ["object-1"]},
    )

    old_completion = (
        window.bridge.state is idle
        and len(window.project.objects) == 1
        and bool(window._workspace_status.get("resident_object_ids"))
    )
    assert old_completion is True
    assert gate._normal_io_read_ready(
        window=window, idle_state=idle, previews=previews
    ) is False

    QTimer.singleShot(0, lambda: previews.append(object()))
    gate._wait(
        app,
        lambda: gate._normal_io_read_ready(
            window=window, idle_state=idle, previews=previews
        ),
        "queued preview completion",
        timeout_s=1.0,
    )


def test_preview_completion_is_recorded_after_canvas_accepts_it() -> None:
    gate = _gate()
    events: list[str] = []
    previews: list[object] = []

    def display(_preview: object, _spec: object) -> None:
        events.append("display")

    preview = object()
    gate._record_preview_after_display(
        previews=previews,
        display=display,
        preview=preview,
        spec=object(),
    )

    assert events == ["display"]
    assert previews == [preview]


def test_preview_completion_is_not_recorded_when_canvas_rejects_it() -> None:
    gate = _gate()
    previews: list[object] = []

    def display(_preview: object, _spec: object) -> None:
        raise RuntimeError("canvas rejected preview")

    with pytest.raises(RuntimeError, match="rejected"):
        gate._record_preview_after_display(
            previews=previews,
            display=display,
            preview=object(),
            spec=object(),
        )

    assert previews == []


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 is unavailable",
)
def test_normal_reopen_waits_for_queued_action_settlement() -> None:
    """A disabled Review action cannot dispatch during status/result settling."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    gate = _gate()
    idle = object()
    action = QAction("Review / Restore Data")
    action.setEnabled(False)
    triggered: list[bool] = []
    action.triggered.connect(lambda: triggered.append(True))
    window = SimpleNamespace(
        bridge=SimpleNamespace(state=idle),
        project=SimpleNamespace(objects=[object()]),
        _workspace_status={
            "project_path": "/tmp/normal-project.gwxproj",
            "needs_restore": True,
            "resident_object_ids": [],
        },
        _command_reserved=True,
        _modal_active=False,
        restore_project_action=action,
    )

    old_completion = (
        window.bridge.state is idle
        and len(window.project.objects) == 1
        and window._workspace_status.get("project_path")
        == "/tmp/normal-project.gwxproj"
        and window._workspace_status.get("needs_restore") is True
        and window._workspace_status.get("resident_object_ids") == []
    )
    assert old_completion is True
    action.trigger()
    assert triggered == []
    assert gate._normal_reopen_ready(
        window=window,
        idle_state=idle,
        project_path="/tmp/normal-project.gwxproj",
    ) is False

    def settle() -> None:
        window._command_reserved = False
        action.setEnabled(True)

    QTimer.singleShot(0, settle)
    gate._wait(
        app,
        lambda: gate._normal_reopen_ready(
            window=window,
            idle_state=idle,
            project_path="/tmp/normal-project.gwxproj",
        ),
        "queued restore action settlement",
        timeout_s=1.0,
    )
    action.trigger()
    assert triggered == [True]


def test_gate_selects_a_timeseries_before_the_post_asd_crop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery-changing Crop must not target the ASD FrequencySeries."""
    gate = _gate()
    idle = object()
    bridge = SimpleNamespace(state=idle)
    crop_action = SimpleNamespace(enabled=False)
    crop_action.isEnabled = lambda: crop_action.enabled
    selected: list[str] = []

    class Tree:
        def setCurrentItem(self, item: object) -> None:  # noqa: N802 - Qt API spelling
            selected.append(item.object_id)  # type: ignore[attr-defined]
            window._current_object_id = item.object_id  # type: ignore[attr-defined]
            crop_action.enabled = True

    objects = (
        SimpleNamespace(object_id="read", kind="TimeSeries"),
        SimpleNamespace(object_id="crop", kind="TimeSeries"),
        SimpleNamespace(object_id="asd", kind="FrequencySeries"),
    )
    window = SimpleNamespace(
        bridge=bridge,
        crop_action=crop_action,
        project=SimpleNamespace(objects=objects),
        source_tree=Tree(),
        _current_object_id="asd",
    )
    item = SimpleNamespace(object_id="crop")
    window._find_object_item = lambda _tree, object_id: (
        item if object_id == "crop" else None
    )

    def assert_selection_predicate(
        _app: object, predicate: object, label: str, timeout_s: float = 30.0
    ) -> None:
        del timeout_s
        assert label == "post-ASD Crop input"
        assert predicate()  # type: ignore[operator]

    monkeypatch.setattr(gate, "_wait", assert_selection_predicate)

    assert (
        gate._select_latest_timeseries_for_crop(
            app=object(), window=window, idle_state=idle
        )
        == "crop"
    )
    assert selected == ["crop"]


def test_installed_package_must_be_under_site_packages_and_outside_checkout(
    tmp_path: Path,
) -> None:
    """A gate fails closed if Studio resolves from P rather than the fresh venv."""
    checkout = tmp_path / "public-p"
    checkout_module = checkout / "src" / "gwexpy_studio" / "__init__.py"
    checkout_module.parent.mkdir(parents=True)
    checkout_module.write_text("", encoding="utf-8")
    purelib = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed_module = purelib / "gwexpy_studio" / "__init__.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("", encoding="utf-8")

    _gate().verify_installed_module_path(
        module_path=installed_module,
        site_roots=(purelib,),
        checkout_root=checkout,
    )
    with pytest.raises(_gate().GateError, match="checkout"):
        _gate().verify_installed_module_path(
            module_path=checkout_module,
            site_roots=(purelib,),
            checkout_root=checkout,
        )


def test_installed_package_ignores_absent_site_package_candidates(
    tmp_path: Path,
) -> None:
    """Ubuntu's venv may report absent dist-packages roots alongside purelib."""
    checkout = tmp_path / "public-p"
    checkout.mkdir()
    purelib = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed_module = purelib / "gwexpy_studio" / "__init__.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("", encoding="utf-8")
    absent_site_roots = (
        tmp_path / "venv" / "local" / "lib" / "python3.12" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3.12" / "dist-packages",
    )

    _gate().verify_installed_module_path(
        module_path=installed_module,
        site_roots=(purelib, *absent_site_roots),
        checkout_root=checkout,
    )


def test_installed_package_rejects_only_absent_site_package_candidates(
    tmp_path: Path,
) -> None:
    """Skipping Ubuntu's absent candidates must not permit an unrooted import."""
    checkout = tmp_path / "public-p"
    checkout.mkdir()
    foreign_module = tmp_path / "foreign" / "gwexpy_studio" / "__init__.py"
    foreign_module.parent.mkdir(parents=True)
    foreign_module.write_text("", encoding="utf-8")
    absent_site_roots = (
        tmp_path / "venv" / "local" / "lib" / "python3.12" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3" / "dist-packages",
        tmp_path / "venv" / "lib" / "python3.12" / "dist-packages",
    )

    with pytest.raises(_gate().GateError, match="site-packages"):
        _gate().verify_installed_module_path(
            module_path=foreign_module,
            site_roots=absent_site_roots,
            checkout_root=checkout,
        )


def test_gate_environment_isolated_and_result_is_bounded_path_free(
    tmp_path: Path,
) -> None:
    """The external Qt gate has its own roots and never emits host paths."""
    inherited = {
        "GWEXPY_STUDIO_IO_CAPABILITIES": "/private/forged-policy.json",
        "HOME": "/private/home",
        "PYTHONPATH": "/private/checkout/src",
        "PYTHONNOUSERSITE": "0",
        "PYTHONUSERBASE": "/private/userbase",
        "XDG_CACHE_HOME": "/old/cache",
    }

    environment = _gate().gate_environment(tmp_path, inherited, "trialgate-")
    encoded = (
        _gate()
        .gate_result_json(
            {
                "asd": True,
                "crop": True,
                "launcher_import": True,
                "project_reopen": True,
                "recovery": True,
                "save_project": True,
                "shared_memory_cleanup": True,
                "try_sample": True,
                "welcome": True,
                "worker_exit": True,
            },
            installed=_identity(),
        )
        .decode("utf-8")
    )

    assert "PYTHONPATH" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONUSERBASE" not in environment
    assert "GWEXPY_STUDIO_IO_CAPABILITIES" not in environment
    assert environment["QT_QPA_PLATFORM"] == "offscreen"
    assert environment["MPLBACKEND"] == "Agg"
    assert environment["GWEXPY_STUDIO_SHM_PREFIX"] == "trialgate-"
    assert str(tmp_path) not in encoded
    assert "/private" not in encoded
    result = json.loads(encoded)
    assert result == {
        "architecture": "x86_64",
        "checks": {
            "asd": True,
            "crop": True,
            "launcher_import": True,
            "project_reopen": True,
            "recovery": True,
            "save_project": True,
            "shared_memory_cleanup": True,
            "try_sample": True,
            "welcome": True,
            "worker_exit": True,
        },
        "installed": {
            "build_id": _identity()["build_id"],
            "source_sha": _identity()["source_sha"],
            "version": _identity()["version"],
        },
        "python_version": "3.12.12",
        "schema": 2,
        "status": "passed",
    }


def test_macos_gate_environment_keeps_native_cocoa_selection(tmp_path: Path) -> None:
    environment = _gate().gate_environment(
        tmp_path, {"QT_QPA_PLATFORM": "forged"}, "trialgate-", native_qt=True
    )

    assert "QT_QPA_PLATFORM" not in environment


def test_operation_dialog_acceptance_is_native_safe_and_bounded() -> None:
    """Cocoa dialogs use a direct button signal and cannot poll forever."""
    source = inspect.getsource(_gate()._click_dialog)

    assert "button.click()" in source
    assert "QTest.mouseClick" not in source
    assert "deadline_timer" in source
    assert "topLevelWidgets" in source
    assert "technical-gate operation dialog timed out" in source


def test_recovery_message_finds_the_visible_cocoa_dialog() -> None:
    """Cocoa may expose a visible message box without an active modal widget."""
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    dialog.show()
    app.processEvents()
    cocoa_like_app = SimpleNamespace(
        activeModalWidget=lambda: None,
        topLevelWidgets=lambda: [dialog],
    )

    scheduled = _gate()._schedule_message_box_button(
        app=cocoa_like_app,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
    )
    _gate()._wait(
        app,
        lambda: scheduled.handled or scheduled.error is not None,
        "recovery message click",
        timeout_s=1.0,
    )

    assert scheduled.error is None
    assert scheduled.handled is True
    dialog.close()
    owner.close()


def test_recovery_message_reports_visible_and_selected_boundaries() -> None:
    """A diagnostic stage can distinguish discovery from button activation."""
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    dialog.show()
    app.processEvents()
    cocoa_like_app = SimpleNamespace(
        activeModalWidget=lambda: None,
        topLevelWidgets=lambda: [dialog],
    )
    boundaries: list[str] = []

    scheduled = _gate()._schedule_message_box_button(
        app=cocoa_like_app,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
        on_visible=lambda: boundaries.append("visible"),
        on_selected=lambda: boundaries.append("selected"),
    )
    _gate()._wait(
        app,
        lambda: scheduled.handled or scheduled.error is not None,
        "recovery message diagnostics",
        timeout_s=1.0,
    )

    assert scheduled.error is None
    assert scheduled.handled is True
    assert boundaries == ["visible", "selected"]
    dialog.close()
    owner.close()


def test_workspace_dialog_binding_drives_message_without_global_discovery() -> None:
    """The gate binds the actual message passed to a native modal boundary."""
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    from gwexpy_studio.ui import workspace_window

    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    cocoa_like_app = SimpleNamespace(
        activeModalWidget=lambda: None,
        topLevelWidgets=lambda: [],
    )
    logical_window = SimpleNamespace(
        _modal_active=False,
        _update_command_state=lambda: None,
    )
    boundaries: list[str] = []
    original_workspace_dialog = workspace_window.workspace_dialog
    binding = _gate()._install_workspace_message_binding()
    scheduled = _gate()._schedule_message_box_button(
        app=cocoa_like_app,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
        binding=binding,
        on_visible=lambda: boundaries.append("visible"),
        on_selected=lambda: boundaries.append("selected"),
    )

    try:
        workspace_window.workspace_dialog(logical_window, dialog.exec)
    finally:
        binding.restore()

    assert scheduled.error is None
    assert scheduled.handled is True
    assert boundaries == ["visible", "selected"]
    assert workspace_window.workspace_dialog is original_workspace_dialog
    dialog.close()
    owner.close()


def _diagnostic_binding(
    *,
    record: object,
    original: object | None = None,
    message_type: type[object] | None = None,
) -> object:
    gate = _gate()
    module = SimpleNamespace(
        workspace_dialog=original
        or (lambda window, execute, *args, **kwargs: execute(*args, **kwargs))
    )
    return gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=message_type or object,
        record=record,
    )


def test_workspace_dialog_consumes_metadata_without_forwarding_it() -> None:
    from gwexpy_studio.ui.workspace_dialogs import workspace_dialog

    window = SimpleNamespace(
        _modal_active=False,
        _update_command_state=lambda: None,
    )
    seen: list[tuple[tuple[object, ...], dict[str, object], bool]] = []
    sentinel = object()
    dialog = object()

    def execute(*args: object, **kwargs: object) -> object:
        seen.append((args, kwargs, window._modal_active))
        return sentinel

    assert not hasattr(execute, "__self__")
    assert (
        workspace_dialog(window, execute, "payload", dialog_instance=dialog, key=3)
        is sentinel
    )
    assert seen == [(('payload',), {"key": 3}, True)]
    assert window._modal_active is False


def test_generic_review_binding_uses_explicit_instance_for_plain_callable() -> None:
    from gwexpy_studio.ui.workspace_dialogs import workspace_dialog

    gate = _gate()
    module = SimpleNamespace(workspace_dialog=workspace_dialog)
    stages: list[str] = []
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=stages.append,
    )
    scheduler = _BindingScheduler()
    binding.register("Review restoration", scheduler)
    updates: list[bool] = []
    window = SimpleNamespace(
        _modal_active=False,
        _update_command_state=lambda: updates.append(window._modal_active),
    )
    seen: list[tuple[tuple[object, ...], dict[str, object], bool]] = []
    sentinel = object()
    dialog = SimpleNamespace(windowTitle=lambda: "Review restoration")

    def execute(*args: object, **kwargs: object) -> object:
        seen.append((args, kwargs, window._modal_active))
        return sentinel

    assert not hasattr(execute, "__self__")
    assert binding.recovery_attempt == "unarmed"
    try:
        result = module.workspace_dialog(
            window,
            execute,
            "payload",
            dialog_instance=dialog,
            key=3,
        )
        assert result is sentinel
        assert scheduler.bound is dialog
        assert seen == [(('payload',), {"key": 3}, True)]
        assert window._modal_active is False
        assert updates == [True, False]
        assert binding.recovery_attempt == "unarmed"
        assert stages == []
    finally:
        binding.restore()

    assert module.workspace_dialog is workspace_dialog
    assert binding._scheduled == {}
    assert scheduler.cleared == 1


def test_review_scheduler_stays_dormant_until_matching_result() -> None:
    binding = _diagnostic_binding(record=lambda _stage: None)
    scheduler = _ReviewScheduler()

    binding.register_review(scheduler)
    assert scheduler.pre_dialog_started is True
    assert scheduler.pre_dialog_timeout > 15.0
    assert scheduler.interaction_started is False
    binding.correlate_review_dispatch("review-1")

    assert binding.arm_review(command_id="other") is False
    assert binding.review_attempt == "pending"
    assert binding.arm_review(command_id="review-1") is True
    assert binding.review_attempt == "armed"
    assert scheduler.interaction_started is False
    binding.restore()


def test_dedicated_review_path_has_no_legacy_widget_discovery() -> None:
    gate = _gate()
    source = "\n".join(
        (
            inspect.getsource(gate._schedule_review_confirmation),
            inspect.getsource(gate._WorkspaceMessageBinding._review_dialog),
        )
    )
    for forbidden in (
        "execute.__self__",
        "windowTitle",
        "activeModalWidget",
        "topLevelWidgets",
        ".text()",
    ):
        assert forbidden not in source
    assert "QMessageBox.StandardButton.Ok" in source


def test_review_terminal_tombstone_rejects_a_late_dialog_instance() -> None:
    binding = _diagnostic_binding(record=lambda _stage: None, message_type=object)
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    binding.correlate_review_dispatch("review-1")
    binding.fail_review()
    calls: list[str] = []

    result = binding._workspace_module.workspace_dialog(
        object(), lambda: calls.append("modal"), dialog_instance=object()
    )

    assert result is None
    assert calls == []
    assert binding.review_attempt == "tombstone"
    assert scheduler.error is binding._review_error
    binding.restore()


def test_review_pre_dialog_timeout_tombstones_before_late_instance() -> None:
    binding = _diagnostic_binding(record=lambda _stage: None, message_type=object)
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    assert scheduler.pre_dialog_callback is not None
    scheduler.pre_dialog_callback()
    calls: list[str] = []

    assert binding._workspace_module.workspace_dialog(
        object(), lambda: calls.append("modal"), dialog_instance=object()
    ) is None
    assert calls == []
    assert binding.review_attempt == "tombstone"
    assert scheduler.error is not None
    binding.restore()


def test_review_timeout_callback_after_arm_is_ignored() -> None:
    binding = _diagnostic_binding(record=lambda _stage: None, message_type=object)
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    binding.correlate_review_dispatch("review-1")
    assert binding.arm_review(command_id="review-1") is True

    binding._review_timeout()

    assert binding.review_attempt == "armed"
    assert scheduler.error is None
    binding.restore()


@pytest.mark.parametrize("instance", [None, object()], ids=["missing", "wrong-type"])
def test_dedicated_review_rejects_missing_or_wrong_instance(instance: object) -> None:
    binding = _diagnostic_binding(record=lambda _stage: None, message_type=str)
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    binding.correlate_review_dispatch("review-1")
    assert binding.arm_review(command_id="review-1") is True
    calls: list[str] = []

    try:
        assert binding._workspace_module.workspace_dialog(
            object(), lambda: calls.append("modal"), dialog_instance=instance
        ) is None
        assert calls == []
        assert scheduler.error is binding._review_error
    finally:
        binding.restore()


def test_dedicated_review_accepts_plain_callable_with_explicit_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()

    class FakeMessageBox:
        class StandardButton:
            Ok = object()

    fake_widgets = SimpleNamespace(QMessageBox=FakeMessageBox)
    monkeypatch.setitem(sys.modules, "PySide6", SimpleNamespace(QtWidgets=fake_widgets))
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", fake_widgets)
    module = SimpleNamespace(
        workspace_dialog=lambda _window, execute, *args, **kwargs: execute(
            *args, **kwargs
        )
    )
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=lambda _stage: None,
    )
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    binding.correlate_review_dispatch("review-1")
    assert binding.arm_review(command_id="review-1") is True
    sentinel = FakeMessageBox.StandardButton.Ok

    try:
        assert (
            module.workspace_dialog(
                object(), lambda: sentinel, dialog_instance=object()
            )
            is sentinel
        )
        assert binding.review_attempt == "consumed"
    finally:
        binding.restore()


def test_review_modal_exception_is_re_raised_after_fixed_cleanup(monkeypatch):
    gate = _gate()
    sentinel = RuntimeError("modal failure")

    class FakeMessageBox:
        class StandardButton:
            Ok = object()

    fake_widgets = SimpleNamespace(QMessageBox=FakeMessageBox)
    monkeypatch.setitem(sys.modules, "PySide6", SimpleNamespace(QtWidgets=fake_widgets))
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", fake_widgets)
    module = SimpleNamespace(
        workspace_dialog=lambda _window, _execute, *args, **kwargs: (
            (_ for _ in ()).throw(sentinel)
        )
    )
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=lambda _stage: None,
    )
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    binding.correlate_review_dispatch("review-1")
    assert binding.arm_review(command_id="review-1") is True

    try:
        with pytest.raises(RuntimeError) as caught:
            module.workspace_dialog(object(), lambda: None, dialog_instance=object())
        assert caught.value is sentinel
        assert binding.review_attempt == "tombstone"
        assert scheduler.error is binding._review_error
    finally:
        binding.restore()


def test_successful_review_stays_consumed_until_binding_restore(monkeypatch):
    gate = _gate()

    class FakeMessageBox:
        class StandardButton:
            Ok = object()

    fake_widgets = SimpleNamespace(QMessageBox=FakeMessageBox)
    monkeypatch.setitem(sys.modules, "PySide6", SimpleNamespace(QtWidgets=fake_widgets))
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", fake_widgets)
    calls: list[str] = []
    module = SimpleNamespace(
        workspace_dialog=lambda _window, execute, *args, **kwargs: execute(
            *args, **kwargs
        )
    )
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=lambda _stage: None,
    )
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    binding.correlate_review_dispatch("review-1")
    assert binding.arm_review(command_id="review-1") is True

    try:
        assert (
            module.workspace_dialog(
                object(),
                lambda: FakeMessageBox.StandardButton.Ok,
                dialog_instance=object(),
            )
            is FakeMessageBox.StandardButton.Ok
        )
        assert binding.review_attempt == "consumed"
        assert (
            module.workspace_dialog(
                object(), lambda: calls.append("late"), dialog_instance=object()
            )
            is None
        )
        assert calls == []
    finally:
        binding.restore()
    assert binding.review_attempt == "unarmed"


def test_duplicate_review_registration_fails_closed_and_cleans_both_schedulers(
) -> None:
    gate = _gate()
    binding = _diagnostic_binding(record=lambda _stage: None)
    first = _ReviewScheduler()
    second = _ReviewScheduler()
    binding.register_review(first)

    with pytest.raises(gate.GateError, match="review dialog binding"):
        binding.register_review(second)

    assert first.error is binding._review_error
    assert second.error is binding._review_error
    assert first.cleared == 1
    assert second.cleared == 1
    binding.restore()


def test_review_restore_callsite_passes_explicit_dialog_instance() -> None:
    source_path = Path(__file__).parents[2] / "src/gwexpy_studio/ui/workspace_window.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    review = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_review_restore_response"
    )
    calls = [
        node
        for node in ast.walk(review)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "workspace_dialog"
    ]

    assert len(calls) == 1
    assert any(
        keyword.arg == "dialog_instance"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "dialog"
        for keyword in calls[0].keywords
    )


def test_workspace_dialog_restores_modal_state_and_propagates_original_exception(
) -> None:
    from gwexpy_studio.ui.workspace_dialogs import workspace_dialog

    window = SimpleNamespace(
        _modal_active=True,
        _update_command_state=lambda: None,
    )
    sentinel = RuntimeError("original")

    def execute() -> None:
        assert window._modal_active is True
        raise sentinel

    with pytest.raises(RuntimeError) as caught:
        workspace_dialog(window, execute, dialog_instance=object())
    assert caught.value is sentinel
    assert window._modal_active is True


def test_armed_recovery_binds_explicit_instance_for_callable_without_self() -> None:
    stages: list[str] = []
    def original(
        _window: object,
        execute: object,
        *args: object,
        dialog_instance: object = None,
        **kwargs: object,
    ) -> object:
        del dialog_instance
        return execute(*args, **kwargs)  # type: ignore[operator]

    module = SimpleNamespace(workspace_dialog=original)
    gate = _gate()
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=stages.append,
    )
    scheduler = _BindingScheduler()
    binding.register_recovery(scheduler)
    binding.arm_recovery()
    sentinel = object()
    try:
        result = module.workspace_dialog(
            object(), lambda: sentinel, dialog_instance=object()
        )
        assert result is sentinel
        assert binding._recovery_modal_active is False
        assert binding.binding_error is None
        assert binding._diagnostic_message is None
        assert binding._diagnostic_scheduler is None
        assert binding._recovery_scheduler is None
        assert binding._recovery_expected_instance is binding._MISSING
        assert scheduler.cleared == 1
    finally:
        binding.restore()
    assert scheduler.bound is not None
    assert binding._recovery_scheduler is None
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-instance-bound",
        "consumer/recovery-dialog-modal-returned",
    ]


@pytest.mark.parametrize(
    "instance",
    [None, object()],
    ids=["missing-instance", "wrong-type"],
)
def test_armed_recovery_invalid_instance_stores_fixed_error_without_modal(
    instance: object,
) -> None:
    stages: list[str] = []
    calls: list[str] = []

    def original(*_args: object, **_kwargs: object) -> object:
        calls.append("modal")
        return object()

    module = SimpleNamespace(workspace_dialog=original)
    gate = _gate()
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=str,
        record=stages.append,
    )
    scheduler = _BindingScheduler()
    binding.register_recovery(scheduler)
    binding.arm_recovery()
    try:
        result = module.workspace_dialog(
            object(), lambda: object(), dialog_instance=instance
        )
        fixed_error = binding.error
    finally:
        binding.restore()
    assert result is None
    assert calls == []
    assert fixed_error is not None
    assert str(fixed_error) == "technical-gate recovery dialog binding failed"
    assert scheduler.error is fixed_error
    assert scheduler.stops >= 1
    assert stages == ["consumer/recovery-dialog-boundary-entered"]


def test_recovery_registration_rejects_duplicate_scheduler_slot() -> None:
    gate = _gate()
    binding = _diagnostic_binding(record=lambda _: None)
    first = _BindingScheduler()
    second = _BindingScheduler()
    binding.register_recovery(first)
    with pytest.raises(gate.GateError, match="binding"):
        binding.register_recovery(second)
    assert binding.error is not None
    assert second.error is binding.error
    binding.restore()


def test_armed_recovery_rejects_unrelated_explicit_instance() -> None:
    stages: list[str] = []
    expected = object()
    unrelated = object()
    def original(
        _window: object,
        execute: object,
        *args: object,
        dialog_instance: object = None,
        **kwargs: object,
    ) -> object:
        del dialog_instance
        return execute(*args, **kwargs)  # type: ignore[operator]

    module = SimpleNamespace(workspace_dialog=original)
    gate = _gate()
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=stages.append,
    )
    scheduler = _BindingScheduler()
    binding.register_recovery(scheduler, dialog_instance=expected)
    binding.arm_recovery()
    try:
        result = module.workspace_dialog(
            object(), lambda: "modal", dialog_instance=unrelated
        )
        fixed_error = binding.error
    finally:
        binding.restore()
    assert result is None
    assert fixed_error is not None
    assert scheduler.error is fixed_error
    assert stages == ["consumer/recovery-dialog-boundary-entered"]


def test_armed_recovery_rejects_reentrant_modal_entry() -> None:
    stages: list[str] = []
    message = object()
    module = SimpleNamespace(workspace_dialog=lambda *_args, **_kwargs: None)
    gate = _gate()
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=stages.append,
    )
    scheduler = _BindingScheduler()
    binding.register_recovery(scheduler)
    binding.arm_recovery()
    replacement = module.workspace_dialog

    def original(_window: object, _execute: object, **kwargs: object) -> object:
        return replacement(object(), lambda: "nested", **kwargs)

    binding._original = original
    try:
        result = replacement(
            object(), lambda: "outer", dialog_instance=message
        )
        fixed_error = binding.error
    finally:
        binding.restore()
    assert result is None
    assert fixed_error is not None
    assert scheduler.error is fixed_error
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-instance-bound",
    ]


def test_armed_recovery_recorder_reentry_fails_before_modal_entry() -> None:
    stages: list[str] = []
    calls: list[str] = []
    message = object()
    module = SimpleNamespace(workspace_dialog=lambda *_args, **_kwargs: None)
    gate = _gate()

    def record(stage: str) -> None:
        stages.append(stage)
        if stage == "consumer/recovery-dialog-boundary-entered":
            module.workspace_dialog(
                object(), lambda: calls.append("nested"), dialog_instance=message
            )

    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=object,
        record=record,
    )
    scheduler = _BindingScheduler()
    binding.register_recovery(scheduler)
    binding.arm_recovery()

    def original(*_args: object, **_kwargs: object) -> object:
        calls.append("modal")
        return object()

    binding._original = original
    replacement = module.workspace_dialog
    try:
        result = replacement(object(), lambda: None, dialog_instance=message)
        fixed_error = binding.error
    finally:
        binding.restore()

    assert result is None
    assert calls == []
    assert fixed_error is not None
    assert str(fixed_error) == "technical-gate recovery dialog binding failed"
    assert scheduler.error is fixed_error


def test_armed_recovery_live_modal_reentry_rejects_bound_dialog_without_timeout(
) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    module = SimpleNamespace(
        workspace_dialog=lambda _window, execute, *args, **kwargs: execute(
            *args, **kwargs
        )
    )
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=QMessageBox,
        record=lambda _stage: None,
    )
    replacement = module.workspace_dialog

    def original(
        _window: object,
        execute: object,
        *args: object,
        dialog_instance: object = None,
        **kwargs: object,
    ) -> object:
        del dialog_instance
        QTimer.singleShot(
            0,
            lambda: replacement(object(), lambda: None, dialog_instance=dialog),
        )
        return execute(*args, **kwargs)  # type: ignore[operator]

    binding._original = original
    scheduler = gate._schedule_message_box_button(
        app=_app,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=1.0,
        binding=binding,
        recovery=True,
    )
    binding.arm_recovery()
    started = time.monotonic()
    try:
        replacement(object(), dialog.exec, dialog_instance=dialog)
        fixed_error = binding.error
    finally:
        binding.restore()
        dialog.close()
        owner.close()

    assert time.monotonic() - started < 0.5
    assert fixed_error is not None
    assert str(fixed_error) == "technical-gate recovery dialog binding failed"
    assert scheduler.error is fixed_error
    assert not dialog.isVisible()


def test_terminal_failure_continues_after_hostile_scheduler_stop() -> None:
    binding = _diagnostic_binding(record=lambda _: None)
    healthy = _BindingScheduler()
    hostile = _HostileStopScheduler()
    binding.register_recovery(healthy)

    fixed_error = binding._terminal_failure(hostile)

    assert str(fixed_error) == "technical-gate recovery dialog binding failed"
    assert hostile.stops == 1
    assert healthy.stops == 1
    assert healthy.error is fixed_error
    binding.restore()


def test_terminal_failure_continues_after_hostile_scheduler_stop_lookup() -> None:
    binding = _diagnostic_binding(record=lambda _: None)
    healthy = _BindingScheduler()
    hostile = _HostileReleaseScheduler()
    binding.register_recovery(healthy)

    fixed_error = binding._terminal_failure(hostile)

    assert str(fixed_error) == "technical-gate recovery dialog binding failed"
    assert hostile.error is fixed_error
    assert healthy.stops == 1
    binding.restore()


def test_invalid_recovery_instance_clears_refs_when_release_is_hostile() -> None:
    gate = _gate()
    module = SimpleNamespace(
        workspace_dialog=lambda _window, _execute, **_kwargs: object()
    )
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=str,
        record=lambda _stage: None,
    )
    scheduler = _HostileReleaseScheduler()
    binding.register_recovery(scheduler)
    binding.arm_recovery()

    try:
        result = module.workspace_dialog(
            object(), lambda: object(), dialog_instance=object()
        )
        fixed_error = binding.error
    finally:
        binding.restore()

    assert result is None
    assert fixed_error is not None
    assert str(fixed_error) == "technical-gate recovery dialog binding failed"
    assert scheduler.error is fixed_error
    assert binding._recovery_scheduler is None
    assert binding._diagnostic_scheduler is None
    assert binding._diagnostic_message is None
    assert binding._recovery_expected_instance is binding._MISSING


def test_workspace_dialog_binding_emits_fixed_recovery_stages_in_success_order(
) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    app_calls: list[str] = []
    app_like = SimpleNamespace(
        activeModalWidget=lambda: app_calls.append("active") or None,
        topLevelWidgets=lambda: app_calls.append("top-level") or [],
    )
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    binding.arm_recovery()
    scheduled = gate._schedule_message_box_button(
        app=app_like,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.5,
        binding=binding,
        recovery=True,
        on_visible=lambda: stages.append("existing-visible"),
        on_selected=lambda: stages.append("existing-selected"),
    )

    try:
        binding._workspace_module.workspace_dialog(
            object(), dialog.exec, dialog_instance=dialog
        )
    finally:
        binding.restore()
        dialog.close()
        owner.close()

    assert scheduled.error is None
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-instance-bound",
        "consumer/recovery-dialog-poll-entered",
        "existing-visible",
        "consumer/recovery-dialog-button-resolved",
        "existing-selected",
        "consumer/recovery-dialog-modal-returned",
    ]
    assert app_calls == []


def test_workspace_dialog_binding_unarmed_arbitrary_callable_emits_no_new_stages(
) -> None:
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append)
    sentinel = object()

    assert (
        binding._workspace_module.workspace_dialog(object(), lambda: sentinel)
        is sentinel
    )
    assert stages == []


def test_workspace_dialog_binding_armed_missing_instance_is_single_use() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    app_like = SimpleNamespace(
        activeModalWidget=lambda: None, topLevelWidgets=lambda: []
    )
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    binding.arm_recovery()
    scheduled = gate._schedule_message_box_button(
        app=app_like,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.5,
        binding=binding,
        recovery=True,
    )
    try:
        result = binding._workspace_module.workspace_dialog(
            object(), dialog.exec
        )
        fixed_error = binding.error
    finally:
        binding.restore()
        dialog.close()
        owner.close()
    assert result is None
    assert fixed_error is not None
    assert scheduled.error is fixed_error
    assert stages == ["consumer/recovery-dialog-boundary-entered"]


def test_workspace_dialog_binding_unarmed_different_titles_keep_generic_path() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    wrong = QMessageBox(owner)
    wrong.setWindowTitle("Other dialog")
    wrong.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
    right = QMessageBox(owner)
    right.setWindowTitle("Recover unfinished work")
    right.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    app_like = SimpleNamespace(
        activeModalWidget=lambda: None, topLevelWidgets=lambda: []
    )
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    wrong_schedule = gate._schedule_message_box_button(
        app=app_like,
        owner=owner,
        label="OK",
        title="Other dialog",
        required=True,
        timeout_s=0.5,
        binding=binding,
    )
    right_schedule = gate._schedule_message_box_button(
        app=app_like,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.5,
        binding=binding,
    )
    try:
        binding._workspace_module.workspace_dialog(object(), wrong.exec)
        binding._workspace_module.workspace_dialog(object(), right.exec)
    finally:
        binding.restore()
        wrong.close()
        right.close()
        owner.close()
    assert wrong_schedule.error is None
    assert right_schedule.error is None
    assert stages == []


def test_workspace_dialog_binding_armed_without_scheduler_fails_closed(
) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    binding.arm_recovery()
    try:
        result = binding._workspace_module.workspace_dialog(
            object(), dialog.reject, dialog_instance=dialog
        )
        fixed_error = binding.error
    finally:
        binding.restore()
        dialog.close()
        owner.close()
    assert result is None
    assert fixed_error is not None
    assert stages == ["consumer/recovery-dialog-boundary-entered"]


def test_workspace_dialog_binding_unarmed_registered_dialogs_keep_existing_click_path(
) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    dialogs = (
        ("Recover unfinished work", "Restore"),
        ("Other dialog", "OK"),
    )
    scheduled: list[object] = []
    try:
        for title, label in dialogs:
            dialog = QMessageBox(owner)
            dialog.setWindowTitle(title)
            dialog.addButton(label, QMessageBox.ButtonRole.AcceptRole)
            state = gate._schedule_message_box_button(
                app=SimpleNamespace(
                    activeModalWidget=lambda: None,
                    topLevelWidgets=lambda: [],
                ),
                owner=owner,
                label=label,
                title=title,
                required=True,
                timeout_s=0.5,
                binding=binding,
            )
            scheduled.append((state, dialog))
            binding._workspace_module.workspace_dialog(object(), dialog.exec)
    finally:
        binding.restore()
        for _, dialog in scheduled:
            dialog.close()
        owner.close()
    assert all(state.error is None and state.handled for state, _ in scheduled)
    assert stages == []


def test_workspace_dialog_binding_fallback_discovery_never_runs_diagnostic_callbacks(
) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    calls: list[str] = []
    dialog.show()
    _app.processEvents()
    state = gate._schedule_message_box_button(
        app=SimpleNamespace(
            activeModalWidget=lambda: None, topLevelWidgets=lambda: [dialog]
        ),
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.5,
        on_diagnostic_poll=lambda: calls.append("poll"),
        on_diagnostic_button=lambda: calls.append("button"),
    )
    gate._wait(
        _app,
        lambda: state.handled or state.error is not None,
        "fallback",
        timeout_s=1,
    )
    dialog.close()
    owner.close()
    assert state.error is None
    assert calls == []


def test_workspace_dialog_binding_bound_dialog_stays_authoritative_when_not_visible(
) -> None:
    from PySide6.QtWidgets import QApplication, QWidget

    gate = _gate()
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    fallback_calls: list[str] = []

    class Button:
        def __init__(self) -> None:
            self.clicked = False

        def text(self) -> str:
            return "Restore"

        def click(self) -> None:
            self.clicked = True

    class BoundMessage:
        def __init__(self) -> None:
            self.button = Button()
            self.rejected = False

        def isVisible(self) -> bool:  # noqa: N802 - Qt API spelling
            return False

        def buttons(self) -> list[Button]:
            return [self.button]

        def reject(self) -> None:
            self.rejected = True

    message = BoundMessage()
    state = gate._schedule_message_box_button(
        app=SimpleNamespace(
            activeModalWidget=lambda: None,
            topLevelWidgets=lambda: fallback_calls.append("fallback") or [],
        ),
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
    )
    state.bind(message)
    gate._wait(
        app,
        lambda: state.handled or state.error is not None,
        "bound dialog authority",
        timeout_s=1,
    )
    owner.close()
    assert state.error is None
    assert state.handled is True
    assert message.button.clicked is True
    assert fallback_calls == []


def test_message_scheduler_fails_closed_when_qt_owner_is_deleted() -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QWidget

    gate = _gate()
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    binding = _diagnostic_binding(record=lambda _: None)
    state = gate._schedule_message_box_button(
        app=SimpleNamespace(activeModalWidget=lambda: None, topLevelWidgets=lambda: []),
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=1,
        binding=binding,
    )
    owner.deleteLater()
    app.sendPostedEvents(owner, QEvent.Type.DeferredDelete)
    app.processEvents()
    binding.restore()
    assert state.error is not None
    assert state._on_visible is None
    assert state._on_selected is None


def test_workspace_dialog_binding_missing_button_never_emits_modal_return() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    binding.arm_recovery()
    scheduled = gate._schedule_message_box_button(
        app=SimpleNamespace(
            activeModalWidget=lambda: None, topLevelWidgets=lambda: []
        ),
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
        binding=binding,
        recovery=True,
    )
    try:
        binding._workspace_module.workspace_dialog(
            object(), dialog.exec, dialog_instance=dialog
        )
    finally:
        binding.restore()
        dialog.close()
        owner.close()
    assert scheduled.error is not None
    assert "consumer/recovery-dialog-button-resolved" not in stages
    assert "consumer/recovery-dialog-modal-returned" not in stages


def test_workspace_dialog_binding_selected_callback_failure_never_emits_modal_return(
) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    stages: list[str] = []
    sentinel = RuntimeError("selected callback")
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    binding.arm_recovery()
    scheduled = gate._schedule_message_box_button(
        app=SimpleNamespace(
            activeModalWidget=lambda: None, topLevelWidgets=lambda: []
        ),
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.5,
        binding=binding,
        recovery=True,
        on_selected=lambda: (_ for _ in ()).throw(sentinel),
    )
    try:
        binding._workspace_module.workspace_dialog(
            object(), dialog.exec, dialog_instance=dialog
        )
    finally:
        binding.restore()
        dialog.close()
        owner.close()
    assert scheduled.error is sentinel
    assert "consumer/recovery-dialog-modal-returned" not in stages


def test_workspace_dialog_binding_other_scheduler_never_emits_modal_return(
) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append, message_type=QMessageBox)
    binding.arm_recovery()
    scheduled = gate._schedule_message_box_button(
        app=SimpleNamespace(
            activeModalWidget=lambda: None, topLevelWidgets=lambda: []
        ),
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.2,
        binding=binding,
        recovery=True,
    )
    QTimer.singleShot(1, dialog.reject)
    try:
        binding._workspace_module.workspace_dialog(
            object(), dialog.exec, dialog_instance=dialog
        )
    finally:
        binding.restore()
        dialog.close()
        owner.close()
    assert scheduled.error is not None
    assert "consumer/recovery-dialog-modal-returned" not in stages


def test_recovery_dispatch_diagnostics_arm_only_nonempty_success_before_delegation(
) -> None:
    gate = _gate()
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append)
    calls: list[str] = []
    armed_at_delegate: list[bool] = []

    def handle(value: object) -> object:
        calls.append("delegate")
        armed_at_delegate.append(binding.recovery_armed)
        return value

    window = SimpleNamespace(
        _dispatch_command=lambda *_args, **_kwargs: True,
        _handle_workspace_result=handle,
        _pending_action="list_recoveries",
    )
    gate._instrument_recovery_boundaries(
        window=window, record=lambda _: None, message_binding=binding
    )
    for result in (
        SimpleNamespace(success=False, payload={}),
        SimpleNamespace(success=True, payload={"recoveries": []}),
    ):
        window._handle_workspace_result(result)
        assert not binding.recovery_armed
    result = SimpleNamespace(success=True, payload={"recoveries": [{"run_id": "r1"}]})
    assert window._handle_workspace_result(result) is result
    assert calls == ["delegate", "delegate", "delegate"]
    assert armed_at_delegate == [False, False, True]
    assert binding.recovery_armed


def test_workspace_dialog_binding_diagnostic_stages_are_idempotent_and_single_use(
) -> None:
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append)
    binding.arm_recovery()
    binding.arm_recovery()
    binding._record_diagnostic_stage("consumer/recovery-dialog-boundary-entered")
    binding._record_diagnostic_stage("consumer/recovery-dialog-boundary-entered")
    assert stages == ["consumer/recovery-dialog-boundary-entered"]
    assert binding.recovery_armed
    assert binding.recovery_attempt == "armed"


def test_workspace_dialog_binding_preserves_positional_forwarding_and_return_identity(
) -> None:
    seen: list[tuple[object, ...]] = []
    sentinel = object()

    def original(
        window: object, execute: object, *args: object, **kwargs: object
    ) -> object:
        seen.extend((window, execute, *args, kwargs))
        return sentinel

    binding = _diagnostic_binding(record=lambda _: None, original=original)
    assert (
        binding._workspace_module.workspace_dialog("window", "execute", 1, 2, key=3)
        is sentinel
    )
    assert seen == ["window", "execute", 1, 2, {"key": 3}]


def test_workspace_dialog_binding_preserves_sentinel_exception_without_modal_return(
) -> None:
    sentinel = RuntimeError("sentinel")
    stages: list[str] = []

    def original(*_args: object, **_kwargs: object) -> object:
        raise sentinel

    binding = _diagnostic_binding(record=stages.append, original=original)
    with pytest.raises(RuntimeError) as caught:
        binding._workspace_module.workspace_dialog(object(), lambda: None)
    assert caught.value is sentinel
    assert "consumer/recovery-dialog-modal-returned" not in stages


def test_workspace_dialog_binding_rejects_unknown_stage_before_recording() -> None:
    recorded: list[str] = []
    binding = _diagnostic_binding(record=recorded.append)
    with pytest.raises(_gate().GateError, match="stage"):
        binding._record_diagnostic_stage("consumer/recovery-dialog-unknown")
    assert recorded == []


def test_workspace_dialog_binding_recorder_failure_hides_recorder_text(
) -> None:
    hostile = "/private/hostile-recorder-output"

    def record(_stage: str) -> None:
        raise RuntimeError(hostile)

    binding = _diagnostic_binding(record=record)
    binding.arm_recovery()
    assert binding._workspace_module.workspace_dialog(object(), lambda: None) is None
    assert binding.error is not None
    assert hostile not in str(binding.error)


def test_workspace_dialog_binding_restore_clears_attempt_registrations_and_references(
) -> None:
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append)
    binding.arm_recovery()
    binding._record_diagnostic_stage("consumer/recovery-dialog-boundary-entered")
    binding._diagnostic_message = object()
    binding.register("Recover unfinished work", object())
    binding.restore()
    assert not binding.recovery_armed
    assert binding.recovery_attempt == "unarmed"
    assert binding._scheduled == {}
    assert binding._diagnostic_message is None
    assert binding._diagnostic_scheduler is None


def test_workspace_dialog_binding_restore_rearm_cannot_reemit_stage() -> None:
    stages: list[str] = []
    binding = _diagnostic_binding(record=stages.append)
    binding.arm_recovery()
    binding._record_diagnostic_stage("consumer/recovery-dialog-boundary-entered")
    binding._diagnostic_message = object()
    binding.register("Recover unfinished work", object())

    binding.restore()
    assert binding.recovery_attempt == "unarmed"
    assert binding._scheduled == {}
    assert binding._diagnostic_message is None
    assert binding._diagnostic_scheduler is None

    binding.arm_recovery()
    binding._record_diagnostic_stage("consumer/recovery-dialog-boundary-entered")
    assert stages == ["consumer/recovery-dialog-boundary-entered"]


def test_workspace_dialog_binding_source_declares_exact_fixed_stages_and_record_wiring(
) -> None:
    gate = _gate()
    stages = gate._RECOVERY_DIALOG_DIAGNOSTIC_STAGES
    assert stages == {
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-instance-bound",
        "consumer/recovery-dialog-poll-entered",
        "consumer/recovery-dialog-button-resolved",
        "consumer/recovery-dialog-modal-returned",
    }
    source = inspect.getsource(gate._WorkspaceMessageBinding)
    assert "record=" in source
    assert "message_binding=" in inspect.getsource(gate._instrument_recovery_boundaries)


def test_review_diagnostics_are_not_public_gate_stages() -> None:
    gate = _gate()
    capture = importlib.import_module("scripts.capture_trial_resolution")

    assert not hasattr(gate, "_DATA_RESTORE_DIAGNOSTIC_STAGES")
    for stage in (
        "recovery-binding-isolation-confirmed",
        "review-restore-dispatch-accepted",
        "review-restore-result-succeeded",
        "review-dialog-scheduler-bound",
        "review-dialog-modal-returned",
        "restore-project-dispatch-accepted",
        "restore-project-settled",
    ):
        assert f"consumer/{stage}" not in gate._ALLOWED_GATE_STAGE_PAIRS
        assert f"consumer/{stage}" not in capture._ALLOWED_GATE_STAGE_PAIRS


def test_review_dispatch_arms_before_application_handler() -> None:
    gate = _gate()
    binding = _diagnostic_binding(record=lambda _stage: None)
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    observed: list[str] = []

    def dispatch(
        _kind: str,
        _payload: dict[str, object],
        *,
        pending_action: str,
        pending_params: dict[str, object] | None = None,
    ) -> bool:
        del pending_action, pending_params
        window._pending_command_id = "review-1"
        return True

    def handle(_result: object) -> object:
        observed.append(binding.review_attempt)
        return "handled"

    window = SimpleNamespace(
        _dispatch_command=dispatch,
        _handle_workspace_result=handle,
        _pending_action="review_restore",
    )
    restore = gate._instrument_recovery_boundaries(
        window=window,
        record=lambda _stage: None,
        message_binding=binding,
    )
    try:
        assert window._dispatch_command(
            "review_restore", {}, pending_action="review_restore"
        )
        result = SimpleNamespace(success=True, payload={}, command_id="review-1")
        assert window._handle_workspace_result(result) == "handled"
    finally:
        restore()
        binding.restore()

    assert observed == ["armed"]


def test_review_handler_failure_before_dialog_tombstones_and_reraises() -> None:
    gate = _gate()
    binding = _diagnostic_binding(record=lambda _stage: None)
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    observed = RuntimeError("handler before dialog")
    window = SimpleNamespace(_pending_action="review_restore")

    def dispatch(
        _kind: str,
        _payload: dict[str, object],
        *,
        pending_action: str,
        pending_params: dict[str, object] | None = None,
    ) -> bool:
        del pending_action, pending_params
        window._pending_command_id = "review-1"
        return True

    def handle(_result: object) -> object:
        raise observed

    window._dispatch_command = dispatch
    window._handle_workspace_result = handle
    restore = gate._instrument_recovery_boundaries(
        window=window,
        record=lambda _stage: None,
        message_binding=binding,
    )
    try:
        assert window._dispatch_command(
            "review_restore", {}, pending_action="review_restore"
        )
        with pytest.raises(RuntimeError) as caught:
            window._handle_workspace_result(
                SimpleNamespace(success=True, payload={}, command_id="review-1")
            )
        assert caught.value is observed
        fixed_error = binding.error
    finally:
        restore()
        binding.restore()
    assert scheduler.error is not None
    assert str(fixed_error) == "technical-gate review dialog binding failed"


def test_review_handler_failure_after_dialog_tombstones_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate()

    class FakeMessageBox:
        class StandardButton:
            Ok = object()

    fake_widgets = SimpleNamespace(QMessageBox=FakeMessageBox)
    monkeypatch.setitem(sys.modules, "PySide6", SimpleNamespace(QtWidgets=fake_widgets))
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", fake_widgets)
    binding = _diagnostic_binding(record=lambda _stage: None)
    scheduler = _ReviewScheduler()
    binding.register_review(scheduler)
    observed = RuntimeError("handler after dialog")
    window = SimpleNamespace(_pending_action="review_restore")

    def dispatch(
        _kind: str,
        _payload: dict[str, object],
        *,
        pending_action: str,
        pending_params: dict[str, object] | None = None,
    ) -> bool:
        del pending_action, pending_params
        window._pending_command_id = "review-1"
        return True

    def handle(_result: object) -> object:
        binding._workspace_module.workspace_dialog(
            object(), lambda: FakeMessageBox.StandardButton.Ok, dialog_instance=object()
        )
        raise observed

    window._dispatch_command = dispatch
    window._handle_workspace_result = handle
    restore = gate._instrument_recovery_boundaries(
        window=window,
        record=lambda _stage: None,
        message_binding=binding,
    )
    try:
        assert window._dispatch_command(
            "review_restore", {}, pending_action="review_restore"
        )
        with pytest.raises(RuntimeError) as caught:
            window._handle_workspace_result(
                SimpleNamespace(success=True, payload={}, command_id="review-1")
            )
        assert caught.value is observed
        fixed_error = binding.error
    finally:
        restore()
        binding.restore()
    assert str(fixed_error) == "technical-gate review dialog binding failed"


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 is unavailable",
)
def test_real_review_scheduler_binds_ok_and_releases_every_reference() -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    module = SimpleNamespace(
        workspace_dialog=lambda _window, execute, *args, **kwargs: execute(
            *args, **kwargs
        )
    )
    binding = gate._WorkspaceMessageBinding(
        workspace_module=module,
        message_type=QMessageBox,
        record=lambda _stage: None,
    )
    state = gate._schedule_review_confirmation(
        app=app,
        owner=owner,
        required=True,
        binding=binding,
        pre_dialog_timeout_s=95.0,
    )
    dialog = QMessageBox(owner)
    dialog.setStandardButtons(
        QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
    )
    try:
        assert state._poll_timer.isActive() is False
        assert state._deadline_timer.isActive() is False
        assert state._pre_dialog_timer.isActive() is True
        binding.correlate_review_dispatch("review-1")
        assert binding.arm_review(command_id="review-1") is True
        binding._review_timeout()
        assert binding.review_attempt == "armed"
        assert state._pre_dialog_timer.isActive() is False
        result = module.workspace_dialog(
            owner,
            dialog.exec,
            dialog_instance=dialog,
        )
        assert result == QMessageBox.StandardButton.Ok
        assert state.handled is True
        assert state._timers_released is True
        assert state.bound_message is None
        assert state._on_visible is None
        assert state._on_selected is None
        assert binding._review_scheduler is None
        assert binding.review_attempt == "consumed"
    finally:
        binding.restore()
        dialog.close()
        owner.close()
        app.processEvents()


def test_consumer_waits_for_review_before_restore_settlement() -> None:
    source = inspect.getsource(_gate()._run_consumer_launcher)

    assert source.index('"review dialog"') < source.index('"reviewed data restore"')


def test_consumer_recovery_diagnostics_cover_each_modal_boundary() -> None:
    source = inspect.getsource(_gate()._run_consumer_launcher)

    for stage in (
        "recovery-review-trigger",
        "recovery-review-requested",
        "recovery-candidate-visible",
        "recovery-candidate-selected",
        "unsaved-project-visible",
        "unsaved-project-discarded",
        "recovery-restored",
    ):
        assert f'"{stage}"' in source


def test_recovery_dispatch_diagnostics_preserve_accepted_command_and_result(
) -> None:
    gate = _gate()
    delegated: list[tuple[object, ...]] = []
    stages: list[str] = []
    candidates = [{"run_id": "recovery-1"}]
    result = SimpleNamespace(success=True, payload={"recoveries": candidates})

    def dispatch(
        kind: str,
        payload: dict[str, object],
        *,
        pending_action: str,
        pending_params: dict[str, object] | None = None,
    ) -> bool:
        delegated.append((kind, payload, pending_action, pending_params))
        return True

    def handle_workspace_result(value: object) -> str:
        delegated.append(("result", value))
        return "handled"

    window = SimpleNamespace(
        _dispatch_command=dispatch,
        _handle_workspace_result=handle_workspace_result,
        _pending_action="list_recoveries",
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    accepted = window._dispatch_command(
        "list_recoveries",
        {"scope": "all"},
        pending_action="list_recoveries",
        pending_params={"source": "review"},
    )
    handled = window._handle_workspace_result(result)

    assert accepted is True
    assert handled == "handled"
    assert delegated == [
        (
            "list_recoveries",
            {"scope": "all"},
            "list_recoveries",
            {"source": "review"},
        ),
        ("result", result),
    ]
    assert stages == [
        "recovery-list-dispatch-requested",
        "recovery-list-dispatch-accepted",
        "recovery-list-result-received",
        "recovery-list-result-succeeded",
        "recovery-candidates-received",
    ]


def test_recovery_dispatch_diagnostics_restore_after_launcher_success() -> None:
    gate = _gate()

    def original_dispatch(*_args: object, **_kwargs: object) -> bool:
        return True

    def original_handler(result: object) -> object:
        return result

    window = SimpleNamespace(
        _dispatch_command=original_dispatch,
        _handle_workspace_result=original_handler,
        _pending_action=None,
    )

    restore = gate._instrument_recovery_boundaries(
        window=window, record=lambda _stage: None
    )
    assert window._dispatch_command is not original_dispatch
    assert window._handle_workspace_result is not original_handler

    restore()

    assert window._dispatch_command is original_dispatch
    assert window._handle_workspace_result is original_handler


def test_recovery_dispatch_diagnostics_restore_identity_guarded_after_launcher_failure(
) -> None:
    gate = _gate()

    def original_dispatch(*_args: object, **_kwargs: object) -> bool:
        return True

    def original_handler(result: object) -> object:
        return result

    window = SimpleNamespace(
        _dispatch_command=original_dispatch,
        _handle_workspace_result=original_handler,
        _pending_action=None,
    )

    restore = gate._instrument_recovery_boundaries(
        window=window, record=lambda _stage: None
    )
    def replacement_handler(result: object) -> object:
        return result
    window._handle_workspace_result = replacement_handler
    try:
        raise RuntimeError("launcher failure")
    except RuntimeError:
        pass
    finally:
        restore()

    assert window._dispatch_command is original_dispatch
    assert window._handle_workspace_result is replacement_handler


def test_recovery_dispatch_diagnostics_do_not_report_rejected_command_as_accepted(
) -> None:
    gate = _gate()
    stages: list[str] = []
    window = SimpleNamespace(
        _dispatch_command=lambda *_args, **_kwargs: False,
        _handle_workspace_result=lambda result: result,
        _pending_action=None,
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    accepted = window._dispatch_command(
        "list_recoveries", {}, pending_action="list_recoveries"
    )

    assert accepted is False
    assert stages == ["recovery-list-dispatch-requested"]


def test_recovery_dispatch_diagnostics_distinguish_an_empty_list_result() -> None:
    gate = _gate()
    stages: list[str] = []
    result = SimpleNamespace(success=True, payload={"recoveries": []})
    window = SimpleNamespace(
        _dispatch_command=lambda *_args, **_kwargs: True,
        _handle_workspace_result=lambda value: value,
        _pending_action="list_recoveries",
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    handled = window._handle_workspace_result(result)

    assert handled is result
    assert stages == [
        "recovery-list-result-received",
        "recovery-list-result-succeeded",
    ]


def test_recovery_dispatch_diagnostics_record_an_error_result_at_handler_entry(
) -> None:
    gate = _gate()
    stages: list[str] = []
    handled: list[object] = []
    result = SimpleNamespace(success=False, payload={})

    def handle_workspace_result(value: object) -> str:
        handled.append(value)
        return "handled"

    window = SimpleNamespace(
        _dispatch_command=lambda *_args, **_kwargs: True,
        _handle_workspace_result=handle_workspace_result,
        _pending_action="list_recoveries",
    )
    gate._instrument_recovery_boundaries(window=window, record=stages.append)

    outcome = window._handle_workspace_result(result)

    assert outcome == "handled"
    assert handled == [result]
    assert stages == ["recovery-list-result-received"]


def test_consumer_installs_recovery_dispatch_diagnostics_before_review() -> None:
    source = inspect.getsource(_gate()._run_consumer_launcher)

    assert source.index("_instrument_recovery_boundaries(") < source.index(
        "QTest.mouseClick("
    )


def test_shared_memory_cleanup_probe_uses_reattach_not_dev_shm() -> None:
    assert _gate().shared_memory_cleanup_probe("trialgate-") is True


def test_generated_shm_names_fit_darwin_kernel_budget() -> None:
    """The portable run, phase, and cleanup names fit Darwin's 30-byte cap."""
    gate = _gate()
    run_prefix = gate._new_shm_run_prefix()
    assert len(run_prefix.encode("ascii")) == 13
    assert len(f"{run_prefix}n".encode("ascii")) == 14
    assert len(f"{run_prefix}r".encode("ascii")) == 14
    assert len(gate._new_shm_probe_name(run_prefix).encode("ascii")) == 30


def test_gate_result_reader_requires_the_exact_bounded_success_schema() -> None:
    """The resolver accepts only a complete, internally consistent gate record."""
    checks = {name: True for name in _gate()._CHECK_NAMES}
    encoded = _gate().gate_result_json(checks, installed=_identity())

    assert _gate().read_gate_result(encoded) == {
        "architecture": "x86_64",
        "checks": checks,
        "installed": {
            "build_id": _identity()["build_id"],
            "source_sha": _identity()["source_sha"],
            "version": _identity()["version"],
        },
        "python_version": "3.12.12",
        "schema": 3,
        "status": "passed",
    }

    malformed = json.loads(encoded)
    malformed["checks"]["worker_exit"] = False
    with pytest.raises(_gate().GateError, match="status"):
        _gate().read_gate_result(json.dumps(malformed, sort_keys=True).encode("utf-8"))


def test_gate_command_uses_isolated_venv_python_and_binds_private_paths(
    tmp_path: Path,
) -> None:
    """The master invokes the immutable, source-verified gate script it receives."""
    python = tmp_path / "phase-two" / "bin" / "python"
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    result = work_root / "technical-gate.json"
    gate_fd = 37

    command = _gate().technical_gate_command(
        python=python,
        gate_fd=gate_fd,
        checkout_root=checkout,
        work_root=work_root,
        result_path=result,
        native_qt=True,
    )

    assert command[:3] == (str(python), "-I", "-c")
    assert command[3] == _gate()._SEALED_GATE_BOOTSTRAP
    assert command[4] == str(gate_fd)
    assert command[command.index("--gate-fd") + 1] == str(gate_fd)
    assert command[command.index("--checkout") + 1] == str(checkout)
    assert command[command.index("--work-root") + 1] == str(work_root)
    assert command[command.index("--result") + 1] == str(result)
    assert "--native-qt" in command


def test_external_recovery_gate_runs_a_crashing_producer_then_a_fresh_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal, export-crash, and recovery phases use isolated launcher processes."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    phase_python = tmp_path / "phase-two" / "bin" / "python"
    gate_fd = 37
    checkout.mkdir()
    work_root.mkdir()
    seen: list[tuple[str, ...]] = []

    def fake_run(command: Sequence[str], **kwargs: object) -> SimpleNamespace:
        values: tuple[str, ...] = tuple(command)
        seen.append(values)
        if "--phase" not in values:
            assert values == (str(phase_python), str(work_root / "python-export.py"))
            assert "pass_fds" not in kwargs
            return SimpleNamespace(returncode=0)
        assert kwargs["pass_fds"] == (gate_fd,)
        phase = values[values.index("--phase") + 1]
        if phase == "producer":
            (work_root / "trial.gwxproj").write_text("saved", encoding="utf-8")
            (work_root / "python-export.py").write_text(
                "print('export')\n", encoding="utf-8"
            )
        return SimpleNamespace(returncode=-signal.SIGKILL if phase == "producer" else 0)

    monkeypatch.setattr(
        _gate(), "subprocess", SimpleNamespace(run=fake_run), raising=False
    )
    checks = {name: False for name in _gate()._CHECK_NAMES}

    _gate().run_external_recovery_gate(
        checks=checks,
        checkout=checkout,
        gate_fd=gate_fd,
        phase_python=phase_python,
        work_root=work_root,
        native_qt=True,
        parent_shm_prefix="trialgate-unique-",
    )

    phase_commands = [command for command in seen if "--phase" in command]
    assert [command[command.index("--phase") + 1] for command in phase_commands] == [
        "normal",
        "producer",
        "consumer",
    ]
    assert all(
        command[:3] == (str(phase_python), "-I", "-c") for command in phase_commands
    )
    assert all(
        command[3] == _gate()._SEALED_GATE_BOOTSTRAP for command in phase_commands
    )
    assert all(command[4] == str(gate_fd) for command in phase_commands)
    assert "--project" not in phase_commands[0]
    assert "--project" not in phase_commands[1]
    assert phase_commands[2][phase_commands[2].index("--project") + 1] == str(
        work_root / "trial.gwxproj"
    )
    assert all(
        checks[name]
        for name in (
            "welcome",
            "try_sample",
            "crop",
            "asd",
            "save_project",
            "normal_close_reopen",
            "io_read",
            "io_refusal",
            "project_reopen",
            "recovery",
            "worker_exit",
        )
    )


def test_external_recovery_gate_preserves_native_qt_and_derives_phase_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    phase_python = tmp_path / "phase-two" / "bin" / "python"
    checkout.mkdir()
    work_root.mkdir()
    environments: list[dict[str, str]] = []

    def fake_run(command: Sequence[str], **kwargs: object) -> SimpleNamespace:
        values = tuple(command)
        environments.append(dict(kwargs["env"]))
        phase = values[values.index("--phase") + 1]
        if phase == "producer":
            (work_root / "trial.gwxproj").write_text("saved", encoding="utf-8")
            (work_root / "python-export.py").write_text(
                "print('export')\n", encoding="utf-8"
            )
        return SimpleNamespace(returncode=-signal.SIGKILL if phase == "producer" else 0)

    monkeypatch.setattr(
        _gate(), "subprocess", SimpleNamespace(run=fake_run), raising=False
    )
    _gate().run_external_recovery_gate(
        checks={name: False for name in _gate()._CHECK_NAMES},
        checkout=checkout,
        gate_fd=37,
        phase_python=phase_python,
        work_root=work_root,
        native_qt=True,
        parent_shm_prefix="g0123456789ab",
    )

    assert [item["GWEXPY_STUDIO_SHM_PREFIX"] for item in environments] == [
        "g0123456789abn",
        "g0123456789abr",
        "g0123456789abr",
    ]
    assert all(len(item["GWEXPY_STUDIO_SHM_PREFIX"]) <= 14 for item in environments)
    assert all("QT_QPA_PLATFORM" not in item for item in environments)


def test_phase_main_runs_the_producer_without_a_final_gate_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash producer is a child phase, not a second master gate."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    checkout.mkdir()
    work_root.mkdir()
    seen: list[tuple[Path, Path]] = []

    monkeypatch.setattr(
        _gate(),
        "_run_producer_launcher",
        lambda *, checkout, work_root: seen.append((checkout, work_root)),
        raising=False,
    )

    assert (
        _gate().main(
            [
                "--phase",
                "producer",
                "--checkout",
                str(checkout),
                "--work-root",
                str(work_root),
            ]
        )
        == 0
    )
    assert seen == [(checkout.resolve(), work_root.resolve())]


def test_external_recovery_gate_rejects_a_crashed_producer_without_saved_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGKILL alone cannot claim that the recovery producer reached Save."""
    checkout = tmp_path / "public-p"
    work_root = tmp_path / "private-work"
    gate_fd = 37
    checkout.mkdir()
    work_root.mkdir()

    def fake_run(command: Sequence[str], **_kwargs: object) -> SimpleNamespace:
        values = tuple(command)
        phase = values[values.index("--phase") + 1]
        return SimpleNamespace(
            returncode=0 if phase == "normal" else -signal.SIGKILL
        )

    monkeypatch.setattr(
        _gate(), "subprocess", SimpleNamespace(run=fake_run), raising=False
    )

    with pytest.raises(_gate().GateError, match="did not save"):
        _gate().run_external_recovery_gate(
            checks={name: False for name in _gate()._CHECK_NAMES},
            checkout=checkout,
            gate_fd=gate_fd,
            phase_python=tmp_path / "phase-two" / "bin" / "python",
            work_root=work_root,
            parent_shm_prefix="trialgate-unique-",
        )


def test_new_gate_result_records_normal_replay_and_io_as_independent_checks() -> None:
    gate = _gate()

    assert {
        "normal_close_reopen",
        "export_numeric_replay",
        "io_read",
        "io_refusal",
    } <= set(gate._CHECK_NAMES)
    encoded = gate.gate_result_json(
        {name: True for name in gate._CHECK_NAMES}, installed=_identity()
    )

    document = json.loads(encoded)
    assert document["schema"] == 3
    assert document["status"] == "passed"
    assert gate.read_gate_result(encoded)["schema"] == 3


def test_gate_result_reader_keeps_historical_schema2_bytes_readable() -> None:
    gate = _gate()
    legacy_names = (
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
    )
    legacy = {
        "architecture": "x86_64",
        "checks": {name: True for name in legacy_names},
        "installed": {
            "build_id": _identity()["build_id"],
            "source_sha": _identity()["source_sha"],
            "version": _identity()["version"],
        },
        "python_version": _identity()["python_version"],
        "schema": 2,
        "status": "passed",
    }

    assert gate.read_gate_result(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ) == legacy


def test_normal_phase_is_a_separate_launcher_phase(tmp_path: Path) -> None:
    gate = _gate()
    command = gate._phase_command(
        phase="normal",
        python=tmp_path / "phase-two" / "bin" / "python",
        gate_fd=37,
        checkout=tmp_path / "public-p",
        work_root=tmp_path / "private-work",
    )

    assert command[command.index("--phase") + 1] == "normal"
    assert "--project" not in command


def _replay_reference() -> dict[str, object]:
    return {
        "schema": 1,
        "time_series": {
            "object_id": "obj-ts",
            "kind": "TimeSeries",
            "values": [1.0, 2.0],
            "unit": "m",
            "t0": 10.0,
            "dt": 0.5,
            "times": [10.0, 10.5],
        },
        "frequency_series": {
            "object_id": "obj-asd",
            "kind": "FrequencySeries",
            "values": [3.0, 4.0],
            "unit": "m / Hz**0.5",
            "f0": 1.0,
            "df": 0.25,
            "frequencies": [1.0, 1.25],
        },
    }


def _replay_namespace() -> dict[str, object]:
    return {
        "obj_ts": type("TimeSeries", (), {
            "value": [1.0, 2.0],
            "unit": "m",
            "t0": SimpleNamespace(to_value=lambda _unit: 10.0),
            "dt": SimpleNamespace(to_value=lambda _unit: 0.5),
            "times": SimpleNamespace(to_value=lambda _unit: [10.0, 10.5]),
        })(),
        "obj_asd": type("FrequencySeries", (), {
            "value": [3.0, 4.0],
            "unit": "m / Hz**0.5",
            "f0": SimpleNamespace(to_value=lambda _unit: 1.0),
            "df": SimpleNamespace(to_value=lambda _unit: 0.25),
            "frequencies": SimpleNamespace(to_value=lambda _unit: [1.0, 1.25]),
        })(),
    }


def test_export_replay_validator_rejects_wrong_targets_values_units_and_axes() -> None:
    gate = _gate()
    reference = _replay_reference()

    gate.validate_export_replay_namespace(_replay_namespace(), reference)
    for mutation, expected in (
        (lambda value: value.pop("obj_ts"), "target"),
        (
            lambda value: value["obj_ts"].value.__setitem__(0, 9.0),
            "value",
        ),
        (lambda value: setattr(value["obj_ts"], "unit", "s"), "unit"),
        (
            lambda value: setattr(
                value["obj_asd"].frequencies,
                "to_value",
                lambda _unit: [1.0, 1.5],
            ),
            "axis",
        ),
        (
            lambda value: setattr(
                value["obj_ts"].t0,
                "to_value",
                lambda _unit: 11.0,
            ),
            "axis",
        ),
        (
            lambda value: setattr(
                value["obj_asd"].f0,
                "to_value",
                lambda _unit: 1.5,
            ),
            "axis",
        ),
    ):
        namespace = _replay_namespace()
        mutation(namespace)
        with pytest.raises(gate.GateError, match=expected):
            gate.validate_export_replay_namespace(namespace, reference)


def test_export_replay_snapshot_rejects_t0_and_f0_mutations() -> None:
    gate = _gate()
    reference = _replay_reference()
    observed = {
        key: {
            "kind": value["kind"],
            "values": value["values"],
            "unit": value["unit"],
            "t0": value.get("t0"),
            "dt": value.get("dt"),
            "times": value.get("times"),
            "f0": value.get("f0"),
            "df": value.get("df"),
            "frequencies": value.get("frequencies"),
        }
        for key, value in reference.items()
        if key != "schema"
    }
    observed["time_series"].pop("f0")
    observed["time_series"].pop("df")
    observed["time_series"].pop("frequencies")
    observed["frequency_series"].pop("t0")
    observed["frequency_series"].pop("dt")
    observed["frequency_series"].pop("times")
    gate._validate_replay_snapshot(observed, reference)
    for key, origin in (("time_series", "t0"), ("frequency_series", "f0")):
        mutated = json.loads(json.dumps(observed))
        mutated[key][origin] += 1.0
        with pytest.raises(gate.GateError, match="axis"):
            gate._validate_replay_snapshot(mutated, reference)


def test_export_replay_driver_runs_a_real_studio_free_python_script(
    tmp_path: Path,
) -> None:
    """The driver executes runpy in the prepared third prefix, not the checkout."""
    gate = _gate()
    replay_python_value = os.environ.get("GWEXPY_STUDIO_REPLAY_PYTHON")
    if replay_python_value is None or not Path(replay_python_value).is_file():
        pytest.skip("prepared Studio-free replay prefix is unavailable")
    replay_python = Path(replay_python_value)
    (tmp_path / "python-export.py").write_text(
        "import numpy as np\n"
        "from gwexpy.timeseries import TimeSeries\n"
        "from gwexpy.frequencyseries import FrequencySeries\n"
        "obj_ts = TimeSeries(np.array([1., 2.]), t0=10., dt=.5, unit='m')\n"
        "obj_asd = FrequencySeries(\n"
        "    np.array([3., 4.]), f0=1., df=.25, unit='m / Hz**0.5'\n"
        ")\n",
        encoding="utf-8",
    )
    (tmp_path / "export-reference.json").write_text(
        json.dumps(_replay_reference()), encoding="utf-8"
    )

    gate._run_export_replay(
        replay_python=replay_python,
        checkout=Path(__file__).resolve().parents[2],
        work_root=tmp_path,
    )

    assert (tmp_path / "python-replay.json").is_file()


def test_export_replay_validator_rejects_studio_visibility() -> None:
    gate = _gate()

    with pytest.raises(gate.GateError, match="Studio"):
        gate.validate_export_replay_namespace(
            _replay_namespace(), _replay_reference(), studio_visible=True
        )
