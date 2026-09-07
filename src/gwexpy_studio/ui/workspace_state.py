"""JSON-only projections of current Qt layout, selection, and unapproved drafts."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QByteArray, QSignalBlocker, Qt
from PySide6.QtWidgets import QTreeWidgetItemIterator

from ..domain.project_v2 import object_from_dict, record_dict
from ..errors import ProjectFormatError
from .io_panel import DataIOPanel
from .parameter_fields import OP_FIELDS
from .parameter_panel import ParameterPanel


def _valid_draft(name: str, draft: Any) -> bool:
    if not isinstance(draft, Mapping):
        return False
    if name == "parameter_panel":
        operation = draft.get("operation", "arithmetic")
        saved = draft.get("saved", {})
        if (
            not isinstance(operation, str)
            or operation not in OP_FIELDS
            or not isinstance(saved, Mapping)
        ):
            return False
        for op, fields in saved.items():
            if op not in OP_FIELDS or not isinstance(fields, Mapping):
                return False
            for field_name, _, kind, _, _ in OP_FIELDS[op]:
                if field_name in fields and not isinstance(
                    fields[field_name], bool if kind == "bool" else str
                ):
                    return False
        return True
    for key in ("datatype", "format", "paths", "args", "kwargs", "combine"):
        if key in draft and not isinstance(draft[key], str):
            return False
    return all(
        key not in draft
        or (type(draft[key]) is int and 0 <= draft[key] <= 2_147_483_647)
        for key in ("max_bytes", "max_entries")
    )


def tree_items(window: Any) -> list[Any]:
    """Return current tree rows in visible hierarchy order."""
    iterator = QTreeWidgetItemIterator(window.source_tree)
    result = []
    while iterator.value():
        result.append(iterator.value())
        iterator += 1
    return result


def capture_ui_state(window: Any) -> dict[str, Any]:
    """Capture the current layout and editable data without authorization state."""
    drafts = {}
    for name in ("parameter_panel", "open_data_panel", "export_data_panel"):
        panel = getattr(window, name, None)
        if panel is not None:
            drafts[name] = panel.draft_state()
    return {
        "layout": bytes(window.saveState().toBase64()).decode("ascii"),
        "geometry": bytes(window.saveGeometry().toBase64()).decode("ascii"),
        "selection": {
            "object_id": window._current_object_id,
            "member": record_dict(window._current_member)
            if window._current_member is not None
            else None,
            "selector": dict(window._current_member.selector)
            if window._current_member is not None
            else None,
        },
        "expanded_sources": [
            str(item.data(0, Qt.ItemDataRole.UserRole))
            for item in tree_items(window)
            if item.isExpanded()
        ],
        "panel_drafts": drafts,
    }


def restore_ui_state(window: Any, state: dict[str, Any]) -> None:
    """Restore data-only controls; inspection/authorization state is never loaded."""
    window._restoring_ui = True
    invalid = []
    try:
        for key, restore in (
            ("layout", window.restoreState),
            ("geometry", window.restoreGeometry),
        ):
            raw = state.get(key)
            if raw is not None:
                try:
                    if not isinstance(raw, str) or not restore(
                        QByteArray(base64.b64decode(raw, validate=True))
                    ):
                        invalid.append(key)
                except (ValueError, binascii.Error):
                    invalid.append(key)
        selection = state.get("selection", {})
        if not isinstance(selection, Mapping):
            invalid.append("selection")
            selection = {}
        window._current_object_id = selection.get("object_id")
        window._current_member = None
        selected = window._selected_object()
        if selected is not None and selection.get("selector") is not None:
            window._current_member = next(
                (
                    member
                    for member in selected.members
                    if dict(member.selector) == selection["selector"]
                ),
                None,
            )
            if window._current_member is None and isinstance(
                selection.get("member"), Mapping
            ):
                try:
                    snapshot = object_from_dict(
                        {**record_dict(selected), "members": [selection["member"]]}
                    ).members[0]
                    if dict(snapshot.selector) == selection["selector"]:
                        window._current_member = snapshot
                except ProjectFormatError:
                    invalid.append("selection.member")
        window._refresh_project_views(selected_object_id=window._current_object_id)
        expanded_raw = state.get("expanded_sources", [])
        if not isinstance(expanded_raw, list) or not all(
            isinstance(item, str) for item in expanded_raw
        ):
            invalid.append("expanded_sources")
            expanded_raw = []
        expanded = set(expanded_raw)
        with QSignalBlocker(window.source_tree):
            for item in tree_items(window):
                item.setExpanded(
                    str(item.data(0, Qt.ItemDataRole.UserRole)) in expanded
                )
        drafts = state.get("panel_drafts", {})
        if not isinstance(drafts, Mapping):
            invalid.append("panel_drafts")
            drafts = {}
        for name, draft in drafts.items():
            if name not in ("parameter_panel", "open_data_panel", "export_data_panel"):
                continue
            if not _valid_draft(name, draft):
                invalid.append(name)
                continue
            panel = getattr(window, name, None)
            if panel is None:
                panel = (
                    ParameterPanel(window)
                    if name == "parameter_panel"
                    else DataIOPanel(
                        direction="read" if name == "open_data_panel" else "write",
                        parent=window,
                    )
                )
                setattr(window, name, panel)
                window._wire_workspace_panel(name, panel)
            panel.restore_draft(draft)
    finally:
        window._restoring_ui = False
    if invalid:
        window._show_error(
            "invalid_ui_state", "Ignored invalid UI state: " + ", ".join(invalid)
        )
