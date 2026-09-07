"""Workspace menus and document lifetime in a real Qt window."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from gwexpy_studio.ui.bridge import BridgeResult, BridgeState
from gwexpy_studio.ui.window import MainWindow


class WorkspaceBridge(QObject):
    """Command-recording Qt bridge for window workflow assertions."""

    result_received = Signal(object)
    safe_to_destroy = Signal()
    state_changed = Signal(str)
    progress_received = Signal(object)
    state = BridgeState.IDLE

    def __init__(self):
        """Initialize independent test state."""
        super().__init__()
        self.commands = []
        self.closed = []
        self.cancels = 0
        self.worker_thread = SimpleNamespace(isRunning=lambda: False)

    def send_command(self, kind, payload):
        """Record commands without running scientific code."""
        self.commands.append((kind, payload))
        return f"command-{len(self.commands)}"

    def close(self, **kwargs):
        """Record cleanup without creating a data worker."""
        self.closed.append(kwargs)

    def request_cancel(self):
        """Record a cancellation request."""
        self.cancels += 1


@pytest.mark.contract("GUI-WSP-0008")
@pytest.mark.gui
def test_workspace_menus_keep_open_data_and_capture_save_state(qapp):
    """Workspace menus keep open data and capture save state."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window.show()
    window.save_project_to("/tmp/example.gwxproj")
    assert bridge.commands[-1][0] == "save_project"
    assert bridge.commands[-1][1]["path"] == "/tmp/example.gwxproj"
    state = bridge.commands[-1][1]["ui_state"]
    assert "layout" in state and "selection" in state and "panel_drafts" in state
    assert window.open_action.text().replace("&", "").startswith("Open Data")
    assert window.open_project_action.shortcut().toString() == "Ctrl+O"
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0009")
@pytest.mark.gui
def test_cancel_unsaved_close_never_shuts_down_bridge(qapp, monkeypatch):
    """Cancel unsaved close never shuts down bridge."""
    from PySide6.QtCore import QEvent
    from shiboken6 import isValid

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"dirty": True, "revision": 4, "generation": 0}
    window.show()
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
    )
    window.close()
    qapp.processEvents()
    assert window.isVisible()
    assert bridge.closed == []
    assert not window._pending_close
    window.deleteLater()
    # processEvents alone does not deliver DeferredDelete without app.exec().
    qapp.sendPostedEvents(window, QEvent.Type.DeferredDelete)
    assert not isValid(window)
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0010")
@pytest.mark.gui
def test_open_restores_unapproved_drafts_without_loading_arrays(qapp):
    """Open restores unapproved drafts without loading arrays."""
    from gwexpy_studio.domain.model import DataObjectRef
    from gwexpy_studio.domain.project import Project

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    obj = DataObjectRef(
        object_id="a", kind="TimeSeries", shape=(2,), dtype="float64", unit="m"
    )
    project = Project(
        objects=(obj,),
        ui_state={
            "selection": {"object_id": "a", "selector": None},
            "panel_drafts": {
                "open_data_panel": {
                    "datatype": "TimeSeries",
                    "format": "custom",
                    "paths": "入力.dat",
                    "args": "[]",
                    "kwargs": '{"scale":2}',
                }
            },
        },
    )
    window._dispatch_command(
        "open_project", {"path": "/tmp/doc.gwxproj"}, pending_action="open_project"
    )
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=True,
            payload={
                "project": project,
                "workspace_status": {
                    "generation": 1,
                    "revision": 1,
                    "dirty": False,
                    "needs_restore": True,
                    "active_object_ids": ["a"],
                    "resident_object_ids": [],
                },
            },
        )
    )
    assert len(bridge.commands) == 1
    assert window._current_object_id == "a"
    assert not window.crop_action.isEnabled()
    assert window.restore_project_action.isEnabled()
    assert "not restored" in window.source_tree.topLevelItem(0).text(1)
    assert window.open_data_panel.paths_edit.toPlainText() == "入力.dat"
    assert not window.open_data_panel.confirm_button.isEnabled()
    assert window.open_data_panel._inspection is None
    assert window.plot_canvas._preview is None
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0011")
@pytest.mark.gui
def test_failed_save_keeps_document_and_cancels_pending_switch(qapp):
    """Failed save keeps document and cancels pending switch."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    project = window.project
    window._workspace_status = {"dirty": True, "revision": 2, "generation": 0}
    window._after_save = ("new_project", {})
    window.save_project_to("/missing/doc.gwxproj")
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=False,
            error_code="save_failed",
            error_message="Disk full",
        )
    )
    assert window.project is project
    assert window._workspace_status["dirty"]
    assert window._after_save is None
    assert len(bridge.commands) == 1
    assert "Disk full" in window.statusBar().currentMessage()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0012")
@pytest.mark.gui
def test_ui_checkpoint_debounces_drafts_and_does_not_clear_dirty(qapp):
    """Ui checkpoint debounces drafts and does not clear dirty."""
    from PySide6.QtTest import QTest

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"dirty": True, "revision": 1, "generation": 0}
    window.show_parameter_panel("timeseries.lowpass")
    window.parameter_panel.fields["frequency"].setText("10")
    QTest.qWait(500)
    window.parameter_panel.fields["frequency"].setText("20")
    QTest.qWait(600)
    assert not bridge.commands
    QTest.qWait(500)
    assert len(bridge.commands) == 1
    assert bridge.commands[0][0] == "set_ui_state"
    assert (
        bridge.commands[0][1]["ui_state"]["panel_drafts"]["parameter_panel"]["saved"][
            "timeseries.lowpass"
        ]["frequency"]
        == "20"
    )
    assert window._workspace_status["dirty"]
    window.parameter_panel.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0013")
@pytest.mark.gui
def test_unsaved_save_completes_before_project_switch(qapp, monkeypatch):
    """Unsaved save completes before project switch."""
    from PySide6.QtWidgets import QFileDialog

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"dirty": True, "revision": 1, "generation": "doc"}
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args: QMessageBox.StandardButton.Save
    )
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *args: ("/tmp/saved.gwxproj", "")
    )
    window._request_document_change("new_project", {})
    assert [kind for kind, _ in bridge.commands] == ["save_project"]
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=True,
            payload={
                "project": window.project,
                "workspace_status": {
                    "dirty": False,
                    "revision": 2,
                    "generation": "doc",
                },
            },
        )
    )
    assert [kind for kind, _ in bridge.commands] == ["save_project", "new_project"]
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0014")
@pytest.mark.gui
def test_busy_interrupt_waits_for_result_before_switch(qapp, monkeypatch):
    """Busy interrupt waits for result before switch."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"dirty": False}
    window._dispatch_command("start", {}, pending_action="start")
    bridge.state = BridgeState.RUNNING
    monkeypatch.setattr(window, "_choose_busy", lambda: "interrupt")
    window._request_document_change("new_project", {})
    assert bridge.cancels == 1
    assert len(bridge.commands) == 1
    bridge.state = BridgeState.IDLE
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=False,
            error_code="cancelled",
            error_message="Cancelled",
        )
    )
    qapp.processEvents()
    assert bridge.commands[-1][0] == "new_project"
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0015")
@pytest.mark.gui
def test_restore_cancel_and_recovery_error_remain_visible(qapp):
    """Restore cancel and recovery error remain visible."""
    from PySide6.QtWidgets import QPushButton

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"dirty": True, "recovery_error": "Recovery disk full"}
    window._update_workspace_actions()
    window._dispatch_command("review_restore", {}, pending_action="review_restore")
    window._show_restore_progress("Reviewing")
    window._on_workspace_progress(
        {"completed": 2, "total": 5, "label": "Checking input"}
    )
    assert window._restore_progress.value() == 2
    window._restore_progress.findChild(QPushButton).click()
    assert bridge.cancels == 1
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=False,
            error_code="cancelled",
            error_message="Restoration cancelled",
        )
    )
    assert window._restore_progress is None
    assert bridge.cancels == 1
    assert window.recovery_status.text() == "Recovery disk full"
    window.deleteLater()
    qapp.processEvents()


def click_modal_button(text):
    """Activate the named button in the real confirmation dialog."""
    from PySide6.QtWidgets import QApplication

    dialog = QApplication.activeModalWidget()
    next(
        button for button in dialog.buttons() if button.text().replace("&", "") == text
    ).click()


@pytest.mark.gui
@pytest.mark.parametrize(
    "choice, expected",
    [
        pytest.param(
            "Restore",
            "recover_project",
            marks=pytest.mark.contract("GUI-WSP-0016"),
            id="restore",
        ),
        pytest.param(
            "Discard",
            "discard_recovery",
            marks=pytest.mark.contract("GUI-WSP-0017"),
            id="discard",
        ),
        pytest.param(
            "Later", None, marks=pytest.mark.contract("GUI-WSP-0018"), id="later"
        ),
    ],
)
def test_recovery_choices_are_explicit_and_never_replay_data(
    qapp, choice, expected, monkeypatch
):
    """Recovery choices are explicit and never replay data."""
    from PySide6.QtCore import QTimer

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    QTimer.singleShot(0, lambda: click_modal_button(choice))
    window._show_recovery_candidates(
        [{"run_id": "unfinished", "project_path": "/tmp/document.gwxproj"}]
    )
    assert bridge.commands == (
        [] if expected is None else [(expected, {"run_id": "unfinished"})]
    )
    if choice == "Restore":
        window._clear_pending_command()
        window._update_command_state()
        current = window.project
        status = {"dirty": True, "project_path": "/tmp/current.gwxproj"}
        window._workspace_status = status
        for decision in (
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        ):
            monkeypatch.setattr(QMessageBox, "warning", lambda *args: decision)
            window.recoveries_action.trigger()
            command_id = f"command-{len(bridge.commands)}"
            QTimer.singleShot(0, lambda: click_modal_button("Restore"))
            window._on_bridge_result(
                BridgeResult(
                    command_id=command_id,
                    success=True,
                    payload={
                        "project": current,
                        "workspace_status": status,
                        "recoveries": [
                            {
                                "run_id": "unfinished",
                                "original_path": "/tmp/recovered.gwxproj",
                            }
                        ],
                    },
                )
            )
            if decision == QMessageBox.StandardButton.Cancel:
                assert bridge.commands[-1][0] == "list_recoveries"
                assert window._after_save is None
            else:
                assert bridge.commands[-1][0] == "save_project"
                window._on_bridge_result(
                    BridgeResult(
                        command_id=f"command-{len(bridge.commands)}",
                        success=False,
                        error_code="save_failed",
                        error_message="Disk full",
                    )
                )
                assert window._after_save is None
            assert window.project is current
            assert window._workspace_status["dirty"]
        assert [kind for kind, _ in bridge.commands] == [
            "recover_project",
            "list_recoveries",
            "list_recoveries",
            "save_project",
        ]
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0019")
@pytest.mark.gui
def test_restore_review_displays_details_before_explicit_confirmation(qapp):
    """Restore review displays details before explicit confirmation."""
    from PySide6.QtCore import QTimer

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    review = {
        "message": "Environment changed; new results will be recorded",
        "sources": [{"paths": ["入力.h5"], "verified": False}],
        "targets": ["output"],
        "environment_differences": {"gwexpy": {"recorded": "0.1", "current": "0.2"}},
        "token": "private-review-token",
        "document_digest": "internal-digest",
    }
    displayed = []

    def inspect_review():
        from PySide6.QtWidgets import QApplication

        dialog = QApplication.activeModalWidget()
        displayed.append((dialog.informativeText(), dialog.detailedText()))
        click_modal_button("OK")

    QTimer.singleShot(0, inspect_review)
    window._review_restore_response(review)
    assert "入力.h5" in displayed[0][0]
    assert "0.1 → 0.2" in displayed[0][0]
    assert "unverified" in displayed[0][0].lower()
    assert "1" in displayed[0][0]
    assert "private-review-token" not in displayed[0][1]
    assert "internal-digest" not in displayed[0][1]
    assert bridge.commands == [
        ("restore_project", {"review": review, "confirmed": True})
    ]
    assert window._restore_progress is not None
    window._restore_progress.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0020")
@pytest.mark.gui
def test_malformed_ui_state_keeps_opened_document_usable(qapp):
    """Malformed optional widget state cannot escape a Qt result slot."""
    from gwexpy_studio.ui.workspace_state import restore_ui_state

    window = MainWindow(bridge=WorkspaceBridge())
    restore_ui_state(
        window, {"layout": "日本語", "selection": "bad", "panel_drafts": []}
    )
    assert not window._restoring_ui
    assert "UI state" in window.statusBar().currentMessage()
    restore_ui_state(window, {"panel_drafts": {"parameter_panel": {"saved": []}}})
    assert not window._restoring_ui
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0021")
@pytest.mark.gui
def test_recovery_status_shows_last_checkpoint_and_environment_change(qapp):
    """Persistent recovery status identifies the recoverable checkpoint."""
    window = MainWindow(bridge=WorkspaceBridge())
    window._workspace_status = {
        "recovery_error": "Disk full",
        "last_checkpoint_at": "2026-09-06T12:00:00Z",
        "environment_changed": True,
    }
    window._update_workspace_actions()
    assert "2026-09-06T12:00:00Z" in window.recovery_status.text()
    assert "environment" in window.recovery_status.text().lower()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0022")
@pytest.mark.gui
def test_mutation_flushes_current_selection_and_draft_before_debounce(qapp):
    """An immediate analysis gesture carries the current editable UI state."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"dirty": False}
    window._current_object_id = "new-selection"
    window.show_parameter_panel("timeseries.lowpass")
    window.parameter_panel.fields["frequency"].setText("23")
    window._dispatch_command(
        "apply",
        {"op_name": "timeseries.lowpass", "input_id": "new-selection"},
        pending_action="apply",
    )
    state = bridge.commands[0][1]["ui_state"]
    assert state["selection"]["object_id"] == "new-selection"
    assert (
        state["panel_drafts"]["parameter_panel"]["saved"]["timeseries.lowpass"][
            "frequency"
        ]
        == "23"
    )
    assert not window._checkpoint_timer.isActive()
    window.parameter_panel.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0023")
@pytest.mark.gui
def test_busy_fatal_transition_waits_for_stopped_thread_before_document_change(
    qapp, monkeypatch
):
    """A fatal result does not lose the user's deferred project switch."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"dirty": False}
    window._after_busy = ("new_project", {})
    bridge.state = BridgeState.FAILED
    bridge.worker_thread = SimpleNamespace(isRunning=lambda: True)
    changes = []
    monkeypatch.setattr(
        window, "_request_document_change", lambda *args: changes.append(args)
    )
    window._continue_after_busy()
    assert not changes
    assert window._after_busy == ("new_project", {})
    bridge.worker_thread = SimpleNamespace(isRunning=lambda: False)
    bridge.safe_to_destroy.emit()
    qapp.processEvents()
    assert changes == [("new_project", {})]
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0024")
@pytest.mark.gui
def test_restore_keeps_member_selection_and_requests_that_member_preview(qapp):
    """Restoration preserves a selected container member instead of its parent."""
    from gwexpy_studio.domain.model import DataObjectRef, MemberRef

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    member = MemberRef(
        selector={"key": "second"},
        label="second",
        kind="FrequencySeries",
        shape=(2,),
        dtype="complex128",
        unit="",
    )
    parent = DataObjectRef(
        object_id="batch",
        kind="FrequencySeriesDict",
        shape=(1,),
        dtype=None,
        unit=None,
        members=(member,),
    )
    window.project.objects = (parent,)
    window._current_object_id = "batch"
    window._workspace_status = {"resident_object_ids": []}
    window._refresh_project_views(selected_object_id="batch")
    parent_item = window._find_object_item(window.source_tree, "batch")
    window.source_tree.setCurrentItem(parent_item.child(0))
    assert window._current_member == member
    assert not bridge.commands
    window._dispatch_command(
        "restore_project",
        {"review": {}, "confirmed": True},
        pending_action="restore_project",
    )
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=True,
            payload={
                "project": window.project,
                "workspace_status": {
                    "resident_object_ids": ["batch"],
                    "active_object_ids": ["batch"],
                },
            },
        )
    )
    assert bridge.commands[-1] == (
        "member_preview",
        {"object_id": "batch", "selector": {"key": "second"}},
    )
    assert window._current_member == member
    from dataclasses import replace

    from gwexpy_studio.ui.workspace_state import capture_ui_state, restore_ui_state

    state = capture_ui_state(window)
    window.project.objects = (replace(parent, members=()),)
    restore_ui_state(window, state)
    assert window._current_member == member
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0025")
@pytest.mark.gui
def test_later_recovery_reaches_next_candidate_and_displays_original_path(qapp):
    """Every startup recovery remains reachable without automatic restoration."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    observed = []

    def choose_later():
        observed.append(QApplication.activeModalWidget().text())
        click_modal_button("Later")
        QTimer.singleShot(0, choose_discard)

    def choose_discard():
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            QTimer.singleShot(1, choose_discard)
            return
        observed.append(dialog.text())
        click_modal_button("Discard")

    QTimer.singleShot(0, choose_later)
    window._show_recovery_candidates(
        [
            {"run_id": "one", "original_path": "最初.gwxproj"},
            {"run_id": "two", "original_path": "次.gwxproj"},
        ]
    )
    qapp.processEvents()
    assert observed == ["最初.gwxproj", "次.gwxproj"]
    assert bridge.commands == [("discard_recovery", {"run_id": "two"})]
    assert window.recoveries_action.text() == "Review Recoveries…"
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0026")
@pytest.mark.gui
def test_control_z_in_text_editor_keeps_analysis_history_unchanged(qapp):
    """The analysis shortcut gives a focused text editor its local undo."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    window._workspace_status = {"can_undo": True, "can_redo": True}
    window.show_parameter_panel("timeseries.lowpass")
    window.parameter_panel.show()
    editor = window.parameter_panel.fields["frequency"]
    editor.setFocus()
    QTest.keyClicks(editor, "24")
    window._update_workspace_actions()
    QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert editor.text() == ""
    assert not bridge.commands
    window.parameter_panel.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0027")
@pytest.mark.gui
def test_late_snapshot_cannot_replace_current_project_or_release_new_command(qapp):
    """Command identity rejects delayed snapshots from an earlier request."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    current = window.project
    window._workspace_status = {"generation": "new", "revision": 5}
    window._dispatch_command(
        "set_ui_state", {"ui_state": {}}, pending_action="set_ui_state"
    )
    window._on_bridge_result(
        BridgeResult(
            command_id="old-command",
            success=True,
            payload={
                "workspace_status": {"generation": "old", "revision": 90},
            },
        )
    )
    assert window.project is current
    assert window._workspace_status["generation"] == "new"
    assert window._command_reserved
    window.deleteLater()
    qapp.processEvents()
