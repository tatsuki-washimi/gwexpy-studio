"""Stable read-to-source identity, independent of scientific reader arguments."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..errors import ProjectFormatError

if TYPE_CHECKING:
    from .model import Operation
    from .project import Project


def source_bindings_from_dict(value: Any) -> dict[str, tuple[str, ...]]:
    """Parse ordered source IDs without accepting strings as ID sequences."""
    if not isinstance(value, Mapping):
        raise ProjectFormatError("source_bindings: expected object")
    result = {}
    for op_id, ids in value.items():
        if (
            not isinstance(op_id, str)
            or not op_id
            or not isinstance(ids, (tuple, list))
            or not ids
            or any(not isinstance(item, str) or not item for item in ids)
            or len(ids) != len(set(ids))
        ):
            raise ProjectFormatError("source_bindings: invalid ordered source IDs")
        result[op_id] = tuple(ids)
    return result


def validate_source_bindings(project: Project) -> None:
    """Require bindings to reference the exact ordered native read inputs."""
    sources = {source.source_id: source for source in project.sources}
    if len(sources) != len(project.sources):
        raise ProjectFormatError("source_bindings: duplicate source ID")
    operations = {op.op_id: op for op in project.graph.operations}
    for op_id, ids in source_bindings_from_dict(project.source_bindings).items():
        op = operations.get(op_id)
        if (
            op is None
            or op.operation_id != "data.read"
            or not set(ids) <= sources.keys()
        ):
            raise ProjectFormatError("source_bindings: unknown read or source ID")
        source = op.params.get("source")
        if not isinstance(source, (str, tuple, list)):
            raise ProjectFormatError("source_bindings: invalid native read paths")
        paths = (source,) if isinstance(source, str) else tuple(source or ())
        if tuple(sources[source_id].uri for source_id in ids) != paths:
            raise ProjectFormatError("source_bindings: read input order differs")


def source_ids_for_operation(project: Project, op: Operation) -> tuple[str, ...]:
    """Resolve exact new bindings or canonical references in older projects."""
    if op.op_id in project.source_bindings:
        return project.source_bindings[op.op_id]
    source = op.params.get("source")
    if isinstance(source, Mapping):
        source_id = source.get("source_id")
        return (source_id,) if isinstance(source_id, str) else ()
    paths = (source,) if isinstance(source, str) else source
    if not isinstance(paths, (tuple, list)):
        return ()
    # Older native projects recorded paths, not per-read source associations.
    # Use one canonical reference per path/format; never show every duplicate.
    result = []
    for path in paths:
        candidates = [item for item in project.sources if item.uri == path]
        matching = [
            item for item in candidates if item.format == op.params.get("format")
        ]
        if matching or candidates:
            result.append((matching or candidates)[0].source_id)
    return tuple(result)
