"""Session-only undo for visual declarations and dock placement."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QByteArray, QEvent, QObject, QTimer
from PySide6.QtGui import QUndoCommand, QUndoStack


class _ViewChange(QUndoCommand):
    def __init__(self, label: str, before: Any, after: Any, apply: Any) -> None:
        super().__init__(label)
        self.before, self.after, self.apply = before, after, apply
        self.initial = True

    def undo(self) -> None:
        self.apply(self.before)

    def redo(self) -> None:
        if self.initial:
            self.initial = False
        else:
            self.apply(self.after)


class WorkspaceViewHistory(QObject):
    """Track views without issuing scientific undo or serializing the stack."""

    def __init__(self, window: Any, menu: Any) -> None:
        """Connect the window to its separate visual undo stack."""
        super().__init__(window)
        self.window = window
        window.installEventFilter(self)
        self.stack = QUndoStack(self)
        self.applying = False
        self.data_epoch = 0
        self.pending_preview: dict[str, Any] | None = None
        self.layout = b""
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._record_layout)
        menu.addSeparator()
        self.undo_action = self.stack.createUndoAction(window, "Undo View")
        self.redo_action = self.stack.createRedoAction(window, "Redo View")
        self.undo_action.setShortcut("Ctrl+Alt+Z")
        self.redo_action.setShortcut("Ctrl+Alt+Shift+Z")
        menu.addActions([self.undo_action, self.redo_action])
        self.stack.canUndoChanged.connect(window._update_workspace_actions)
        self.stack.canRedoChanged.connect(window._update_workspace_actions)
        window.plot_canvas.view_changed.connect(self._record_plot)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Checkpoint window geometry after user movement or resizing."""
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            self.window._queue_ui_checkpoint()
        return False

    def _record_plot(self, before: Any, after: Any) -> None:
        if not self.applying:
            self.stack.push(
                _ViewChange(
                    "Plot settings",
                    (self.window.plot_canvas._preview, before, self.data_epoch),
                    (self.window.plot_canvas._preview, after, self.data_epoch),
                    self._apply_plot,
                )
            )

    def _apply_plot(self, entry: Any) -> None:
        recorded_preview, spec, epoch = entry
        preview = recorded_preview
        current = self.window.plot_canvas._preview
        if (
            current is not None
            and recorded_preview is not None
            and (
                current.ref.object_id == recorded_preview.ref.object_id
                and current.ref.metadata.get("member_selector")
                == recorded_preview.ref.metadata.get("member_selector")
            )
        ):
            preview = current
        elif epoch != self.data_epoch:
            preview = None
        self.applying = True
        try:
            if recorded_preview is not None and self.window._is_active(
                recorded_preview.ref.object_id
            ):
                object_id = recorded_preview.ref.object_id
                self.window._current_object_id = object_id
                self.window._current_member = None
                selector = recorded_preview.ref.metadata.get("member_selector")
                parent = self.window._selected_object()
                if selector is not None and parent is not None:
                    self.window._current_member = next(
                        (
                            member
                            for member in parent.members
                            if dict(member.selector) == selector
                        ),
                        None,
                    )
                self.window._refresh_project_views(selected_object_id=object_id)
                if preview is not None and self.window._is_resident(object_id):
                    self.window.plot_canvas.set_preview(preview, spec)
                    self.window.central_stack.setCurrentWidget(self.window.plot_canvas)
                else:
                    self.window.plot_canvas.clear()
                    self.window.central_stack.setCurrentWidget(
                        self.window.central_placeholder
                    )
                    if self.window._is_resident(object_id):
                        self.pending_preview = {"object_id": object_id}
                        if selector is not None:
                            self.pending_preview["selector"] = dict(selector)
            self.window._save_plot_spec(spec)
        finally:
            self.applying = False

    def invalidate_data(self) -> None:
        """Discard trust in array snapshots when a scientific worker is replaced."""
        self.data_epoch += 1
        self.pending_preview = None

    def restore_pending_preview(self, succeeded: bool) -> None:
        """Fetch current data only after the replayed PlotSpec has been saved."""
        request, self.pending_preview = self.pending_preview, None
        if succeeded and request:
            member = "selector" in request
            self.window._dispatch_command(
                "member_preview" if member else "preview",
                request,
                pending_action="signal_member_preview" if member else "preview",
            )

    def capture_layout(self) -> None:
        """Use the present dock arrangement as the next undo baseline."""
        self.layout = bytes(self.window.saveState())

    def layout_changed(self, *_args: Any) -> None:
        """Combine the signals belonging to one dock placement gesture."""
        if not self.applying and not self.window._restoring_ui:
            self.timer.start()

    def _record_layout(self) -> None:
        current = bytes(self.window.saveState())
        if current != self.layout:
            self.stack.push(
                _ViewChange("Dock placement", self.layout, current, self._apply_layout)
            )
            self.layout = current
            self.window._queue_ui_checkpoint()

    def _apply_layout(self, state: bytes) -> None:
        self.applying = True
        try:
            self.window.restoreState(QByteArray(state))
            self.layout = state
            self.window._queue_ui_checkpoint()
        finally:
            self.applying = False
