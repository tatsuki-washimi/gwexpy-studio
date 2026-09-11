"""Contracts for the source-tree-independent installed-wheel technical gate.

A last-success prefix cannot distinguish a next-operation exception from a
failure to record its successor; the prefix only identifies the last confirmed
boundary.
"""

from __future__ import annotations

import importlib
import inspect
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _gate():
    return importlib.import_module("scripts.run_trial_technical_gate")


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


class _PrefixMessage:
    def __init__(self, title: str, *, title_error: BaseException | None = None) -> None:
        self._title = title
        self.title_reads = 0
        self._title_error = title_error

    def windowTitle(self) -> str:  # noqa: N802 - Qt API spelling
        self.title_reads += 1
        if self._title_error is not None:
            raise self._title_error
        return self._title

    def execute(self) -> object:
        return "executed"


class _PrefixCallable:
    def __init__(self, owner: object) -> None:
        self.owner = owner
        self.self_reads = 0

    @property
    def __self__(self) -> object:
        self.self_reads += 1
        return self.owner

    def __call__(self) -> object:
        return "executed"


class _PrefixScheduler:
    diagnostic_selection_succeeded = True

    def __init__(self, *, bind_error: BaseException | None = None) -> None:
        self.bind_error = bind_error
        self.bound: object | None = None
        self.retain_error: BaseException | None = None

    def bind(self, message: object) -> None:
        if self.bind_error is not None:
            raise self.bind_error
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
        if self.retain_error is not None:
            raise self.retain_error


def _prefix_binding(stages: list[str]) -> Any:
    return _diagnostic_binding(record=stages.append, message_type=_PrefixMessage)


def test_recovery_prefix_absent_callable_owner_stops_after_boundary() -> None:
    """The last prefix cannot classify an exception or missing successor record."""
    stages: list[str] = []
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    try:
        binding._workspace_module.workspace_dialog(object(), lambda: "executed")
    finally:
        binding.restore()

    assert stages == ["consumer/recovery-dialog-boundary-entered"]


def test_recovery_prefix_incompatible_callable_owner_stops_after_owner_present(
) -> None:
    """A present callable owner must be compatible before title inspection."""
    stages: list[str] = []
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    owner = object()
    execute = _PrefixCallable(owner)
    try:
        assert (
            binding._workspace_module.workspace_dialog(object(), execute)
            == "executed"
        )
    finally:
        binding.restore()

    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
    ]
    assert execute.self_reads == 1


def test_recovery_prefix_title_read_exception_stops_before_title_read_stage() -> None:
    """A title-read exception leaves the prior prefix as the only evidence."""
    stages: list[str] = []
    sentinel = RuntimeError("title value must stay private")
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    message = _PrefixMessage("Recover unfinished work", title_error=sentinel)
    try:
        with pytest.raises(RuntimeError) as caught:
            binding._workspace_module.workspace_dialog(object(), message.execute)
    finally:
        binding.restore()

    assert caught.value is sentinel
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
    ]
    assert message.title_reads == 1


def test_recovery_prefix_title_mismatch_stops_after_title_read() -> None:
    """A non-recovery title cannot reach scheduler resolution."""
    stages: list[str] = []
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    message = _PrefixMessage("Other dialog")
    try:
        assert (
            binding._workspace_module.workspace_dialog(object(), message.execute)
            == "executed"
        )
    finally:
        binding.restore()

    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
    ]
    assert message.title_reads == 1


def test_recovery_prefix_missing_scheduler_stops_after_title_match() -> None:
    """A matched title without a scheduler cannot claim a bound dialog."""
    stages: list[str] = []
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    message = _PrefixMessage("Recover unfinished work")
    try:
        assert (
            binding._workspace_module.workspace_dialog(object(), message.execute)
            == "executed"
        )
    finally:
        binding.restore()

    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
        "consumer/recovery-dialog-title-matched",
    ]
    assert message.title_reads == 1


def test_recovery_prefix_bind_failure_stops_after_scheduler_resolution() -> None:
    """A bind exception leaves scheduler resolution as the last confirmed step."""
    stages: list[str] = []
    sentinel = RuntimeError("bind detail must stay private")
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    scheduler = _PrefixScheduler(bind_error=sentinel)
    binding.register("Recover unfinished work", scheduler)
    message = _PrefixMessage("Recover unfinished work")
    try:
        with pytest.raises(RuntimeError) as caught:
            binding._workspace_module.workspace_dialog(object(), message.execute)
    finally:
        binding.restore()

    assert caught.value is sentinel
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
        "consumer/recovery-dialog-title-matched",
        "consumer/recovery-dialog-scheduler-resolved",
    ]


def test_recovery_prefix_retain_failure_stops_after_scheduler_bind() -> None:
    """A retain exception leaves scheduler binding as the last confirmed step."""
    stages: list[str] = []
    sentinel = RuntimeError("retain detail must stay private")
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    scheduler = _PrefixScheduler()
    scheduler.retain_error = sentinel
    binding.register("Recover unfinished work", scheduler)
    message = _PrefixMessage("Recover unfinished work")
    try:
        with pytest.raises(RuntimeError) as caught:
            binding._workspace_module.workspace_dialog(object(), message.execute)
    finally:
        binding.restore()

    assert caught.value is sentinel
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
        "consumer/recovery-dialog-title-matched",
        "consumer/recovery-dialog-scheduler-resolved",
        "consumer/recovery-dialog-scheduler-bound",
    ]


def test_recovery_prefix_full_success_reads_owner_and_title_once() -> None:
    """Full success emits each prefix only after its operation succeeds."""
    stages: list[str] = []
    binding = _prefix_binding(stages)
    binding.arm_recovery()
    scheduler = _PrefixScheduler()
    binding.register("Recover unfinished work", scheduler)
    message = _PrefixMessage("Recover unfinished work")
    execute = _PrefixCallable(message)
    try:
        assert (
            binding._workspace_module.workspace_dialog(object(), execute)
            == "executed"
        )
    finally:
        binding.restore()

    assert stages[:8] == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
        "consumer/recovery-dialog-title-matched",
        "consumer/recovery-dialog-scheduler-resolved",
        "consumer/recovery-dialog-scheduler-bound",
        "consumer/recovery-dialog-diagnostic-retained",
    ]
    assert stages[8:] == [
        "consumer/recovery-dialog-instance-bound",
        "consumer/recovery-dialog-modal-returned",
    ]
    assert execute.self_reads == 1
    assert message.title_reads == 1


def test_workspace_dialog_binding_emits_fixed_recovery_stages_in_success_order(
) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

    gate = _gate()
    _app = QApplication.instance() or QApplication([])
    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    app_like = SimpleNamespace(
        activeModalWidget=lambda: None,
        topLevelWidgets=lambda: [],
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
        on_visible=lambda: stages.append("existing-visible"),
        on_selected=lambda: stages.append("existing-selected"),
    )

    try:
        binding._workspace_module.workspace_dialog(object(), dialog.exec)
    finally:
        binding.restore()
        dialog.close()
        owner.close()

    assert scheduled.error is None
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
        "consumer/recovery-dialog-title-matched",
        "consumer/recovery-dialog-scheduler-resolved",
        "consumer/recovery-dialog-scheduler-bound",
        "consumer/recovery-dialog-diagnostic-retained",
        "consumer/recovery-dialog-instance-bound",
        "consumer/recovery-dialog-poll-entered",
        "existing-visible",
        "consumer/recovery-dialog-button-resolved",
        "existing-selected",
        "consumer/recovery-dialog-modal-returned",
    ]


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


def test_workspace_dialog_binding_armed_unbound_callable_is_single_use() -> None:
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
    assert (
        binding._workspace_module.workspace_dialog(object(), lambda: "unbound")
        == "unbound"
    )
    scheduled = gate._schedule_message_box_button(
        app=app_like,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
        timeout_s=0.5,
        binding=binding,
    )
    try:
        binding._workspace_module.workspace_dialog(object(), dialog.exec)
    finally:
        binding.restore()
        dialog.close()
        owner.close()
    assert scheduled.error is None
    assert stages == ["consumer/recovery-dialog-boundary-entered"]


def test_workspace_dialog_binding_armed_different_title_is_single_use() -> None:
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
    binding.arm_recovery()
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
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
    ]


def test_workspace_dialog_binding_armed_fixed_title_without_scheduler_reports_prefix(
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
        binding._workspace_module.workspace_dialog(object(), dialog.reject)
    finally:
        binding.restore()
        dialog.close()
        owner.close()
    assert stages == [
        "consumer/recovery-dialog-boundary-entered",
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
        "consumer/recovery-dialog-title-matched",
    ]


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
    )
    try:
        binding._workspace_module.workspace_dialog(object(), dialog.exec)
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
        on_selected=lambda: (_ for _ in ()).throw(sentinel),
    )
    try:
        binding._workspace_module.workspace_dialog(object(), dialog.exec)
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
    )
    QTimer.singleShot(1, dialog.reject)
    try:
        binding._workspace_module.workspace_dialog(object(), dialog.exec)
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
    binding.arm_recovery()
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
    with pytest.raises(_gate().GateError) as caught:
        binding._workspace_module.workspace_dialog(object(), lambda: None)
    assert hostile not in str(caught.value)


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
        "consumer/recovery-dialog-callable-owner-present",
        "consumer/recovery-dialog-callable-owner-compatible",
        "consumer/recovery-dialog-title-read",
        "consumer/recovery-dialog-title-matched",
        "consumer/recovery-dialog-scheduler-resolved",
        "consumer/recovery-dialog-scheduler-bound",
        "consumer/recovery-dialog-diagnostic-retained",
        "consumer/recovery-dialog-instance-bound",
        "consumer/recovery-dialog-poll-entered",
        "consumer/recovery-dialog-button-resolved",
        "consumer/recovery-dialog-modal-returned",
    }
    source = inspect.getsource(gate._WorkspaceMessageBinding)
    assert "record=" in source
    assert "message_binding=" in inspect.getsource(gate._instrument_recovery_boundaries)


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
        "schema": 2,
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
    )

    assert command[:3] == (str(python), "-I", "-c")
    assert command[3] == _gate()._SEALED_GATE_BOOTSTRAP
    assert command[4] == str(gate_fd)
    assert command[command.index("--gate-fd") + 1] == str(gate_fd)
    assert command[command.index("--checkout") + 1] == str(checkout)
    assert command[command.index("--work-root") + 1] == str(work_root)
    assert command[command.index("--result") + 1] == str(result)


def test_external_recovery_gate_runs_a_crashing_producer_then_a_fresh_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery qualification uses two isolated phase-two launcher processes."""
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
    )

    phase_commands = [command for command in seen if "--phase" in command]
    assert [command[command.index("--phase") + 1] for command in phase_commands] == [
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
    assert phase_commands[1][phase_commands[1].index("--project") + 1] == str(
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
            "project_reopen",
            "recovery",
            "worker_exit",
        )
    )


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

    monkeypatch.setattr(
        _gate(),
        "subprocess",
        SimpleNamespace(
            run=lambda *_args, **_kwargs: SimpleNamespace(returncode=-signal.SIGKILL)
        ),
        raising=False,
    )

    with pytest.raises(_gate().GateError, match="did not save"):
        _gate().run_external_recovery_gate(
            checks={name: False for name in _gate()._CHECK_NAMES},
            checkout=checkout,
            gate_fd=gate_fd,
            phase_python=tmp_path / "phase-two" / "bin" / "python",
            work_root=work_root,
        )
