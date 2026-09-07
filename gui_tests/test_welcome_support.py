"""Offscreen interactions for the trial welcome and support UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

import gwexpy_studio.ui.trial_support_window as trial_support_module
from gwexpy_studio.domain.project import Project
from gwexpy_studio.runtime.trial import RecentProjectStore
from gwexpy_studio.ui.bridge import BridgeResult, BridgeState
from gwexpy_studio.ui.window import MainWindow


class _StoppedThread:
    def isRunning(self) -> bool:  # noqa: N802 - Qt API spelling
        return False


class _BridgeStub(QObject):
    result_received = Signal(object)
    safe_to_destroy = Signal()
    state_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.state = BridgeState.IDLE
        self.worker_thread = _StoppedThread()
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def send_command(self, kind: str, payload: dict[str, Any]) -> str:
        self.commands.append((kind, payload))
        return f"command-{len(self.commands)}"

    def close(self, timeout_s: float = 15.0) -> None:
        del timeout_s


def _dispose(window: MainWindow, qapp: QApplication) -> None:
    window.close()
    window.deleteLater()
    qapp.processEvents()


def _developer_capability_snapshot() -> dict[str, object]:
    """Build the same safe bootstrap contract returned by a source-mode worker."""
    from gwexpy_studio.ops.io_capabilities import (
        CapabilityManifest,
        probe_effective_capabilities,
    )

    return probe_effective_capabilities(
        CapabilityManifest(mode="developer")
    ).document()


def _invalid_capability_snapshot() -> dict[str, object]:
    """Build the fail-closed worker result used for a damaged trial policy."""
    from gwexpy_studio.ops.io_capabilities import (
        CapabilityManifest,
        probe_effective_capabilities,
    )

    return probe_effective_capabilities(CapabilityManifest(mode="invalid")).document()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-WLC-001")
def test_empty_workspace_welcome_reuses_file_actions(qapp: QApplication) -> None:
    """The empty state routes its two primary choices through File actions."""
    window = MainWindow(bridge=_BridgeStub())  # type: ignore[arg-type]
    try:
        assert window.central_stack.currentWidget() is window.welcome_panel
        assert window.welcome_panel.open_data_button.isEnabled()
        assert window.welcome_panel.open_project_button.isEnabled()

        opened: list[str] = []
        window.open_action.triggered.disconnect()
        window.open_action.triggered.connect(lambda: opened.append("data"))
        window.open_project_action.triggered.disconnect()
        window.open_project_action.triggered.connect(lambda: opened.append("project"))

        window.welcome_panel.open_data_button.click()
        window.welcome_panel.open_project_button.click()

        assert opened == ["data", "project"]
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-WLC-002")
def test_try_sample_prefills_the_normal_open_data_panel_without_reading(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Try Sample materializes one sample and stops at the ordinary review UI."""
    sample = tmp_path / "sample.csv"
    sample.write_text("time,value\n0,1\n", encoding="utf-8")
    monkeypatch.setattr(trial_support_module, "sample_path", lambda: sample)
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        window.welcome_panel.try_sample_button.click()

        assert window.open_data_panel is not None
        assert window.open_data_panel.paths_edit.toPlainText() == str(sample)
        assert json.loads(window.open_data_panel.kwargs_edit.toPlainText()) == {
            "skiprows": 1
        }
        assert [kind for kind, _payload in bridge.commands] == ["catalog_io"]
        assert window.open_data_panel.confirm_button.isEnabled() is False
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.explicit_capability_handshake
@pytest.mark.contract(id="GUI-WLC-003")
def test_welcome_actions_wait_for_a_pending_recovery_lookup(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Welcome cannot materialize or open files while File actions are reserved."""
    project = tmp_path / "saved.gwxproj"
    project.write_text("placeholder", encoding="utf-8")
    RecentProjectStore().record(project)
    materialized: list[None] = []
    monkeypatch.setattr(
        trial_support_module,
        "sample_path",
        lambda: materialized.append(None) or tmp_path / "sample.csv",
    )
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        recent_item = window.welcome_panel.recent_projects.item(0)
        assert recent_item is not None
        assert not window.open_action.isEnabled()
        assert not window.open_project_action.isEnabled()
        assert not window.welcome_panel.open_data_button.isEnabled()
        assert not window.welcome_panel.open_project_button.isEnabled()
        assert not window.welcome_panel.try_sample_button.isEnabled()
        assert not window.welcome_panel.clear_recent_projects_button.isEnabled()
        assert not recent_item.flags() & Qt.ItemFlag.ItemIsEnabled

        window.initialize_workspace()

        assert bridge.commands == [("start", {})]
        assert not window.open_action.isEnabled()
        assert not window.open_project_action.isEnabled()
        assert not window.welcome_panel.open_data_button.isEnabled()
        assert not window.welcome_panel.open_project_button.isEnabled()
        assert not window.welcome_panel.try_sample_button.isEnabled()
        assert not window.welcome_panel.clear_recent_projects_button.isEnabled()
        assert not recent_item.flags() & Qt.ItemFlag.ItemIsEnabled

        window._try_sample()
        assert materialized == []

        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={
                    "ready": True,
                    "io_capabilities": _developer_capability_snapshot(),
                },
            )
        )

        assert bridge.commands == [("start", {}), ("list_recoveries", {})]
        assert not window.open_action.isEnabled()

        window._on_bridge_result(
            BridgeResult(
                command_id="command-2", success=True, payload={"recoveries": []}
            )
        )

        assert window.open_action.isEnabled()
        assert window.open_project_action.isEnabled()
        assert window.welcome_panel.open_data_button.isEnabled()
        assert window.welcome_panel.open_project_button.isEnabled()
        assert window.welcome_panel.try_sample_button.isEnabled()
        assert window.welcome_panel.clear_recent_projects_button.isEnabled()
        assert recent_item.flags() & Qt.ItemFlag.ItemIsEnabled

        bridge.state = BridgeState.RUNNING
        bridge.state_changed.emit(BridgeState.RUNNING)

        assert not window.open_action.isEnabled()
        assert not window.open_project_action.isEnabled()
        assert not window.welcome_panel.open_data_button.isEnabled()
        assert not window.welcome_panel.open_project_button.isEnabled()
        assert not window.welcome_panel.try_sample_button.isEnabled()
        assert not window.welcome_panel.clear_recent_projects_button.isEnabled()
        assert not recent_item.flags() & Qt.ItemFlag.ItemIsEnabled
        window._try_sample()
        assert materialized == []

        bridge.state = BridgeState.IDLE
        bridge.state_changed.emit(BridgeState.IDLE)

        assert window.welcome_panel.open_data_button.isEnabled()
        assert window.welcome_panel.open_project_button.isEnabled()
        assert window.welcome_panel.try_sample_button.isEnabled()
        assert window.welcome_panel.clear_recent_projects_button.isEnabled()
        assert recent_item.flags() & Qt.ItemFlag.ItemIsEnabled
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.explicit_capability_handshake
@pytest.mark.contract(id="GUI-WLC-012")
def test_data_actions_wait_for_valid_worker_capabilities_before_recovery_lookup(
    qapp: QApplication,
) -> None:
    """A launch cannot enable data I/O from an omitted or unvalidated snapshot."""
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        assert not window.open_action.isEnabled()
        assert not window.export_action.isEnabled()
        assert not window.open_project_action.isEnabled()

        window.initialize_workspace()

        assert bridge.commands == [("start", {})]
        assert not window.open_action.isEnabled()
        assert not window.open_project_action.isEnabled()

        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={"ready": True},
            )
        )
        assert not window.open_action.isEnabled()
        assert not window.open_project_action.isEnabled()
        assert bridge.commands == [("start", {})]

        window.initialize_workspace()
        window._on_bridge_result(
            BridgeResult(
                command_id="command-2",
                success=True,
                payload={
                    "ready": True,
                    "io_capabilities": _developer_capability_snapshot(),
                },
            )
        )
        assert bridge.commands == [
            ("start", {}),
            ("start", {}),
            ("list_recoveries", {}),
        ]
        assert not window.open_action.isEnabled()

        window._on_bridge_result(
            BridgeResult(
                command_id="command-3", success=True, payload={"recoveries": []}
            )
        )
        assert window.open_action.isEnabled()
        assert window.open_project_action.isEnabled()
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.explicit_capability_handshake
@pytest.mark.contract(id="GUI-WLC-014")
@pytest.mark.parametrize("terminal_state", (BridgeState.CLOSED, BridgeState.FAILED))
def test_invalid_worker_capability_snapshot_keeps_data_and_project_actions_disabled(
    qapp: QApplication, terminal_state: BridgeState
) -> None:
    """A broken trial policy cannot leave any data/project control enabled."""
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        window.initialize_workspace()
        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={
                    "ready": True,
                    "io_capabilities": _invalid_capability_snapshot(),
                },
            )
        )

        assert window._io_capability_ready is False
        actions = (
            window.open_action,
            window.export_action,
            window.export_data_action,
            window.include_writes_action,
            *window._operation_actions,
            *window.signal_actions.values(),
            window.filter_response_action,
            window.new_project_action,
            window.open_project_action,
            window.save_project_action,
            window.save_project_as_action,
            window.close_project_action,
            window.restore_project_action,
            window.recoveries_action,
            window.undo_analysis_action,
            window.redo_analysis_action,
            window.view_history.undo_action,
            window.view_history.redo_action,
        )
        assert all(not action.isEnabled() for action in actions)
        assert not window.plot_canvas._controls.isEnabled()
        assert not window.plot_canvas.toolbar.isEnabled()
        assert not window.welcome_panel.open_data_button.isEnabled()
        assert not window.welcome_panel.open_project_button.isEnabled()
        assert not window.welcome_panel.try_sample_button.isEnabled()

        bridge.state = terminal_state
        bridge.state_changed.emit(terminal_state)

        assert all(not action.isEnabled() for action in actions)
        assert bridge.commands == [("start", {})]
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.explicit_capability_handshake
@pytest.mark.contract(id="GUI-WLC-015")
def test_worker_snapshot_must_match_the_gui_static_capability_policy(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker cannot downgrade or substitute the launcher policy at handshake."""
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        CapabilityManifest,
        unprobed_effective_capabilities,
    )

    policy = tmp_path / "io-capabilities.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "datatype": "TimeSeries",
                        "format": "csv",
                        "direction": "read",
                        "tier": "A",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, str(policy))
    wrong_snapshot = unprobed_effective_capabilities(
        CapabilityManifest(mode="frozen", entries=(), digest="b" * 64)
    ).document()
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        window.initialize_workspace()
        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={"ready": True, "io_capabilities": wrong_snapshot},
            )
        )

        assert window._io_capability_ready is False
        assert not window.open_action.isEnabled()
        assert not window.open_project_action.isEnabled()
        assert bridge.commands == [("start", {})]
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.explicit_capability_handshake
@pytest.mark.contract(id="GUI-WLC-016")
@pytest.mark.parametrize("catalog_kind", ("missing", "stale", "developer"))
def test_frozen_handshake_rejects_missing_or_mismatched_io_catalog(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_kind: str,
) -> None:
    """A later catalog cannot downgrade the already-probed frozen worker state."""
    from gwexpy_studio.ops.io_capabilities import (
        CAPABILITY_ENVIRONMENT,
        load_capability_manifest,
        unprobed_effective_capabilities,
    )

    policy = tmp_path / "io-capabilities.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "datatype": "TimeSeries",
                        "format": "csv",
                        "direction": "read",
                        "tier": "A",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, str(policy))
    snapshot = unprobed_effective_capabilities(load_capability_manifest()).document()
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        window.initialize_workspace()
        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={"ready": True, "io_capabilities": snapshot},
            )
        )
        window._on_bridge_result(
            BridgeResult(
                command_id="command-2", success=True, payload={"recoveries": []}
            )
        )
        window.show_open_data()
        panel = window.open_data_panel
        assert panel is not None
        catalog: dict[str, object] = {
            "datatype": "TimeSeries",
            "direction": "read",
            "formats": [
                {
                    "format": "unreviewed",
                    "read": True,
                    "write": False,
                    "auto_identify": True,
                }
            ],
        }
        if catalog_kind == "stale":
            catalog.update(
                {
                    "capability_mode": "frozen",
                    "capability_digest": snapshot["policy_digest"],
                    "capability_effective_digest": "c" * 64,
                }
            )
        elif catalog_kind == "developer":
            catalog.update(
                {
                    "capability_mode": "developer",
                    "capability_digest": None,
                    "capability_effective_digest": snapshot["digest"],
                }
            )

        window._on_bridge_result(
            BridgeResult(
                command_id="command-3",
                success=True,
                payload={"io_catalog": catalog},
            )
        )

        auto = panel.format_combo.model().index(0, 0)
        assert panel._capability_active is True
        assert panel.format_combo.count() == 1
        assert not panel.format_combo.model().flags(auto) & Qt.ItemFlag.ItemIsEnabled
        panel.paths_edit.setPlainText("input.data")
        panel.format_combo.setEditText("unreviewed")
        panel.inspect_button.click()
        assert [kind for kind, _payload in bridge.commands] == [
            "start",
            "list_recoveries",
            "catalog_io",
        ]
        assert "not verified" in panel.error_label.text().lower()
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-WLC-013")
def test_about_diagnostics_uses_the_worker_capability_snapshot(
    qapp: QApplication,
) -> None:
    """About must report the post-probe worker fact, not reread policy in the GUI."""
    window = MainWindow(bridge=_BridgeStub())  # type: ignore[arg-type]
    try:
        snapshot = _developer_capability_snapshot()
        window._io_capability_snapshot = snapshot

        facts = window._diagnostics_facts()
        window.about_action.trigger()
        about = window.about_dialog
        assert about is not None
        displayed = about.diagnostics_text.toPlainText()

        assert facts.io_capability_digest is None
        assert facts.io_capability_snapshot == snapshot
        assert 'I/O capability snapshot: {"digest":"' in displayed
        assert "/home/" not in displayed
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-WLC-004")
def test_recent_projects_are_bounded_graceful_and_clearable(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Recent paths do not open projects at startup and missing entries are safe."""
    store = RecentProjectStore()
    for index in range(11):
        store.record(tmp_path / f"saved-{index}.gwxproj")
    missing = tmp_path / "missing.gwxproj"
    store.record(missing)

    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        recent = window.welcome_panel.recent_projects
        assert recent.count() == 10
        assert bridge.commands == []
        first = recent.item(0)
        assert first is not None
        assert str(missing) in first.text()
        assert not first.flags() & Qt.ItemFlag.ItemIsEnabled

        window.welcome_panel.clear_recent_projects_button.click()
        assert recent.count() == 0
        assert store.list() == ()
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-WLC-005")
def test_existing_recent_project_uses_the_regular_open_project_command(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Selecting an available recent item begins the same document transition."""
    project = tmp_path / "saved.gwxproj"
    project.write_text("placeholder", encoding="utf-8")
    RecentProjectStore().record(project)
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        item = window.welcome_panel.recent_projects.item(0)
        assert item is not None
        window.welcome_panel.recent_projects.itemActivated.emit(item)

        assert bridge.commands == [
            ("open_project", {"path": str(project.resolve(strict=False))})
        ]
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.explicit_capability_handshake
@pytest.mark.contract(id="GUI-WLC-006")
def test_successful_project_open_records_only_its_path_for_welcome(
    qapp: QApplication, tmp_path: Path
) -> None:
    """A successful document transition refreshes Recents without source loading."""
    target = tmp_path / "saved.gwxproj"
    target.write_text("placeholder", encoding="utf-8")
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        window.start_initial_workspace(target)
        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={
                    "ready": True,
                    "io_capabilities": _developer_capability_snapshot(),
                },
            )
        )
        window._on_bridge_result(
            BridgeResult(
                command_id="command-2",
                success=True,
                payload={
                    "project": Project(project_id="trial-project"),
                    "workspace_status": {
                        "generation": 1,
                        "revision": 1,
                        "project_path": str(target),
                    },
                },
            )
        )

        assert RecentProjectStore().list() == (target.resolve(strict=False),)
        assert window.welcome_panel.recent_projects.item(0).data(
            Qt.ItemDataRole.UserRole
        ) == str(target.resolve(strict=False))
        assert [kind for kind, _payload in bridge.commands] == [
            "start",
            "open_project",
            "list_recoveries",
        ]
    finally:
        window._clear_pending_command()
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.parametrize(
    ("kind", "payload"),
    (
        pytest.param(
            "new_project",
            {},
            id="new-project",
            marks=pytest.mark.contract(id="GUI-WLC-007"),
        ),
        pytest.param(
            "close_project",
            {},
            id="close-project",
            marks=pytest.mark.contract(id="GUI-WLC-008"),
        ),
        pytest.param(
            "open_project",
            {"path": "/tmp/empty.gwxproj"},
            id="open-project",
            marks=pytest.mark.contract(id="GUI-WLC-009"),
        ),
        pytest.param(
            "recover_project",
            {"run_id": "empty-recovery"},
            id="recover-project",
            marks=pytest.mark.contract(id="GUI-WLC-012"),
        ),
    ),
)
def test_empty_document_transition_returns_to_the_welcome_panel(
    qapp: QApplication, kind: str, payload: dict[str, str]
) -> None:
    """An empty document never leaves an obsolete no-data placeholder behind."""
    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        window.central_stack.setCurrentWidget(window.central_placeholder)
        assert window._dispatch_command(kind, payload, pending_action=kind)  # type: ignore[arg-type]

        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={
                    "project": Project(),
                    "workspace_status": {"generation": 1, "revision": 1},
                },
            )
        )

        assert window.central_stack.currentWidget() is window.welcome_panel
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-WLC-010")
def test_nonempty_document_transition_keeps_the_data_placeholder(
    qapp: QApplication,
) -> None:
    """Changing the empty state does not replace nonempty-workspace behavior."""
    from gwexpy_studio.domain.model import DataObjectRef

    bridge = _BridgeStub()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        project = Project(
            objects=(
                DataObjectRef(
                    object_id="data-1",
                    kind="TimeSeries",
                    shape=(2,),
                    dtype="float64",
                    unit="m",
                ),
            )
        )
        assert window._dispatch_command(
            "open_project",
            {"path": "/tmp/nonempty.gwxproj"},
            pending_action="open_project",
        )

        window._on_bridge_result(
            BridgeResult(
                command_id="command-1",
                success=True,
                payload={
                    "project": project,
                    "workspace_status": {"generation": 1, "revision": 1},
                },
            )
        )

        assert window.central_stack.currentWidget() is window.central_placeholder
    finally:
        _dispose(window, qapp)


@pytest.mark.gui
@pytest.mark.contract(id="GUI-WLC-011")
def test_help_actions_show_short_instructions_and_path_free_diagnostics(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Support UI separates clipboard-safe facts from the explicit log action."""
    import gwexpy_studio.ui.support as support_module

    opened: list[bool] = []
    monkeypatch.setattr(
        support_module, "open_log_folder", lambda: opened.append(True) or False
    )
    monkeypatch.setenv("GWEXPY_STUDIO_BUILD_ID", "P-abcdef0-20260907-r1-a1")
    monkeypatch.setenv(
        "GWEXPY_STUDIO_SOURCE_COMMIT",
        "abcdef0123456789abcdef0123456789abcdef01",
    )
    window = MainWindow(bridge=_BridgeStub())  # type: ignore[arg-type]
    try:
        snapshot = _developer_capability_snapshot()
        window._io_capability_snapshot = snapshot
        window.quick_start_action.trigger()
        quick_start = window.quick_start_dialog
        assert quick_start is not None
        assert quick_start.isVisible()
        assert "Crop" in quick_start.instructions.text()
        assert "ASD" in quick_start.instructions.text()

        window.about_action.trigger()
        about = window.about_dialog
        assert about is not None
        displayed = about.diagnostics_text.toPlainText()
        assert "Build: P-abcdef0-20260907-r1-a1" in displayed
        assert "Source commit: abcdef0123456789abcdef0123456789abcdef01" in displayed
        assert "Project schema: 3" in displayed
        assert "Worker protocol: 2" in displayed
        assert "I/O capability digest: unknown" in displayed
        assert 'I/O capability snapshot: {"digest":"' in displayed
        assert "/tmp/" not in displayed

        window._show_error("timeout", "/private/experiment/secret.csv")
        about.copy_diagnostics_button.click()
        copied = qapp.clipboard().text()
        assert "Build: P-abcdef0-20260907-r1-a1" in copied
        assert "Source commit: abcdef0123456789abcdef0123456789abcdef01" in copied
        assert "Last error code: timeout" in copied
        assert "/private/experiment/secret.csv" not in copied

        window._show_error("/private/experiment/secret.csv", "unsafe code")
        about.copy_diagnostics_button.click()
        assert "Last error code: unknown" in qapp.clipboard().text()
        assert "/private/experiment/secret.csv" not in qapp.clipboard().text()
        about.open_log_folder_button.click()
        assert opened == [True]
        assert "Could not open" in about.feedback_label.text()
    finally:
        _dispose(window, qapp)
