"""Main application window implementation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QMimeData, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QDockWidget,
    QFileDialog,
    QLabel,
    QListWidget,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from ..application.preview import PreviewPayload
from ..application.project_factory import create_alpha_project
from ..domain.model import DataObjectRef, MemberRef
from ..domain.project import Project
from ..errors import OperationError
from ..ops.source import SourceInspection
from .bridge import BridgeResult, BridgeState, CommandKind, WorkerBridge
from .dialogs import (
    AsdDialog,
    CropDialog,
    DetrendDialog,
    LoadConfirmationDialog,
    SpectrogramDialog,
)
from .models import (
    MEMBER_ROLE,
    populate_history_list,
    populate_metadata_tree,
    populate_source_tree,
)
from .plot_canvas import PlotCanvas
from .trial_support_window import TrialSupportTools
from .welcome import WelcomePanel
from .workspace_commands import WORKSPACE_GESTURES
from .workspace_state import capture_ui_state
from .workspace_window import WorkspaceTools


class MainWindow(TrialSupportTools, WorkspaceTools):
    """Studio main desktop window managing docks, menus, and bridge communication."""

    def __init__(
        self,
        *,
        bridge: WorkerBridge | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Initialize main window layout, menus, docks, and underlying bridge."""
        super().__init__(parent)
        self.setWindowTitle("GWexpy Studio")
        self.resize(1200, 800)

        self._project: Project = create_alpha_project()
        self._current_object_id: str | None = None
        self._pending_action: str | None = None
        self._pending_params: dict[str, Any] | None = None
        self._pending_inspection: SourceInspection | None = None
        self._pending_command_id: str | None = None
        self._command_reserved = False
        self._modal_active = False
        self._error_active = False
        self._pending_close = False
        self._close_retry_scheduled = False
        # Keep all data and project actions fail-closed from construction until
        # the worker returns a validated capability snapshot.
        self._io_capability_ready = False
        self._io_capability_snapshot: dict[str, object] | None = None
        self._init_trial_support()

        self._bridge = bridge if bridge is not None else WorkerBridge(parent=self)
        self._connect_bridge()

        self._init_ui()

    def _connect_bridge(self) -> None:
        bridge = self._bridge
        bridge.result_received.connect(
            lambda result: (
                self._on_bridge_result(result) if self._bridge is bridge else None
            )
        )
        bridge.safe_to_destroy.connect(
            lambda: (
                self._on_bridge_safe_to_destroy() if self._bridge is bridge else None
            )
        )
        bridge.state_changed.connect(
            lambda state: (
                self._on_bridge_state_changed(state) if self._bridge is bridge else None
            )
        )
        progress = getattr(bridge, "progress_received", None)
        if progress is not None:
            progress.connect(
                lambda value: (
                    self._on_workspace_progress(value)
                    if self._bridge is bridge
                    else None
                )
            )

    def _init_ui(self) -> None:
        """Initialize central view, dock panels, and menu actions."""
        # 1. Central Stacked Widget
        self.central_stack = QStackedWidget(self)
        self.welcome_panel = WelcomePanel(self)
        self.central_placeholder = QLabel(
            "No data loaded\n\nOpen a file or drop one here", self
        )
        self.central_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.plot_canvas = PlotCanvas(self)

        self.central_stack.addWidget(self.welcome_panel)
        self.central_stack.addWidget(self.central_placeholder)
        self.central_stack.addWidget(self.plot_canvas)
        self.setCentralWidget(self.central_stack)

        self.plot_canvas.cursor_moved.connect(self._on_cursor_moved)

        # 2. Source Dock
        self.source_dock = QDockWidget("Sources", self)
        self.source_tree = QTreeWidget(self.source_dock)
        self.source_tree.setHeaderLabels(["Source / Object", "Format / Kind"])
        self.source_dock.setWidget(self.source_tree)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.source_dock)
        self.source_tree.itemSelectionChanged.connect(self._on_source_tree_selection)

        # 3. Metadata Dock
        self.metadata_dock = QDockWidget("Metadata", self)
        self.metadata_tree = QTreeWidget(self.metadata_dock)
        self.metadata_tree.setHeaderLabels(["Property", "Value"])
        self.metadata_dock.setWidget(self.metadata_tree)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.metadata_dock)

        # 4. History Dock
        self.history_dock = QDockWidget("History", self)
        self.history_list = QListWidget(self.history_dock)
        self.history_dock.setWidget(self.history_list)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.history_dock)

        # 5. Status Bar
        self.statusBar().showMessage("Ready")
        self.cursor_status = QLabel("", self)
        self.statusBar().addPermanentWidget(self.cursor_status)

        # 6. Menus and Actions
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        self.open_action = QAction("&Open...", self)
        self.open_action.setShortcut("Ctrl+O")
        self.open_action.triggered.connect(self._on_open_action)
        file_menu.addAction(self.open_action)

        self.export_action = QAction("&Export Python...", self)
        self.export_action.setShortcut("Ctrl+E")
        self.export_action.triggered.connect(self._on_export_action)
        file_menu.addAction(self.export_action)

        file_menu.addSeparator()

        self.close_action = QAction("&Close", self)
        self.close_action.setShortcut("Ctrl+Q")
        self.close_action.triggered.connect(self.close)
        file_menu.addAction(self.close_action)

        operation_menu = menubar.addMenu("&Operations")
        self.crop_action = QAction("&Crop...", self)
        self.crop_action.triggered.connect(self._on_crop_action)
        operation_menu.addAction(self.crop_action)

        self.detrend_action = QAction("&Detrend...", self)
        self.detrend_action.triggered.connect(self._on_detrend_action)
        operation_menu.addAction(self.detrend_action)

        self.asd_action = QAction("&ASD...", self)
        self.asd_action.triggered.connect(self._on_asd_action)
        operation_menu.addAction(self.asd_action)

        self.spectrogram_action = QAction("&Spectrogram...", self)
        self.spectrogram_action.triggered.connect(self._on_spectrogram_action)
        operation_menu.addAction(self.spectrogram_action)

        self._operation_actions = (
            self.crop_action,
            self.detrend_action,
            self.asd_action,
            self.spectrogram_action,
        )
        self._init_signal_tools(file_menu, operation_menu)
        self._init_workspace(file_menu)
        self._init_trial_support_actions()
        self._update_command_state()

    @property
    def bridge(self) -> WorkerBridge:
        """Access underlying worker bridge."""
        return self._bridge

    @property
    def project(self) -> Project:
        """Access current project state."""
        return self._project

    def _on_cursor_moved(self, text: str) -> None:
        self.cursor_status.setText(text)

    def _bridge_state(self) -> BridgeState:
        """Read the bridge's public lifecycle state."""
        return self._bridge.state

    def _selected_object(self) -> DataObjectRef | None:
        if self._current_object_id is None:
            return None
        return next(
            (
                obj
                for obj in self._project.objects
                if obj.object_id == self._current_object_id
            ),
            None,
        )

    def _operation_is_available(self) -> bool:
        selected = self._selected_object()
        return (
            self._bridge_state() is BridgeState.IDLE
            and not self._command_reserved
            and not self._modal_active
            and self._io_capability_ready is True
            and selected is not None
            and self._is_resident(selected.object_id)
            and (self._current_member.kind if self._current_member else selected.kind)
            == "TimeSeries"
        )

    def _update_command_state(self) -> None:
        """Apply the bridge, project, and selection command policy in one place."""
        available = (
            self._bridge_state() is BridgeState.IDLE
            and not self._command_reserved
            and not self._modal_active
            and self._io_capability_ready is True
        )
        self.open_action.setEnabled(available)
        self.source_tree.setEnabled(available)
        self.setAcceptDrops(available)
        self.export_action.setEnabled(available and bool(self._project.objects))
        operations_enabled = self._operation_is_available()
        for action in self._operation_actions:
            action.setEnabled(operations_enabled)
        self._update_signal_state(available)
        self._update_workspace_actions()
        self._update_trial_support_action_state()

    def _on_bridge_state_changed(self, _state: str) -> None:
        """Invalidate a worker qualification when its owning bridge terminates."""
        if self._bridge_state() in {BridgeState.CLOSED, BridgeState.FAILED}:
            # A capability snapshot is bound to one worker process.  Do not
            # reuse it if the bridge later creates a replacement worker.
            self._io_capability_ready = False
            self._io_capability_snapshot = None
        self._update_command_state()

    def _show_error(self, code: str, message: str) -> None:
        self._error_active = True
        self._last_error_code = code
        self.statusBar().showMessage(f"Error: [{code}] {message}")

    def _show_status(self, message: str) -> None:
        if not self._error_active:
            self.statusBar().showMessage(message)

    def _reject_for_bridge_state(self) -> bool:
        if self._modal_active:
            self._show_error(
                "modal_active", "Finish the current dialog before starting a command"
            )
            return True
        state = self._bridge_state()
        if state is not BridgeState.IDLE:
            code = (
                "bridge_busy" if state is BridgeState.RUNNING else "bridge_unavailable"
            )
            self._show_error(
                code, f"Cannot start a command while bridge is {state.value}"
            )
            return True
        if self._command_reserved:
            self._show_error(
                "command_pending", "Wait for the current command result to be applied"
            )
            return True
        return False

    def _dispatch_command(
        self,
        kind: CommandKind,
        payload: dict[str, Any],
        *,
        pending_action: str,
        pending_params: dict[str, Any] | None = None,
    ) -> bool:
        """Dispatch one command without changing pending state on refusal."""
        if kind != "start" and self._io_capability_ready is not True:
            self._show_error("worker_unavailable", "Worker capabilities are not ready")
            self._update_command_state()
            return False
        if self._reject_for_bridge_state():
            return False
        if kind in WORKSPACE_GESTURES and self._workspace_status:
            payload = {**payload, "ui_state": capture_ui_state(self)}
            self._checkpoint_timer.stop()
            self._checkpoint_pending = False
        previous_pending = (
            self._pending_action,
            self._pending_params,
            self._pending_inspection,
            self._pending_command_id,
            self._command_reserved,
        )
        self._pending_action = pending_action
        self._pending_params = pending_params
        self._pending_command_id = None
        self._command_reserved = True
        self._update_command_state()
        try:
            command_id = self._bridge.send_command(kind, payload)
        except OperationError as exc:
            (
                self._pending_action,
                self._pending_params,
                self._pending_inspection,
                self._pending_command_id,
                self._command_reserved,
            ) = previous_pending
            self._show_error(exc.code, str(exc))
            self._update_command_state()
            return False
        self._pending_command_id = command_id
        self._error_active = False
        self.statusBar().showMessage("Working...")
        self._update_command_state()
        return True

    def _clear_pending_command(self) -> None:
        self._pending_action = None
        self._pending_params = None
        self._pending_inspection = None
        self._pending_command_id = None
        self._command_reserved = False

    def _clear_selected_object(self) -> None:
        self._current_object_id = None
        self._current_member = None
        populate_metadata_tree(self.metadata_tree, None)
        self.plot_canvas.clear()
        self.central_stack.setCurrentWidget(self.central_placeholder)
        self._update_command_state()

    @staticmethod
    def _find_object_item(
        parent: QTreeWidget | QTreeWidgetItem, object_id: str
    ) -> QTreeWidgetItem | None:
        count = (
            parent.topLevelItemCount()
            if isinstance(parent, QTreeWidget)
            else parent.childCount()
        )
        for index in range(count):
            item = (
                parent.topLevelItem(index)
                if isinstance(parent, QTreeWidget)
                else parent.child(index)
            )
            if item is None:
                continue
            if item.data(0, Qt.ItemDataRole.UserRole) == object_id and item.text(
                1
            ).startswith(("TimeSeries", "FrequencySeries", "Spectrogram")):
                return item
            nested = MainWindow._find_object_item(item, object_id)
            if nested is not None:
                return nested
        return None

    def _refresh_project_views(self, *, selected_object_id: str | None) -> None:
        with QSignalBlocker(self.source_tree):
            populate_source_tree(self.source_tree, self._project)
            self._restore_member_rows()
            selected_item = (
                self._find_object_item(self.source_tree, selected_object_id)
                if selected_object_id is not None
                else None
            )
            if selected_item is not None:
                selected_item = self._selected_member_row(selected_item)
                self.source_tree.setCurrentItem(selected_item)
            else:
                self.source_tree.clearSelection()
        populate_history_list(self.history_list, self._project)
        if selected_item is None:
            self._clear_selected_object()
            return
        selected = self._current_member or self._selected_object()
        populate_metadata_tree(self.metadata_tree, selected)
        self._update_command_state()

    def _select_object_and_request_preview(self, obj_ref: DataObjectRef) -> None:
        self._current_object_id = obj_ref.object_id
        self._current_member = None
        self._refresh_project_views(selected_object_id=obj_ref.object_id)
        self.plot_canvas.clear()
        self.central_stack.setCurrentWidget(self.central_placeholder)
        if obj_ref.kind not in {"TimeSeries", "FrequencySeries", "Spectrogram"}:
            return
        self._dispatch_command(
            "preview",
            {"object_id": obj_ref.object_id},
            pending_action="preview",
            pending_params={"object_id": obj_ref.object_id},
        )

    def _on_source_tree_selection(self) -> None:
        items = self.source_tree.selectedItems()
        if not items:
            self._clear_selected_object()
            return
        item = items[0]
        selected_id = item.data(0, Qt.ItemDataRole.UserRole)
        if selected_id and not self._is_resident(selected_id):
            self._current_object_id = selected_id
            member = item.data(0, MEMBER_ROLE)
            self._current_member = member if isinstance(member, MemberRef) else None
            populate_metadata_tree(
                self.metadata_tree, self._current_member or self._selected_object()
            )
            self.plot_canvas.clear()
            self.central_placeholder.setText(
                "Data not restored\nChoose File → Review / Restore Data"
            )
            self.central_stack.setCurrentWidget(self.central_placeholder)
            self._update_command_state()
            return
        if self._select_signal_item(item):
            return
        obj_id = item.data(0, Qt.ItemDataRole.UserRole)
        matching_obj = next(
            (o for o in self._project.objects if o.object_id == obj_id),
            None,
        )
        if matching_obj is not None:
            self._current_object_id = matching_obj.object_id
            populate_metadata_tree(self.metadata_tree, matching_obj)
            self.plot_canvas.clear()
            self.central_stack.setCurrentWidget(self.central_placeholder)
            self._update_command_state()
            self._dispatch_command(
                "preview",
                {"object_id": matching_obj.object_id},
                pending_action="preview",
                pending_params={"object_id": matching_obj.object_id},
            )
            return
        self._clear_selected_object()

    def open_file(self, uri: str) -> None:
        """Initiate shallow inspection and loading sequence for a file URI."""
        self._dispatch_command(
            "inspect",
            {"uri": uri},
            pending_action="open_inspect",
            pending_params={"uri": uri},
        )

    def apply_operation(
        self,
        op_name: str,
        params: dict[str, Any] | None = None,
        input_id: str | None = None,
    ) -> None:
        """Apply a curated scientific operation on the currently active object."""
        if self._reject_for_bridge_state():
            return
        if self._current_member is not None and input_id in {
            None,
            self._current_object_id,
        }:
            self._panel_dispatch(
                None,
                "apply_multi",
                {
                    "op_name": op_name,
                    "inputs": {"self": self._selected_handle()},
                    "params": params or {},
                },
            )
            return
        target_input = input_id or self._current_object_id
        target = next(
            (obj for obj in self._project.objects if obj.object_id == target_input),
            None,
        )
        if target is None or target.kind != "TimeSeries":
            self._show_error(
                "operation_unavailable", "No TimeSeries data object is selected"
            )
            return

        self._dispatch_command(
            "apply",
            {"op_name": op_name, "input_id": target_input, "params": params or {}},
            pending_action="apply_op",
            pending_params={"op_name": op_name},
        )

    def export_to_file(self, target_path: str) -> None:
        """Export current project execution graph to a Python script."""
        if self._reject_for_bridge_state():
            return
        if not self._project.objects:
            self._show_error("export_unavailable", "There is no data to export")
            return
        payload: dict[str, Any] = {"target_path": target_path}
        if self.include_writes_action.isChecked():
            payload["include_data_writes"] = True
        self._dispatch_command(
            "export",
            payload,
            pending_action="export",
            pending_params={"target_path": target_path},
        )

    def _on_open_action(self) -> None:
        self.show_open_data()

    def _show_operation_dialog(self, dialog_type: Any, op_name: str) -> None:
        if not self._operation_is_available():
            self.apply_operation(op_name)
            return
        input_id = self._current_object_id
        self._modal_active = True
        self._update_command_state()
        try:
            dialog = dialog_type(self)
            accepted = dialog.exec() == dialog.DialogCode.Accepted
            params = dict(dialog.params) if accepted else None
        finally:
            self._modal_active = False
            self._update_command_state()
        if accepted and params is not None:
            self.apply_operation(op_name, params, input_id=input_id)

    def _on_crop_action(self) -> None:
        self._show_operation_dialog(CropDialog, "timeseries.crop")

    def _on_detrend_action(self) -> None:
        self._show_operation_dialog(DetrendDialog, "timeseries.detrend")

    def _on_asd_action(self) -> None:
        self._show_operation_dialog(AsdDialog, "timeseries.asd")

    def _on_spectrogram_action(self) -> None:
        self._show_operation_dialog(SpectrogramDialog, "timeseries.spectrogram")

    def _on_export_action(self) -> None:
        if not self.export_action.isEnabled():
            self.export_to_file("")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Python Script", "export.py", "Python Scripts (*.py)"
        )
        if path:
            self.export_to_file(path)

    def _on_bridge_result(self, result: BridgeResult) -> None:
        """Handle results dispatched from WorkerBridge."""
        if (
            self._pending_command_id is not None
            and result.command_id != self._pending_command_id
        ):
            return
        if self._pending_action == "worker_capability_start":
            self._handle_worker_capability_start(result)
            return
        workspace_pending_action = self._pending_action
        if self._handle_workspace_result(result):
            if result.success and workspace_pending_action in {
                "open_project",
                "save_project",
            }:
                self._record_current_project_as_recent()
            return
        if self._handle_signal_result(result):
            return
        if self._pending_command_id is not None:
            self._pending_command_id = None
            self._command_reserved = False
        payload = result.payload if isinstance(result.payload, Mapping) else {}
        carried_project = payload.get("project")
        selected_before = self._current_object_id

        project_updated = False
        if isinstance(carried_project, Project):
            self._project = carried_project
            project_updated = True

        if not result.success:
            self._clear_pending_command()
            if project_updated:
                self._current_object_id = selected_before
                self._refresh_project_views(selected_object_id=selected_before)
            self._show_error(
                result.error_code or "operation_failed",
                result.error_message or "Command failed",
            )
            self._update_command_state()
            return

        if "inspection" in payload:
            insp = payload["inspection"]
            if self._pending_action == "open_inspect" and isinstance(
                insp, SourceInspection
            ):
                self._pending_action = None
                self._pending_params = None
                self._pending_inspection = insp
                if self._reject_for_bridge_state():
                    self._clear_pending_command()
                    self._update_command_state()
                    return
                self._modal_active = True
                self._update_command_state()
                try:
                    dialog = LoadConfirmationDialog(insp, self)
                    accepted = dialog.exec() == dialog.DialogCode.Accepted
                    can_load = dialog.can_load
                except Exception:
                    self._clear_pending_command()
                    raise
                finally:
                    self._modal_active = False
                    self._update_command_state()
                if accepted and can_load:
                    self._dispatch_command(
                        "load",
                        {"inspection": insp},
                        pending_action="open_load",
                        pending_params={"inspection": insp},
                    )
                elif not can_load:
                    self._clear_pending_command()
                    self._show_error(
                        "unsupported_source_format", "Source cannot be loaded"
                    )
                else:
                    self._clear_pending_command()
                    self._show_status("Open cancelled")
            else:
                self._clear_pending_command()
                self._show_error(
                    "invalid_response", "Unexpected source inspection response"
                )

        elif "object" in payload:
            obj_ref = payload["object"]
            self._clear_pending_command()
            if not isinstance(obj_ref, DataObjectRef):
                self._show_error("invalid_response", "Invalid data object response")
            else:
                self._select_object_and_request_preview(obj_ref)

        elif "preview_payload" in payload:
            self._clear_pending_command()
            preview_payload = payload["preview_payload"]
            if not isinstance(preview_payload, PreviewPayload):
                self._show_error("invalid_response", "Invalid preview payload")
                self._update_command_state()
                return
            if preview_payload.preview.ref.object_id != self._current_object_id:
                self.central_stack.setCurrentWidget(self.central_placeholder)
                self._update_command_state()
                return
            self.plot_canvas.set_preview(preview_payload.preview, preview_payload.spec)
            self.central_stack.setCurrentWidget(self.plot_canvas)
            self._show_status("Preview loaded")

        elif "exported" in payload:
            self._clear_pending_command()
            self._show_status("Export completed successfully")
        else:
            self._clear_pending_command()

        self._update_command_state()

    def _handle_worker_capability_start(self, result: BridgeResult) -> None:
        """Accept only a schema-validated worker snapshot before enabling I/O."""
        self._clear_pending_command()
        if not result.success:
            self._capability_start_continuation = None
            self._io_capability_ready = False
            self._show_error(
                result.error_code or "worker_unavailable",
                result.error_message or "Worker capability startup failed",
            )
            self._update_command_state()
            return
        payload = result.payload if isinstance(result.payload, Mapping) else {}
        try:
            from ..ops.io_capabilities import (
                EffectiveCapabilitySnapshot,
                load_capability_manifest,
                validate_effective_capability_snapshot,
            )

            snapshot = EffectiveCapabilitySnapshot.from_document(
                payload.get("io_capabilities")
            )
            static_policy = load_capability_manifest()
            validate_effective_capability_snapshot(snapshot, static_policy)
            if snapshot.mode == "invalid":
                raise ValueError("Worker capability policy is unavailable")
        except (TypeError, ValueError):
            self._capability_start_continuation = None
            self._io_capability_ready = False
            self._show_error(
                "invalid_response", "Worker did not return I/O capability status"
            )
            self._update_command_state()
            return
        self._io_capability_snapshot = snapshot.document()
        self._io_capability_ready = True
        self._continue_after_worker_capabilities()
        self._update_command_state()

    def _drop_path(self, mime_data: QMimeData) -> tuple[str | None, str | None]:
        """Validate one local file URL and return its decoded filesystem path."""
        if self._reject_for_bridge_state():
            return None, "bridge_state"
        urls = mime_data.urls() if mime_data.hasUrls() else []
        if len(urls) != 1:
            return None, "Drop exactly one file"
        url = urls[0]
        if not url.isLocalFile() or url.host() not in {"", "localhost"}:
            return None, "Drop a local file URL"
        normalized_url = QUrl(url)
        if normalized_url.host() == "localhost":
            normalized_url.setAuthority("")
        local_path = normalized_url.toLocalFile()
        if not local_path:
            return None, "Drop a local file URL"
        path = Path(local_path)
        if path.is_dir():
            return None, "Cannot open a directory"
        if not path.is_file():
            return None, "Dropped path is not a file"
        return str(path), None

    def _validate_drop_event(self, event: QDragEnterEvent | QDropEvent) -> str | None:
        path, error = self._drop_path(event.mimeData())
        if error is not None:
            event.ignore()
            if error != "bridge_state":
                self._show_error("invalid_drop", error)
            return None
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        return path

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept only one existing local file while commands are available."""
        self._validate_drop_event(event)

    def dropEvent(self, event: QDropEvent) -> None:
        """Route one validated local file through the ordinary open workflow."""
        path = self._validate_drop_event(event)
        if path is not None:
            self.open_file(path)

    def _on_bridge_safe_to_destroy(self) -> None:
        """Schedule exactly one ordinary close retry after QThread shutdown."""
        if self._after_busy:
            QTimer.singleShot(0, self._continue_after_busy)
        if not self._pending_close or self._close_retry_scheduled:
            return
        self._close_retry_scheduled = True
        QTimer.singleShot(0, self._retry_pending_close)

    def _retry_pending_close(self) -> None:
        self._close_retry_scheduled = False
        if self._pending_close:
            self.close()

    def close(self) -> bool:
        """Preserve programmatic close compatibility without weakening closeEvent."""
        if not self._workspace_close_authorized:
            if not self._request_document_change("exit", {}):
                return False
        if not self._pending_close and self._bridge.worker_thread.isRunning():
            if self._workspace_status:
                self._bridge.close(clean_workspace=True)
            else:
                self._bridge.close()
        return super().close()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Retain bridge ownership until its worker thread is safe to destroy."""
        if not self._workspace_close_authorized and not self._request_document_change(
            "exit", {}
        ):
            event.ignore()
            return
        if self._bridge.worker_thread.isRunning():
            self._pending_close = True
            if self._workspace_status:
                self._bridge.close(clean_workspace=True)
            else:
                self._bridge.close()
            event.ignore()
            return
        self._pending_close = False
        event.accept()
