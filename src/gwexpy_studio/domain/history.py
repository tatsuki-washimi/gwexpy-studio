"""Persistent action boundaries over an append-only scientific graph."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from ..errors import ProjectFormatError
from .sources import source_ids_for_operation

if TYPE_CHECKING:
    from .project import Project

Handles = tuple[Mapping[str, Any], ...]


def freeze_handles(handles: Collection[Mapping[str, Any]]) -> Handles:
    """Detach selection handles and make their two mapping levels immutable."""
    return tuple(
        MappingProxyType(
            {
                "object_id": handle["object_id"],
                "selector": MappingProxyType(dict(handle["selector"]))
                if handle.get("selector") is not None
                else None,
            }
        )
        for handle in handles
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class ActionGroup:
    """One successful user gesture, including its extraction dependencies."""

    action_id: str
    label: str
    operation_ids: tuple[str, ...]
    before_heads: tuple[str, ...]
    after_heads: tuple[str, ...]
    before_selection: Handles = ()
    after_selection: Handles = ()

    def __post_init__(self) -> None:
        """Own immutable copies of externally supplied selection dictionaries."""
        for name in ("before_selection", "after_selection"):
            object.__setattr__(self, name, freeze_handles(getattr(self, name)))


@dataclass(frozen=True, kw_only=True, slots=True)
class HistoryEvent:
    """Append-only audit of an action commit or a cursor movement."""

    event_id: str
    action: Literal["commit", "undo", "redo"]
    action_id: str
    timestamp: str
    before_cursor: int
    after_cursor: int


@dataclass(frozen=True, kw_only=True, slots=True)
class HistoryState:
    """All semantic groups plus the current branch and persistent undo cursor."""

    initialized: bool = False
    baseline_heads: tuple[str, ...] = ()
    baseline_selection: Handles = ()
    groups: tuple[ActionGroup, ...] = ()
    timeline: tuple[str, ...] = ()
    cursor: int = 0
    events: tuple[HistoryEvent, ...] = ()

    def __post_init__(self) -> None:
        """Detach baseline selection dictionaries from callers."""
        object.__setattr__(
            self, "baseline_selection", freeze_handles(self.baseline_selection)
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class PendingHistory:
    """Ephemeral transaction snapshot; failures need no history mutation."""

    project_id: str
    label: str
    history: HistoryState
    existing_operation_ids: frozenset[str]
    before_selection: Handles


def _legacy_targets(project: Project) -> tuple[str, ...]:
    materialized = tuple(obj.object_id for obj in project.objects)
    candidates = materialized
    if not candidates and not project.executions:
        candidates = tuple(out for op in project.graph.operations for out in op.outputs)
    available = set(candidates)
    consumed = {
        obj_id
        for op in project.graph.operations
        if any(out in available for out in op.outputs)
        for obj_id in op.inputs.values()
    }
    return (
        tuple(obj_id for obj_id in candidates if obj_id not in consumed) or candidates
    )


def _heads(state: HistoryState) -> tuple[str, ...]:
    if state.cursor == 0:
        return state.baseline_heads
    action_id = state.timeline[state.cursor - 1]
    return next(
        group.after_heads for group in state.groups if group.action_id == action_id
    )


def active_targets(project: Project) -> tuple[str, ...]:
    """Return the visible analysis leaves, retaining legacy graph semantics."""
    return (
        _heads(project.history)
        if project.history.initialized
        else _legacy_targets(project)
    )


def object_closure(project: Project, heads: Collection[str]) -> frozenset[str]:
    """Return heads and all required objects in their dependency closure."""
    result = set(heads)
    for op in project.graph.ancestors(heads):
        result.update(op.outputs)
        result.update(op.inputs.values())
    return frozenset(result)


def active_object_ids(project: Project) -> frozenset[str]:
    """Return exactly the current analysis objects and their dependencies."""
    return object_closure(project, active_targets(project))


def active_source_ids(project: Project) -> frozenset[str]:
    """Resolve source identity from the active graph, including repeated paths."""
    return frozenset(
        source_id
        for op in project.graph.ancestors(active_targets(project))
        for source_id in source_ids_for_operation(project, op)
    )


def selected_handles(project: Project) -> Handles:
    """Return selection restored by the most recent analysis cursor change."""
    return _selected_handles(project)


def _selected_handles(project: Project) -> Handles:
    state = project.history
    if not state.events:
        return state.baseline_selection
    event = state.events[-1]
    group = next(group for group in state.groups if group.action_id == event.action_id)
    return group.before_selection if event.action == "undo" else group.after_selection


def can_undo(project: Project) -> bool:
    """Whether one committed user gesture precedes the current cursor."""
    return project.history.cursor > 0


def can_redo(project: Project) -> bool:
    """Whether the current branch contains a redoable committed gesture."""
    return project.history.cursor < len(project.history.timeline)


def begin_history(
    project: Project,
    label: str,
    *,
    selected_handles: Collection[Mapping[str, Any]] | None = None,
) -> PendingHistory:
    """Capture the committed baseline before appending any gesture operations."""
    selection = (
        freeze_handles(selected_handles)
        if selected_handles is not None
        else _selected_handles(project)
    )
    if not project.history.initialized:
        project.history = replace(
            project.history,
            initialized=True,
            baseline_heads=_legacy_targets(project),
            baseline_selection=selection,
        )
    return PendingHistory(
        project_id=project.project_id,
        label=label,
        history=project.history,
        existing_operation_ids=frozenset(op.op_id for op in project.graph.operations),
        before_selection=selection,
    )


def _event(
    action: Literal["commit", "undo", "redo"], action_id: str, before: int, after: int
) -> HistoryEvent:
    return HistoryEvent(
        event_id=str(uuid.uuid4()),
        action=action,
        action_id=action_id,
        timestamp=datetime.now(UTC).isoformat(),
        before_cursor=before,
        after_cursor=after,
    )


def commit_history(
    project: Project,
    pending: PendingHistory,
    *,
    output_ids: Collection[str],
    selected_handles: Collection[Mapping[str, Any]] = (),
) -> ActionGroup:
    """Publish a fully successful gesture; only this operation discards redo."""
    if project.project_id != pending.project_id or project.history != pending.history:
        raise ProjectFormatError("History transaction is stale")
    operations = tuple(
        op
        for op in project.graph.operations
        if op.op_id not in pending.existing_operation_ids
    )
    successful = {
        record.op_id
        for record in project.executions
        if record.status in ("succeeded", "success")
    }
    materialized = {obj.object_id for obj in project.objects}
    outputs = {output for op in operations for output in op.outputs}
    wanted = tuple(dict.fromkeys(output_ids))
    if (
        not operations
        or not wanted
        or not set(wanted) <= outputs
        or not outputs <= materialized
        or any(op.op_id not in successful for op in operations)
    ):
        raise ProjectFormatError("History requires successful materialized operations")
    before = active_targets(project)
    allowed = object_closure(project, before) | outputs
    if any(value not in allowed for op in operations for value in op.inputs.values()):
        raise ProjectFormatError("History operation depends on an inactive object")
    consumed = {value for op in operations for value in op.inputs.values()}
    after = tuple(
        value
        for value in dict.fromkeys((*before, *wanted))
        if value not in consumed or value in wanted
    )
    group = ActionGroup(
        action_id=str(uuid.uuid4()),
        label=pending.label,
        operation_ids=tuple(op.op_id for op in operations),
        before_heads=before,
        after_heads=after,
        before_selection=pending.before_selection,
        after_selection=freeze_handles(selected_handles),
    )
    state = project.history
    candidate = replace(
        state,
        groups=(*state.groups, group),
        timeline=(*state.timeline[: state.cursor], group.action_id),
        cursor=state.cursor + 1,
        events=(
            *state.events,
            _event("commit", group.action_id, state.cursor, state.cursor + 1),
        ),
    )
    from .project_v3 import validate_history

    validate_history(replace(project, history=candidate))
    project.history = candidate
    return group


def undo_history(project: Project) -> HistoryEvent:
    """Move back one gesture while preserving all graph and execution records."""
    if not can_undo(project):
        raise ProjectFormatError("No analysis action to undo")
    state = project.history
    event = _event(
        "undo", state.timeline[state.cursor - 1], state.cursor, state.cursor - 1
    )
    project.history = replace(
        state, cursor=state.cursor - 1, events=(*state.events, event)
    )
    return event


def redo_history(project: Project) -> HistoryEvent:
    """Reactivate the same action and IDs after cache availability is confirmed."""
    if not can_redo(project):
        raise ProjectFormatError("No analysis action to redo")
    state = project.history
    event = _event("redo", state.timeline[state.cursor], state.cursor, state.cursor + 1)
    project.history = replace(
        state, cursor=state.cursor + 1, events=(*state.events, event)
    )
    return event
