"""Document commands, confirmation, recovery, and detached workspace snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSignalBlocker, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QTextEdit,
)

from ..domain.project import Project
from .bridge import BridgeResult, BridgeState, WorkerBridge
from .signal_tools import SignalTools
from .workspace_commands import WORKSPACE_REQUIRED
from .workspace_dialogs import workspace_dialog
from .workspace_review import public_review_details, restoration_summary
from .workspace_state import capture_ui_state, restore_ui_state, tree_items


class WorkspaceTools(SignalTools):
    """Keep document lifetime independent from disposable worker ownership."""

    def _init_workspace(self: Any, file_menu: QMenu) -> None:
        self._workspace_status: dict[str, Any] = {}
        self._restoring_ui = False
        self._workspace_close_authorized = False
        self._after_save = None
        self._after_view_save = None
        self._queued_plot_spec = None
        self._after_busy = None
        self._restore_progress = None
        self._remaining_recoveries: list[Mapping[str, Any]] = []
        self._initial_project_path: Path | None = None
        self._capability_start_continuation: tuple[str, bool] | None = None
        self._checkpoint_pending = False
        self._checkpoint_timer = QTimer(self)
        self._checkpoint_timer.setSingleShot(True)
        self._checkpoint_timer.setInterval(1000)
        self._checkpoint_timer.timeout.connect(self._flush_ui_checkpoint)
        self.recovery_status = QLabel(self)
        self.recovery_status.setStyleSheet("color: #bd3a30")
        self.statusBar().addPermanentWidget(self.recovery_status)
        self.recovery_notice = QLabel(self)
        self.recovery_notice.setStyleSheet("color: #8a5a00")
        self.review_recovery_notice_button = QPushButton("Review Recovery", self)
        self.review_recovery_notice_button.clicked.connect(
            self._review_initial_project_recovery
        )
        self.keep_saved_version_button = QPushButton("Keep Saved Version", self)
        self.keep_saved_version_button.clicked.connect(self._hide_recovery_notice)
        self.statusBar().addPermanentWidget(self.recovery_notice)
        self.statusBar().addPermanentWidget(self.review_recovery_notice_button)
        self.statusBar().addPermanentWidget(self.keep_saved_version_button)
        self._hide_recovery_notice()
        self.open_action.setText("Open &Data…")
        self.open_action.setShortcut("Ctrl+Shift+O")
        definitions = (
            (
                "new_project_action",
                "New Project",
                "Ctrl+N",
                lambda: self._request_document_change("new_project", {}),
            ),
            (
                "open_project_action",
                "Open Project…",
                "Ctrl+O",
                self._open_project_dialog,
            ),
            ("save_project_action", "Save", "Ctrl+S", lambda: self.save_project_to()),
            (
                "save_project_as_action",
                "Save As…",
                "Ctrl+Shift+S",
                lambda: self.save_project_to(save_as=True),
            ),
            (
                "close_project_action",
                "Close Project",
                "Ctrl+W",
                lambda: self._request_document_change("close_project", {}),
            ),
            (
                "restore_project_action",
                "Review / Restore Data…",
                "",
                self.review_restore,
            ),
            ("recoveries_action", "Review Recoveries…", "", self.initialize_workspace),
        )
        for name, label, shortcut, callback in definitions:
            action = QAction(label, self)
            if shortcut:
                action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file_menu.insertAction(self.open_action, action)
            setattr(self, name, action)
        edit = self.menuBar().addMenu("&Edit")
        self.undo_analysis_action = QAction("Undo Analysis", self)
        self.undo_analysis_action.setShortcut("Ctrl+Z")
        self.undo_analysis_action.triggered.connect(
            lambda: self._analysis_history(False)
        )
        self.redo_analysis_action = QAction("Redo Analysis", self)
        self.redo_analysis_action.setShortcuts(["Ctrl+Shift+Z", "Ctrl+Y"])
        self.redo_analysis_action.triggered.connect(
            lambda: self._analysis_history(True)
        )
        edit.addActions([self.undo_analysis_action, self.redo_analysis_action])
        from .workspace_view import WorkspaceViewHistory

        self.view_history = WorkspaceViewHistory(self, edit)
        for index, dock in enumerate(
            (self.source_dock, self.metadata_dock, self.history_dock)
        ):
            dock.setObjectName(f"workspace-dock-{index}")
            dock.dockLocationChanged.connect(self.view_history.layout_changed)
            dock.topLevelChanged.connect(self.view_history.layout_changed)
            dock.visibilityChanged.connect(self._queue_ui_checkpoint)
        self.view_history.capture_layout()
        self.source_tree.itemSelectionChanged.connect(self._queue_ui_checkpoint)
        self.source_tree.itemExpanded.connect(self._queue_ui_checkpoint)
        self.source_tree.itemCollapsed.connect(self._queue_ui_checkpoint)
        self._update_workspace_actions()

    def initialize_workspace(self: Any) -> None:
        """Offer recovery candidates without starting scientific execution."""
        if getattr(self, "_io_capability_ready", None) is not True:
            self._start_for_worker_capabilities("list_recoveries")
            return
        self._dispatch_command("list_recoveries", {}, pending_action="list_recoveries")

    def start_initial_workspace(self: Any, project_path: str | Path) -> None:
        """Open an explicit project before considering recovery candidates.

        This is the installed-launcher path.  It intentionally delegates no
        recovery action until the saved project has opened and its identity is
        available for a non-blocking comparison.
        """
        self._initial_project_path = (
            Path(project_path).expanduser().resolve(strict=False)
        )
        if getattr(self, "_io_capability_ready", None) is not True:
            self._start_for_worker_capabilities("open_initial_project")
            return
        self._request_document_change(
            "open_project", {"path": str(self._initial_project_path)}
        )

    def _start_for_worker_capabilities(
        self: Any, continuation: str, *, redo: bool = False
    ) -> None:
        """Start the worker before exposing any open/save data or project command."""
        if self._command_reserved:
            return
        self._io_capability_ready = False
        self._capability_start_continuation = (continuation, redo)
        self._dispatch_command(
            "start", {}, pending_action="worker_capability_start"
        )

    def _continue_after_worker_capabilities(self: Any) -> None:
        """Resume the requested launch route only after a validated snapshot arrives."""
        continuation_record, self._capability_start_continuation = (
            self._capability_start_continuation,
            None,
        )
        continuation, redo = continuation_record or (None, False)
        if continuation == "list_recoveries":
            self.initialize_workspace()
        elif (
            continuation == "open_initial_project"
            and self._initial_project_path is not None
        ):
            self.start_initial_workspace(self._initial_project_path)
        elif continuation == "review_restore":
            self.review_restore(redo=redo)

    def _hide_recovery_notice(self: Any) -> None:
        """Hide the explicit-project recovery notice without changing state."""
        self.recovery_notice.hide()
        self.review_recovery_notice_button.hide()
        self.keep_saved_version_button.hide()

    def _review_initial_project_recovery(self: Any) -> None:
        """Let the user explicitly enter the existing recovery review flow."""
        self._hide_recovery_notice()
        self.initialize_workspace()

    def _show_initial_project_recovery_notice(self: Any) -> None:
        """Show a non-modal notice that never restores recovery automatically."""
        self.recovery_notice.setText("A newer recovery is available.")
        self.recovery_notice.show()
        self.review_recovery_notice_button.show()
        self.keep_saved_version_button.show()

    @staticmethod
    def _parse_recovery_timestamp(value: object) -> datetime | None:
        """Parse an ISO timestamp, accepting the UTC `Z` spelling used by APIs."""
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(
                f"{value[:-1]}+00:00" if value.endswith("Z") else value
            )
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed

    def _is_newer_same_project_recovery(
        self: Any, candidate: Mapping[str, Any], project_path: Path
    ) -> bool:
        """Return whether one candidate merits a safe, non-blocking warning."""
        original_path = candidate.get("original_path")
        candidate_project_id = candidate.get("project_id")
        if not isinstance(original_path, str) or not isinstance(
            candidate_project_id, str
        ):
            return False
        if not candidate_project_id or candidate_project_id != self._project.project_id:
            return False
        try:
            if Path(original_path).expanduser().resolve(strict=False) != project_path:
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        created = self._parse_recovery_timestamp(candidate.get("created_at"))
        modified = self._parse_recovery_timestamp(self._project.modified)
        if created is None or modified is None:
            # An identity-matched malformed timestamp is still worth exposing;
            # the user, not Studio, decides whether to inspect it.
            return True
        return created > modified

    def _handle_initial_project_recoveries(
        self: Any, candidates: list[Mapping[str, Any]], project_path: Path
    ) -> None:
        """Display at most a notice for a recovery of the explicit project."""
        if any(
            self._is_newer_same_project_recovery(candidate, project_path)
            for candidate in candidates
        ):
            self._show_initial_project_recovery_notice()

    def _wire_workspace_panel(self: Any, name: str, panel: Any) -> None:
        if name == "parameter_panel":
            panel.set_objects(self._object_handles())
            panel.apply_requested.connect(
                lambda value: self._panel_dispatch(panel, "apply_multi", value)
            )
            panel.preview_requested.connect(
                lambda value: self._panel_dispatch(panel, "filter_preview", value)
            )
        else:
            panel.catalog_requested.connect(
                lambda value: self._panel_dispatch(panel, "catalog_io", value)
            )
            if name == "open_data_panel":
                panel.inspect_requested.connect(
                    lambda value: self._panel_dispatch(panel, "inspect_io", value)
                )
                panel.read_requested.connect(
                    lambda value: self._panel_dispatch(panel, "read_io", value)
                )
            else:
                panel.write_requested.connect(self._write_selected_data)
        panel.draft_changed.connect(self._queue_ui_checkpoint)

    def _queue_ui_checkpoint(self: Any, *_args: Any) -> None:
        if self._restoring_ui or not self._workspace_status:
            return
        self._checkpoint_pending = True
        self._checkpoint_timer.start()

    def _flush_ui_checkpoint(self: Any) -> None:
        if not self._checkpoint_pending:
            return
        if (
            self._bridge_state() is not BridgeState.IDLE
            or self._command_reserved
            or self._modal_active
        ):
            self._checkpoint_timer.start()
            return
        self._checkpoint_pending = False
        self._dispatch_command(
            "set_ui_state",
            {"ui_state": capture_ui_state(self)},
            pending_action="set_ui_state",
        )

    def _drain_view_changes(self: Any) -> None:
        if (
            self._bridge_state() is not BridgeState.IDLE
            or self._command_reserved
            or self._modal_active
        ):
            return
        if self._queued_plot_spec is not None:
            spec, self._queued_plot_spec = self._queued_plot_spec, None
            self._save_plot_spec(spec)
        elif self._after_view_save is not None:
            path, save_as = self._after_view_save
            self._after_view_save = None
            self.save_project_to(path, save_as=save_as)

    def _ensure_workspace_bridge(self: Any) -> bool:
        if self._bridge_state() is BridgeState.IDLE:
            return True
        if self._bridge.worker_thread.isRunning():
            self._show_error(
                "bridge_busy", "Wait for the previous worker to finish stopping"
            )
            return False
        old = self._bridge
        controller = old.take_controller()
        self._bridge = WorkerBridge(controller=controller, parent=self)
        self._connect_bridge()
        self._clear_pending_command()
        old.deleteLater()
        return True

    def _open_project_dialog(self: Any) -> None:
        path, _ = workspace_dialog(
            self,
            QFileDialog.getOpenFileName,
            self,
            "Open Project",
            "",
            "Studio projects (*.gwxproj);;All files (*)",
        )
        if path:
            self._request_document_change("open_project", {"path": path})

    def save_project_to(
        self: Any, path: str | None = None, *, save_as: bool = False
    ) -> bool:
        """Save the document together with the current detached UI state."""
        self.plot_canvas.flush_view_changes()
        if (
            self._pending_action == "signal_set_plot"
            or self._queued_plot_spec is not None
        ):
            self._after_view_save = (path, save_as)
            return True
        if path is None and (
            save_as
            or not self._workspace_status.get("project_path")
            or self._workspace_status.get("read_only")
        ):
            path, _ = workspace_dialog(
                self,
                QFileDialog.getSaveFileName,
                self,
                "Save Project",
                "project.gwxproj",
                "Studio projects (*.gwxproj)",
            )
            if not path:
                self._after_save = None
                return False
        if self._queued_plot_spec is not None:
            self._after_view_save = (path, False)
            self._drain_view_changes()
            return True
        if not self._ensure_workspace_bridge():
            return False
        self._checkpoint_timer.stop()
        self._checkpoint_pending = False
        return self._dispatch_command(
            "save_project",
            {"path": path, "ui_state": capture_ui_state(self)},
            pending_action="save_project",
        )

    def _choose_busy(self: Any) -> str:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Operation in progress")
        dialog.setText(
            "Wait for the operation, or interrupt it before changing projects."
        )
        wait = dialog.addButton("Wait", QMessageBox.ButtonRole.AcceptRole)
        interrupt = dialog.addButton(
            "Interrupt", QMessageBox.ButtonRole.DestructiveRole
        )
        dialog.addButton(QMessageBox.StandardButton.Cancel)
        workspace_dialog(self, dialog.exec)
        return (
            "wait"
            if dialog.clickedButton() is wait
            else "interrupt"
            if dialog.clickedButton() is interrupt
            else "cancel"
        )

    def _confirm_unsaved(self: Any) -> str:
        if not (
            self._workspace_status.get("dirty")
            or self._checkpoint_pending
            or self.plot_canvas._view_timer.isActive()
        ):
            return "discard"
        choice = workspace_dialog(
            self,
            QMessageBox.warning,
            self,
            "Unsaved project",
            "Save your project before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        return (
            "save"
            if choice == QMessageBox.StandardButton.Save
            else "discard"
            if choice == QMessageBox.StandardButton.Discard
            else "cancel"
        )

    def _request_document_change(self: Any, kind: str, payload: dict[str, Any]) -> bool:
        if self._workspace_status and (
            self._bridge_state() is BridgeState.RUNNING or self._command_reserved
        ):
            choice = self._choose_busy()
            if choice != "cancel":
                self._after_busy = (kind, payload)
                if choice == "interrupt":
                    self._bridge.request_cancel()
                QTimer.singleShot(0, self._continue_after_busy)
            return False
        choice = self._confirm_unsaved()
        if choice == "cancel":
            return False
        if choice == "save":
            self._after_save = (kind, payload)
            self.save_project_to()
            return False
        return self._perform_document_change(kind, payload)

    def _perform_document_change(self: Any, kind: str, payload: dict[str, Any]) -> bool:
        if kind == "exit":
            self._workspace_close_authorized = True
            return True
        if not self._ensure_workspace_bridge():
            return False
        self._checkpoint_timer.stop()
        return self._dispatch_command(kind, payload, pending_action=kind)

    def _analysis_history(self: Any, redo: bool) -> None:
        editor = QApplication.focusWidget()
        if isinstance(editor, (QLineEdit, QTextEdit, QPlainTextEdit)):
            editor.redo() if redo else editor.undo()
            return
        kind = "redo_analysis" if redo else "undo_analysis"
        self._dispatch_command(kind, {}, pending_action=kind)

    def review_restore(self: Any, *, redo: bool = False) -> None:
        """Review source identities before explicit data restoration."""
        if not self._ensure_workspace_bridge():
            return
        if getattr(self, "_io_capability_ready", None) is not True:
            self._start_for_worker_capabilities("review_restore", redo=redo)
            return
        if self._dispatch_command(
            "review_restore", {"redo": redo}, pending_action="review_restore"
        ):
            self._show_restore_progress("Reviewing source files…")

    def _show_restore_progress(self: Any, text: str) -> None:
        self._restore_progress = QProgressDialog(text, "Cancel", 0, 0, self)
        self._restore_progress.setWindowTitle("Restore Data")
        self._restore_progress.setMinimumDuration(0)
        self._restore_progress.canceled.connect(self._bridge.request_cancel)
        self._restore_progress.show()

    def _on_workspace_progress(self: Any, progress: Mapping[str, Any]) -> None:
        if self._restore_progress is not None:
            self._restore_progress.setLabelText(
                str(progress.get("label", "Restoring…"))
            )
            self._restore_progress.setMaximum(int(progress.get("total", 0)))
            self._restore_progress.setValue(int(progress.get("completed", 0)))

    def _review_restore_response(self: Any, review: Mapping[str, Any]) -> None:
        import json

        dialog = QMessageBox(self)
        dialog.setWindowTitle("Review restoration")
        dialog.setText(
            "Recalculate the active analysis from the reviewed source files?"
        )
        dialog.setInformativeText(restoration_summary(review))
        dialog.setDetailedText(
            json.dumps(public_review_details(review), indent=2, ensure_ascii=False)
        )
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if workspace_dialog(self, dialog.exec) == QMessageBox.StandardButton.Ok:
            if self._dispatch_command(
                "restore_project",
                {"review": dict(review), "confirmed": True},
                pending_action="restore_project",
            ):
                self._show_restore_progress("Restoring analysis…")

    def _show_recovery_candidates(
        self: Any, candidates: list[Mapping[str, Any]]
    ) -> None:
        if not candidates:
            return
        candidate = candidates[0]
        self._remaining_recoveries = candidates[1:]
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Recover unfinished work")
        dialog.setText(
            str(
                candidate.get("original_path")
                or candidate.get("project_path")
                or candidate.get("run_id", "Recovered project")
            )
        )
        dialog.setInformativeText(
            "Restore opens an unsaved document. Data writes and interrupted "
            "operations are not retried."
        )
        restore = dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
        discard = dialog.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        workspace_dialog(self, dialog.exec, dialog_instance=dialog)
        kind = (
            "recover_project"
            if dialog.clickedButton() is restore
            else "discard_recovery"
            if dialog.clickedButton() is discard
            else None
        )
        if kind == "recover_project":
            self._remaining_recoveries = []
            self._request_document_change(kind, {"run_id": candidate["run_id"]})
        elif kind:
            self._dispatch_command(
                kind, {"run_id": candidate["run_id"]}, pending_action=kind
            )
        elif self._remaining_recoveries:
            QTimer.singleShot(
                0, lambda: self._show_recovery_candidates(self._remaining_recoveries)
            )

    def _handle_workspace_result(self: Any, result: BridgeResult) -> bool:
        payload = result.payload if isinstance(result.payload, Mapping) else {}
        status = payload.get("workspace_status")
        if isinstance(status, Mapping):
            old = self._workspace_status
            if status.get("generation") == old.get("generation") and status.get(
                "revision", 0
            ) < old.get("revision", 0):
                return True
            self._workspace_status = dict(status)
            self._update_workspace_actions()
        kind = self._pending_action
        if kind not in WORKSPACE_REQUIRED:
            if not result.success and result.error_code in {
                "timeout",
                "worker_timeout",
                "worker_crashed",
            }:
                self._workspace_status["needs_restore"] = True
                self._workspace_status["resident_object_ids"] = []
                self.view_history.invalidate_data()
                self.plot_canvas.clear()
            if self._after_busy:
                QTimer.singleShot(0, self._continue_after_busy)
            return False
        self._clear_pending_command()
        if self._restore_progress is not None:
            with QSignalBlocker(self._restore_progress):
                self._restore_progress.close()
            self._restore_progress = None
        if isinstance(payload.get("project"), Project):
            self._project = payload["project"]
        if not result.success:
            self._after_save = None
            self._show_error(
                result.error_code or "workspace_failed",
                result.error_message or "Workspace command failed",
            )
            if kind == "open_project" and self._initial_project_path is not None:
                self._initial_project_path = None
                QTimer.singleShot(0, self.initialize_workspace)
            if result.error_code == "restore_required":
                QTimer.singleShot(
                    0, lambda: self.review_restore(redo=kind == "redo_analysis")
                )
        elif kind == "review_restore":
            self._review_restore_response(
                payload.get("review", payload.get("restore_review", {}))
            )
        elif kind == "list_recoveries":
            candidates = payload.get("recoveries", [])
            project_path, self._initial_project_path = (
                self._initial_project_path,
                None,
            )
            if project_path is None:
                self._show_recovery_candidates(candidates)
            else:
                self._handle_initial_project_recoveries(candidates, project_path)
        elif kind == "discard_recovery":
            self._show_recovery_candidates(self._remaining_recoveries)
        else:
            if kind == "restore_project":
                self.view_history.invalidate_data()
                self._member_cache.clear()
                self._member_loading.clear()
                self._member_has_more.clear()
                parent = self._selected_object()
                if self._current_member is not None and parent is not None:
                    selector = dict(self._current_member.selector)
                    self._current_member = next(
                        (
                            member
                            for member in parent.members
                            if dict(member.selector) == selector
                        ),
                        self._current_member,
                    )
                if self.parameter_panel is not None:
                    self.parameter_panel.set_objects(self._object_handles())
            if kind in {
                "new_project",
                "open_project",
                "close_project",
                "recover_project",
            }:
                self.view_history.stack.clear()
                self.view_history.invalidate_data()
                self._queued_plot_spec = None
                self._after_view_save = None
                self._member_cache.clear()
                for name in ("parameter_panel", "open_data_panel", "export_data_panel"):
                    panel = getattr(self, name, None)
                    if panel is not None:
                        panel.close()
                        panel.deleteLater()
                        setattr(self, name, None)
                self.plot_canvas.clear()
                restore_ui_state(self, dict(getattr(self._project, "ui_state", {})))
                self._show_welcome_for_empty_document()
                self.view_history.timer.stop()
                self.view_history.capture_layout()
                self._checkpoint_pending = False
            else:
                if kind in {"undo_analysis", "redo_analysis"}:
                    handles = self._workspace_status.get("selected_handles", [])
                    handle = handles[0] if handles else {}
                    self._current_object_id = handle.get("object_id")
                    self._current_member = None
                    parent = self._selected_object()
                    if parent is not None and handle.get("selector") is not None:
                        self._current_member = next(
                            (
                                member
                                for member in parent.members
                                if dict(member.selector) == handle["selector"]
                            ),
                            None,
                        )
                self._refresh_project_views(selected_object_id=self._current_object_id)
            if kind in {"restore_project", "undo_analysis", "redo_analysis"}:
                selected = self._selected_object()
                if selected is not None and self._is_resident(selected.object_id):
                    if self._current_member is not None:
                        self._dispatch_command(
                            "member_preview",
                            {
                                "object_id": selected.object_id,
                                "selector": dict(self._current_member.selector),
                            },
                            pending_action="signal_member_preview",
                        )
                    else:
                        self._select_object_and_request_preview(selected)
            self._show_status(
                "Project saved"
                if kind == "save_project"
                else "Project opened — select Review / Restore Data"
                if self._workspace_status.get("needs_restore")
                else "Workspace updated"
            )
            if kind == "save_project" and self._after_save:
                change, self._after_save = self._after_save, None
                if self._perform_document_change(*change) and change[0] == "exit":
                    QTimer.singleShot(0, self.close)
            if kind == "open_project" and self._initial_project_path is not None:
                self._dispatch_command(
                    "list_recoveries", {}, pending_action="list_recoveries"
                )
        self._update_command_state()
        if self._after_busy:
            QTimer.singleShot(0, self._continue_after_busy)
        return True

    def _continue_after_busy(self: Any) -> None:
        if not self._after_busy or self._command_reserved:
            return
        if (
            self._bridge_state() is not BridgeState.IDLE
            and self._bridge.worker_thread.isRunning()
        ):
            return
        change, self._after_busy = self._after_busy, None
        if self._request_document_change(*change) and change[0] == "exit":
            self.close()

    def _is_resident(self: Any, object_id: str) -> bool:
        return (
            "resident_object_ids" not in self._workspace_status
            or object_id in self._workspace_status["resident_object_ids"]
        )

    def _is_active(self: Any, object_id: str) -> bool:
        return (
            "active_object_ids" not in self._workspace_status
            or object_id in self._workspace_status["active_object_ids"]
        )

    def _update_workspace_actions(self: Any) -> None:
        if not hasattr(self, "save_project_action"):
            return
        status = self._workspace_status
        idle = (
            self._bridge_state() is BridgeState.IDLE
            and not self._command_reserved
            and not self._modal_active
            and getattr(self, "_io_capability_ready", None) is True
        )
        if idle and (
            self._queued_plot_spec is not None or self._after_view_save is not None
        ):
            QTimer.singleShot(0, self._drain_view_changes)
        for action in (
            self.new_project_action,
            self.close_project_action,
            self.recoveries_action,
        ):
            action.setEnabled(idle)
        self.open_project_action.setEnabled(idle)
        for action in (self.save_project_action, self.save_project_as_action):
            action.setEnabled(idle)
        self.undo_analysis_action.setEnabled(idle and bool(status.get("can_undo")))
        self.redo_analysis_action.setEnabled(idle and bool(status.get("can_redo")))
        if hasattr(self, "view_history"):
            self.view_history.undo_action.setEnabled(
                idle and self.view_history.stack.canUndo()
            )
            self.view_history.redo_action.setEnabled(
                idle and self.view_history.stack.canRedo()
            )
        # A detached project must retain one safe recovery entry point after a
        # worker failure.  It does not make normal data/project actions
        # available: Review / Restore first recreates the bridge and obtains a
        # fresh capability snapshot before it dispatches the restore command.
        restore_can_requalify = (
            bool(status.get("needs_restore"))
            and not self._command_reserved
            and not self._modal_active
            and (
                self._bridge_state() is BridgeState.IDLE
                or not self._bridge.worker_thread.isRunning()
            )
        )
        self.restore_project_action.setEnabled(restore_can_requalify)
        path = status.get("project_path")
        title = Path(path).name if path else "Untitled"
        if status:
            dirty = "*" if status.get("dirty") or self._checkpoint_pending else ""
            self.setWindowTitle(
                f"{dirty}{title} — GWexpy Studio"
                + (" [read only]" if status.get("read_only") else "")
            )
        messages = []
        if status.get("recovery_error"):
            messages.append(str(status["recovery_error"]))
            if status.get("last_checkpoint_at"):
                messages.append(
                    f"Last recoverable point: {status['last_checkpoint_at']}"
                )
        if status.get("environment_changed"):
            messages.append("Recomputed in a changed scientific environment")
        self.recovery_status.setText(" | ".join(messages))
        self.recovery_status.setToolTip(self.recovery_status.text())
        for item in tree_items(self):
            from PySide6.QtCore import Qt

            object_id = item.data(0, Qt.ItemDataRole.UserRole)
            if object_id and item.text(1).startswith(
                ("TimeSeries", "FrequencySeries", "Spectrogram")
            ):
                kind = item.text(1).split(" — ")[0]
                item.setText(
                    1,
                    kind if self._is_resident(object_id) else kind + " — not restored",
                )
