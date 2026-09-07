"""Detached command contracts for native I/O and signal-tool controller calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SIGNAL_REQUIRED: dict[str, frozenset[str]] = {
    "catalog_io": frozenset({"datatype", "direction"}),
    "inspect_io": frozenset({"request"}),
    "read_io": frozenset({"inspection"}),
    "write_data": frozenset({"object_id", "request"}),
    "list_members": frozenset({"object_id"}),
    "member_preview": frozenset({"object_id", "selector"}),
    "apply_multi": frozenset({"op_name", "inputs", "params"}),
    "filter_preview": frozenset({"op_name", "inputs", "params"}),
    "set_plot": frozenset({"spec"}),
}
SIGNAL_OPTIONAL: dict[str, frozenset[str]] = {
    "catalog_io": frozenset(),
    "inspect_io": frozenset(),
    "read_io": frozenset(),
    "write_data": frozenset({"selector"}),
    "list_members": frozenset({"offset", "limit"}),
    "member_preview": frozenset(),
    "apply_multi": frozenset(),
    "filter_preview": frozenset(
        {"generation", "recorded_object_id", "recorded_selector"}
    ),
    "set_plot": frozenset(),
}
SIGNAL_MUTATIONS = frozenset({"read_io", "write_data", "apply_multi"})


def execute_signal_command(
    controller: Any, kind: str, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Execute a supported signal-tool command, returning only detached values."""
    if kind == "catalog_io":
        return {
            "io_catalog": controller.catalog_io(
                payload["datatype"], payload["direction"]
            )
        }
    if kind == "inspect_io":
        return {"io_inspection": controller.inspect_io(payload["request"])}
    if kind == "read_io":
        return {"objects": controller.read_io(payload["inspection"])}
    if kind == "write_data":
        return {
            "activity": controller.write_data(
                payload["object_id"],
                payload["request"],
                selector=payload.get("selector"),
            )
        }
    if kind == "list_members":
        return {
            "object_id": payload["object_id"],
            "members": controller.list_members(
                payload["object_id"],
                offset=payload.get("offset", 0),
                limit=payload.get("limit", 100),
            ),
        }
    if kind == "member_preview":
        return {
            "preview_payload": controller.fetch_member_preview_payload(
                payload["object_id"], selector=payload["selector"]
            )
        }
    if kind == "apply_multi":
        return {
            "object": controller.apply_multi(
                payload["op_name"], payload["inputs"], payload["params"]
            )
        }
    if kind == "filter_preview":
        return controller.filter_preview(
            payload["op_name"],
            payload["inputs"],
            payload["params"],
            generation=payload.get("generation", 0),
            recorded_object_id=payload.get("recorded_object_id"),
            recorded_selector=payload.get("recorded_selector"),
        )
    if kind == "set_plot":
        controller.set_plot_spec(payload["spec"])
        return {"plot_updated": True}
    return None
