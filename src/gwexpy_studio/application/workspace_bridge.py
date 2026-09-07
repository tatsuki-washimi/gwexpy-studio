"""Closed workspace command interface and detached GUI result snapshots."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from ..errors import OperationError
from .workspace_controller import WorkspaceController
from .workspace_document import detached


def workspace_snapshot(controller: WorkspaceController) -> dict[str, Any]:
    """Detach the canonical project and its availability status for Qt."""
    return {
        "project": detached(controller.project),
        "workspace_status": controller.workspace_status(),
    }


def execute_workspace_command(
    controller: WorkspaceController,
    kind: str,
    payload: Mapping[str, Any],
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Dispatch an explicitly supported document command."""
    result: dict[str, Any] = {}
    if kind in {"new_project", "close_project"}:
        controller.new_workspace()
    elif kind == "open_project":
        controller.open_workspace(payload["path"])
    elif kind == "save_project":
        controller.save_workspace(payload.get("path"), ui_state=payload.get("ui_state"))
    elif kind == "set_ui_state":
        controller.set_ui_state(payload["ui_state"])
    elif kind == "undo_analysis":
        controller.undo_analysis()
    elif kind == "redo_analysis":
        controller.redo_analysis()
    elif kind == "review_restore":
        result["review"] = controller.review_restore(
            redo=payload.get("redo", False),
            progress=progress,
            cancel_event=cancel_event,
            deadline_monotonic=deadline_monotonic,
        )
    elif kind == "restore_project":
        controller.restore_workspace(
            payload["review"],
            confirmed=payload.get("confirmed") is True,
            progress=progress,
            cancel_event=cancel_event,
            deadline_monotonic=deadline_monotonic,
        )
    elif kind == "list_recoveries":
        result["recoveries"] = controller.list_recoveries()
    elif kind == "recover_project":
        controller.recover_workspace(payload["run_id"])
    elif kind == "discard_recovery":
        controller.discard_recovery(payload["run_id"])
    else:
        raise OperationError(
            f"Unknown workspace command: {kind}", code="invalid_command"
        )
    return {**result, **workspace_snapshot(controller)}
