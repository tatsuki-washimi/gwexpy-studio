"""Detached workspace failures with the injected legacy-controller contract."""

from __future__ import annotations

from typing import Any

from .signal_bridge import SIGNAL_MUTATIONS


def operation_success_payload(
    controller: Any, kind: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Attach an isolated document, retaining the old injected-controller seam."""
    if hasattr(controller, "workspace_status"):
        from ..application.workspace_bridge import workspace_snapshot

        return {**payload, **workspace_snapshot(controller)}
    if kind in {"start", "load", "apply", "set_plot"} | SIGNAL_MUTATIONS:
        return {**payload, "project": controller.project}
    return payload


def operation_failure_payload(
    controller: Any, kind: str, *, operation_dispatched: bool, timed_out: bool
) -> dict[str, Any] | None:
    """Retain a committed workspace even when its data worker has failed."""
    if controller is not None and hasattr(controller, "workspace_status"):
        from ..application.workspace_bridge import workspace_snapshot

        return workspace_snapshot(controller)
    if (
        timed_out
        or not operation_dispatched
        or controller is None
        or kind not in {"load", "apply"} | SIGNAL_MUTATIONS
    ):
        return None
    return {"project": controller.project}
