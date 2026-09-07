"""Select recorded file writes whose logical scientific input remains active."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from ..domain.model import ActivityRecord
from ..domain.project import Project


def active_writes(
    project: Project, object_ids: Collection[str]
) -> tuple[ActivityRecord, ...]:
    """Include successful writes and matching extraction-only dependencies."""
    active = set(object_ids)
    producers = {output: op for op in project.graph.operations for output in op.outputs}
    successful = {
        record.op_id
        for record in project.executions
        if record.status in ("succeeded", "success")
    }
    materialized = {obj.object_id for obj in project.objects}
    selected = []
    for record in project.activities:
        if (
            record.action not in ("write", "write_data")
            or record.status not in ("succeeded", "success")
            or record.object_id is None
            or record.target is None
            or record.object_id not in materialized
        ):
            continue
        producer = producers.get(record.object_id)
        is_extract = producer is not None and producer.operation_id == "data.extract"
        handle = record.details.get("logical_handle")
        if handle is not None:
            if not isinstance(handle, Mapping):
                continue
            parent = handle.get("object_id")
            selector = handle.get("selector")
            if selector is None:
                if parent != record.object_id:
                    continue
            elif (
                not is_extract
                or producer is None
                or producer.inputs.get("self") != parent
                or producer.params.get("selector") != selector
            ):
                continue
        elif is_extract and producer is not None:
            parent = producer.inputs.get("self")
        else:
            parent = record.object_id
        if parent not in active:
            continue
        if record.object_id not in active and (
            not is_extract or producer is None or producer.op_id not in successful
        ):
            continue
        selected.append(record)
    return tuple(selected)
