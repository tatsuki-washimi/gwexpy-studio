"""Main-window coordination for modeless signal tools and native data I/O."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow, QMenu, QMessageBox, QTreeWidgetItem

from ..application.preview import PreviewPayload
from ..domain.model import DataObjectRef, MemberRef, PlotSpec
from ..domain.project import Project
from .bridge import BridgeState
from .io_panel import DataIOPanel
from .models import (
    LEAF_KINDS,
    MEMBER_ROLE,
    MORE_ROLE,
    populate_member_items,
    populate_metadata_tree,
)
from .parameter_fields import FILTER_OPERATIONS, OP_FIELDS
from .parameter_panel import ParameterPanel

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtWidgets import QLabel, QStackedWidget, QTreeWidget

    from .plot_canvas import PlotCanvas


class SignalTools(QMainWindow):
    """Coordinate detached panel requests through the existing serial bridge."""

    if TYPE_CHECKING:
        export_action: QAction
        close_action: QAction
        source_tree: QTreeWidget
        metadata_tree: QTreeWidget
        central_stack: QStackedWidget
        central_placeholder: QLabel
        plot_canvas: PlotCanvas
        _project: Project
        _current_object_id: str | None
        _pending_action: str | None
        _pending_params: dict[str, Any] | None
        _modal_active: bool
        _command_reserved: bool
        _queued_plot_spec: PlotSpec | None
        view_history: Any
        _bridge: Any
        _after_view_save: Any
        _drain_view_changes: Callable[[], None]
        _selected_object: Callable[[], DataObjectRef | None]
        _reject_for_bridge_state: Callable[[], bool]
        _show_error: Callable[[str, str], None]
        _show_status: Callable[[str], None]
        _dispatch_command: Callable[..., bool]
        _update_command_state: Callable[[], None]
        _find_object_item: Callable[..., QTreeWidgetItem | None]
        _clear_pending_command: Callable[[], None]
        _refresh_project_views: Callable[..., None]
        _select_object_and_request_preview: Callable[[DataObjectRef], None]
        _queue_ui_checkpoint: Callable[..., None]
        _is_resident: Callable[[str], bool]
        _is_active: Callable[[str], bool]

    def _init_signal_tools(self, file_menu: QMenu, operation_menu: QMenu) -> None:
        self.parameter_panel: ParameterPanel | None = None
        self.open_data_panel: DataIOPanel | None = None
        self.export_data_panel: DataIOPanel | None = None
        self._pending_panel: ParameterPanel | DataIOPanel | None = None
        self._current_member: MemberRef | None = None
        self._member_cache: dict[str, tuple[MemberRef, ...]] = {}
        self._member_loading: set[str] = set()
        self._member_has_more: set[str] = set()
        self._write_handle: dict[str, Any] | None = None
        self.export_data_action = QAction("Export &Data…", self)
        self.export_data_action.triggered.connect(self._show_export_data)
        file_menu.insertAction(self.export_action, self.export_data_action)
        self.include_writes_action = QAction(
            "Include data writes in Python export", self
        )
        self.include_writes_action.setCheckable(True)
        self.include_writes_action.setChecked(False)
        file_menu.insertAction(self.close_action, self.include_writes_action)
        operation_menu.addSeparator()
        self.signal_actions: dict[str, QAction] = {}
        filters = operation_menu.addMenu("Filters")
        for operation in OP_FIELDS:
            title = operation.removeprefix("timeseries.").replace("_", " ").title()
            action = QAction(title + "…", self)
            action.triggered.connect(
                lambda _checked=False, op=operation: self.show_parameter_panel(op)
            )
            (filters if operation in FILTER_OPERATIONS else operation_menu).addAction(
                action
            )
            self.signal_actions[operation] = action
        self.filter_response_action = QAction("Show Applied Filter Response…", self)
        self.filter_response_action.triggered.connect(self._show_applied_response)
        operation_menu.addAction(self.filter_response_action)
        self.source_tree.itemExpanded.connect(self._on_member_expand)
        self.plot_canvas.spec_changed.connect(self._save_plot_spec)

    def _update_signal_state(self, available: bool) -> None:
        if not hasattr(self, "signal_actions"):
            return
        selected = self._selected_object()
        selected_kind = (
            self._current_member.kind
            if self._current_member
            else (selected.kind if selected else "")
        )
        self.export_data_action.setEnabled(
            available and selected is not None and self._is_resident(selected.object_id)
        )
        self.include_writes_action.setEnabled(available)
        self.plot_canvas._controls.setEnabled(available)
        self.plot_canvas.toolbar.setEnabled(available)
        for operation, action in self.signal_actions.items():
            supported = (
                selected is not None
                and (
                    operation == "arithmetic" or selected_kind.startswith("TimeSeries")
                )
                and self._is_resident(selected.object_id)
            )
            action.setEnabled(available and supported)
        self.filter_response_action.setEnabled(
            available and self._filter_operation() is not None
        )
        for panel in (
            self.parameter_panel,
            self.open_data_panel,
            self.export_data_panel,
        ):
            if panel is not None:
                panel.set_busy(not available)

    def _object_handles(self) -> list[tuple[str, dict[str, Any]]]:
        handles: list[tuple[str, dict[str, Any]]] = []
        for obj in self._project.objects:
            if not self._is_active(obj.object_id):
                continue
            label = f"{obj.name or obj.object_id} ({obj.kind})"
            handles.append((label, {"object_id": obj.object_id}))
            for member in self._member_cache.get(obj.object_id, obj.members):
                handles.append(
                    (
                        f"{label} / {member.label}",
                        {"object_id": obj.object_id, "selector": dict(member.selector)},
                    )
                )
        selected = self._selected_handle()
        if (
            self._current_member is not None
            and selected is not None
            and self._is_active(selected["object_id"])
            and not any(handle == selected for _, handle in handles)
        ):
            handles.append((self._current_member.label, selected))
        return handles

    def _selected_handle(self) -> dict[str, Any] | None:
        if self._current_object_id is None:
            return None
        handle: dict[str, Any] = {"object_id": self._current_object_id}
        if self._current_member is not None:
            handle["selector"] = dict(self._current_member.selector)
        return handle

    def show_parameter_panel(self, operation: str) -> None:
        """Show the common typed parameter form for a scientific operation."""
        if self._reject_for_bridge_state():
            return
        if self.parameter_panel is None:
            self.parameter_panel = ParameterPanel(self)
            self.parameter_panel.draft_changed.connect(self._queue_ui_checkpoint)
            self.parameter_panel.apply_requested.connect(
                lambda payload: self._panel_dispatch(
                    self.parameter_panel, "apply_multi", payload
                )
            )
            self.parameter_panel.preview_requested.connect(
                lambda payload: self._panel_dispatch(
                    self.parameter_panel, "filter_preview", payload
                )
            )
        panel = self.parameter_panel
        panel.set_operation(operation)
        panel.set_objects(self._object_handles())
        index = panel.self_combo.findData(self._selected_handle())
        if index >= 0:
            panel.self_combo.setCurrentIndex(index)
        panel.show()
        panel.raise_()
        panel.activateWindow()

    def show_open_data(self) -> None:
        """Open the native class/format/arguments configuration panel."""
        if self._reject_for_bridge_state():
            return
        if self.open_data_panel is None:
            self.open_data_panel = DataIOPanel(direction="read", parent=self)
            self.open_data_panel.draft_changed.connect(self._queue_ui_checkpoint)
            panel = self.open_data_panel
            panel.catalog_requested.connect(
                lambda payload: self._panel_dispatch(panel, "catalog_io", payload)
            )
            panel.inspect_requested.connect(
                lambda payload: self._panel_dispatch(panel, "inspect_io", payload)
            )
            panel.read_requested.connect(
                lambda payload: self._panel_dispatch(panel, "read_io", payload)
            )
        self.open_data_panel.show()
        self.open_data_panel.raise_()
        self._panel_dispatch(
            self.open_data_panel,
            "catalog_io",
            {
                "datatype": self.open_data_panel.datatype_combo.currentText(),
                "direction": "read",
            },
        )

    def _show_export_data(self) -> None:
        if self._reject_for_bridge_state():
            return
        handle = self._selected_handle()
        selected = self._current_member or self._selected_object()
        if handle is None or selected is None:
            self._show_error("export_unavailable", "Select data to export")
            return
        self._write_handle = handle
        if self.export_data_panel is None:
            self.export_data_panel = DataIOPanel(direction="write", parent=self)
            self.export_data_panel.draft_changed.connect(self._queue_ui_checkpoint)
            panel = self.export_data_panel
            panel.catalog_requested.connect(
                lambda payload: self._panel_dispatch(panel, "catalog_io", payload)
            )
            panel.write_requested.connect(self._write_selected_data)
        panel = self.export_data_panel
        with QSignalBlocker(panel.datatype_combo):
            panel.datatype_combo.setCurrentText(selected.kind)
        panel.show()
        panel.raise_()
        self._panel_dispatch(
            panel, "catalog_io", {"datatype": selected.kind, "direction": "write"}
        )

    def _write_selected_data(self, request: dict[str, Any]) -> None:
        paths = [
            Path(path).expanduser().absolute() for path in request.get("paths", ())
        ]
        request = {**request, "paths": [str(path) for path in paths]}
        if any(path.exists() or path.is_symlink() for path in paths):
            self._modal_active = True
            self._update_command_state()
            try:
                answer = QMessageBox.question(
                    self,
                    "Overwrite data file?",
                    "Replace the existing data at:\n" + "\n".join(request["paths"]),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
            finally:
                self._modal_active = False
                self._update_command_state()
            if answer != QMessageBox.StandardButton.Yes:
                return
            request = {**request, "overwrite_confirmed": True}
        if self._write_handle is not None:
            self._panel_dispatch(
                self.export_data_panel,
                "write_data",
                {**self._write_handle, "request": request},
            )

    def _panel_dispatch(self, panel: Any, kind: str, payload: dict[str, Any]) -> None:
        if self._dispatch_command(
            kind, payload, pending_action=f"signal_{kind}", pending_params=payload
        ):
            self._pending_panel = panel
        elif panel is not None:
            panel.show_error("command_unavailable", self.statusBar().currentMessage())

    def _save_plot_spec(self, spec: PlotSpec) -> None:
        if (
            self._command_reserved
            or self._bridge.state is not BridgeState.IDLE
            or self._modal_active
        ):
            self._queued_plot_spec = spec
            return
        self._dispatch_command(
            "set_plot", {"spec": spec}, pending_action="signal_set_plot"
        )

    def _on_member_expand(self, item: QTreeWidgetItem) -> None:
        if not self._is_resident(item.data(0, Qt.ItemDataRole.UserRole)):
            return
        object_id = item.data(0, Qt.ItemDataRole.UserRole)
        if item.text(1) in LEAF_KINDS or item.data(0, MEMBER_ROLE) is not None:
            return
        obj = next(
            (obj for obj in self._project.objects if obj.object_id == object_id), None
        )
        if (
            obj is None
            or object_id in self._member_cache
            or object_id in self._member_loading
        ):
            return
        if self._dispatch_command(
            "list_members",
            {"object_id": object_id},
            pending_action="signal_list_members",
        ):
            self._member_loading.add(object_id)

    def _select_signal_item(self, item: QTreeWidgetItem) -> bool:
        if item.data(0, MORE_ROLE):
            object_id = item.data(0, Qt.ItemDataRole.UserRole)
            request = {
                "object_id": object_id,
                "offset": len(self._member_cache.get(object_id, ())),
                "limit": 100,
            }
            self._dispatch_command(
                "list_members",
                request,
                pending_action="signal_list_members",
                pending_params=request,
            )
            return True
        member = item.data(0, MEMBER_ROLE)
        object_id = item.data(0, Qt.ItemDataRole.UserRole)
        obj = next(
            (obj for obj in self._project.objects if obj.object_id == object_id), None
        )
        self._current_member = member if isinstance(member, MemberRef) else None
        if obj is None or (member is None and obj.kind in LEAF_KINDS):
            return False
        self._current_object_id = object_id
        populate_metadata_tree(self.metadata_tree, member or obj)
        self.plot_canvas.clear()
        self.central_stack.setCurrentWidget(self.central_placeholder)
        self._update_command_state()
        if isinstance(member, MemberRef):
            self._dispatch_command(
                "member_preview",
                {"object_id": object_id, "selector": dict(member.selector)},
                pending_action="signal_member_preview",
            )
        return True

    def _restore_member_rows(self) -> None:
        for object_id, members in self._member_cache.items():
            item = self._find_object_item(self.source_tree, object_id)
            if item is not None:
                self._populate_member_page(item, object_id, members)

    def _populate_member_page(
        self, item: QTreeWidgetItem, object_id: str, members: tuple[MemberRef, ...]
    ) -> None:
        with QSignalBlocker(self.source_tree):
            populate_member_items(item, object_id, members)
            if object_id in self._member_has_more:
                more = QTreeWidgetItem(item, ["Load more members…", "More"])
                more.setData(0, Qt.ItemDataRole.UserRole, object_id)
                more.setData(0, MORE_ROLE, True)

    def _selected_member_row(self, parent: QTreeWidgetItem) -> QTreeWidgetItem:
        if self._current_member is None:
            return parent
        for index in range(parent.childCount()):
            child = parent.child(index)
            if child is None:
                continue
            member = child.data(0, MEMBER_ROLE)
            if (
                isinstance(member, MemberRef)
                and member.selector == self._current_member.selector
            ):
                self._current_member = member
                parent.setExpanded(True)
                return child
        child = QTreeWidgetItem(
            parent, [self._current_member.label, self._current_member.kind]
        )
        child.setData(0, Qt.ItemDataRole.UserRole, self._current_object_id)
        child.setData(0, MEMBER_ROLE, self._current_member)
        parent.setExpanded(True)
        return child

    def _handle_signal_result(self, result: Any) -> bool:
        action = self._pending_action or ""
        if not action.startswith("signal_"):
            return False
        panel = self._pending_panel
        self._pending_panel = None
        payload = result.payload if isinstance(result.payload, Mapping) else {}
        pending = self._pending_params or {}
        self._clear_pending_command()
        self._member_loading.clear()
        if isinstance(payload.get("project"), Project):
            self._project = payload["project"]
            self._refresh_project_views(selected_object_id=self._current_object_id)
        if panel is not None:
            panel.set_busy(False)
        if result.success:
            messages = {
                "signal_catalog_io": "Formats loaded",
                "signal_inspect_io": "Inspection ready for confirmation",
                "signal_filter_preview": "Filter response loaded",
                "signal_list_members": "Members loaded",
                "signal_member_preview": "Preview loaded",
                "signal_apply_multi": "Operation completed",
                "signal_read_io": "Data loaded",
                "signal_write_data": "Data export completed",
                "signal_set_plot": "Plot settings saved",
            }
            self._show_status(messages.get(action, "Command completed"))
        if not result.success:
            code, message = (
                result.error_code or "operation_failed",
                result.error_message or "Command failed",
            )
            if panel is not None:
                panel.show_error(code, message)
            self._show_error(code, message)
        elif action == "signal_catalog_io" and isinstance(panel, DataIOPanel):
            catalog = payload.get("io_catalog")
            if (
                isinstance(catalog, Mapping)
                and self._catalog_matches_worker_capabilities(panel, catalog)
            ):
                panel.set_catalog(dict(catalog))
            else:
                panel.set_unavailable_catalog()
                panel.show_error(
                    "io_capability_unavailable",
                    "Worker I/O capability status is unavailable.",
                )
        elif action == "signal_inspect_io" and isinstance(panel, DataIOPanel):
            panel.show_inspection(payload["io_inspection"])
        elif action == "signal_filter_preview" and isinstance(panel, ParameterPanel):
            responses = payload.get("filter_previews") or (payload["filter_preview"],)
            panel.show_responses(
                responses, payload.get("generation", pending.get("generation", -1))
            )
        elif action == "signal_list_members":
            object_id = payload["object_id"]
            members = tuple(payload["members"])
            if pending.get("offset", 0):
                self._member_cache[object_id] += members
            else:
                self._member_cache[object_id] = members
            if len(members) == pending.get("limit", 100):
                self._member_has_more.add(object_id)
            else:
                self._member_has_more.discard(object_id)
            item = self._find_object_item(self.source_tree, object_id)
            if item is not None:
                self._populate_member_page(
                    item, object_id, self._member_cache[object_id]
                )
                with QSignalBlocker(self.source_tree):
                    self.source_tree.setCurrentItem(self._selected_member_row(item))
        elif action == "signal_member_preview":
            preview = payload.get("preview_payload")
            if isinstance(preview, PreviewPayload):
                if self._current_member is not None:
                    ref = preview.preview.ref
                    self._current_member = replace(
                        self._current_member,
                        kind=ref.kind,
                        shape=ref.shape,
                        dtype=ref.dtype,
                        unit=ref.unit,
                        name=ref.name,
                        channel=ref.channel,
                        axes=ref.axes,
                        metadata=ref.metadata,
                    )
                    populate_metadata_tree(self.metadata_tree, self._current_member)
                self.plot_canvas.set_preview(preview.preview, preview.spec)
                self.central_stack.setCurrentWidget(self.plot_canvas)
        elif action in {"signal_apply_multi", "signal_read_io"}:
            objects = payload.get("objects", ())
            obj = payload.get("object") or (objects[-1] if objects else None)
            if isinstance(obj, DataObjectRef):
                self._current_member = None
                self._select_object_and_request_preview(obj)
                if self.parameter_panel is not None:
                    self.parameter_panel.set_objects(self._object_handles())
        if action == "signal_set_plot":
            if not result.success:
                self._after_view_save = None
            self.view_history.restore_pending_preview(result.success)
        self._update_command_state()
        self._drain_view_changes()
        return True

    def _catalog_matches_worker_capabilities(
        self, panel: DataIOPanel, catalog: object
    ) -> bool:
        """Accept a catalog only when it belongs to this worker bootstrap.

        Source mode deliberately retains the old unannotated registry catalog.
        A frozen artifact instead requires the static policy and runtime
        snapshot digests to match the already validated startup handshake.
        """
        if not isinstance(catalog, Mapping):
            return False
        if (
            catalog.get("datatype") != panel.datatype_combo.currentText()
            or catalog.get("direction") != panel.direction
        ):
            return False
        snapshot = getattr(self, "_io_capability_snapshot", None)
        if not isinstance(snapshot, Mapping):
            return False
        mode = snapshot.get("mode")
        if mode == "developer":
            return not any(
                name in catalog
                for name in (
                    "capability_mode",
                    "capability_digest",
                    "capability_effective_digest",
                )
            )
        if mode == "frozen":
            return (
                catalog.get("capability_mode") == "frozen"
                and catalog.get("capability_digest") == snapshot.get("policy_digest")
                and catalog.get("capability_effective_digest") == snapshot.get("digest")
            )
        return False

    def _filter_operation(self) -> Any:
        obj = self._selected_object()
        if obj is None:
            return None
        return next(
            (
                op
                for op in self._project.graph.operations
                if op.op_id == obj.produced_by and op.operation_id in FILTER_OPERATIONS
            ),
            None,
        )

    def _show_applied_response(self) -> None:
        operation = self._filter_operation()
        if operation is None or self._current_object_id is None:
            return
        selected_member = self._current_member
        self.show_parameter_panel(operation.operation_id)
        panel = self.parameter_panel
        if panel is None:
            return
        handle: dict[str, Any] = {"object_id": operation.inputs["self"]}
        if selected_member is not None:
            handle["selector"] = dict(selected_member.selector)
        index = panel.self_combo.findData(handle)
        if index < 0:
            panel.self_combo.addItem("Recorded filter input", handle)
            index = panel.self_combo.count() - 1
        panel.self_combo.setCurrentIndex(index)
        for name, value in operation.params.items():
            if name not in panel.fields:
                continue
            if isinstance(value, dict) and "value" in value:
                value = f"{value['value']} {value.get('unit', '')}".strip()
            elif isinstance(value, list):
                value = json.dumps(value)
            panel._restore_field(
                panel.fields[name], value if isinstance(value, bool) else str(value)
            )
        panel.preview_recorded(
            self._current_object_id,
            dict(selected_member.selector) if selected_member else None,
        )
