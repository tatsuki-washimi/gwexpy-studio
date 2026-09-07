"""Strict parsing and reference validation of persistent workspace history."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from ..errors import ProjectFormatError
from .history import ActionGroup, HistoryEvent, HistoryState, object_closure
from .project_fields import (
    _require_bool,
    _require_int,
    _require_list,
    _require_str,
    _require_str_tuple,
)
from .project_v2 import _selector, _validate_json

if TYPE_CHECKING:
    from .project import Project


def _keys(value: Mapping[str, Any], names: set[str], path: str) -> None:
    if set(value) != names:
        raise ProjectFormatError(f"{path}: invalid fields (missing or unknown keys)")


def _handles(
    value: Mapping[str, Any], key: str, path: str
) -> tuple[Mapping[str, Any], ...]:
    handles = []
    for index, handle in enumerate(_require_list(value, key, path)):
        here = f"{path}.{key}[{index}]"
        if not isinstance(handle, Mapping):
            raise ProjectFormatError(f"{here}: expected handle object")
        _keys(handle, {"object_id", "selector"}, here)
        selector = None if handle["selector"] is None else _selector(handle, here)
        handles.append(
            {"object_id": _require_str(handle, "object_id", here), "selector": selector}
        )
    return tuple(handles)


def history_from_dict(value: Mapping[str, Any]) -> HistoryState:
    """Read all history records without inferring missing user gestures."""
    _keys(
        value,
        {
            "initialized",
            "baseline_heads",
            "baseline_selection",
            "groups",
            "timeline",
            "cursor",
            "events",
        },
        "history",
    )
    groups = []
    for index, raw in enumerate(_require_list(value, "groups", "history")):
        path = f"history.groups[{index}]"
        if not isinstance(raw, Mapping):
            raise ProjectFormatError(f"{path}: expected action object")
        _keys(
            raw,
            {
                "action_id",
                "label",
                "operation_ids",
                "before_heads",
                "after_heads",
                "before_selection",
                "after_selection",
            },
            path,
        )
        groups.append(
            ActionGroup(
                action_id=_require_str(raw, "action_id", path),
                label=_require_str(raw, "label", path),
                operation_ids=_require_str_tuple(raw, "operation_ids", path),
                before_heads=_require_str_tuple(raw, "before_heads", path),
                after_heads=_require_str_tuple(raw, "after_heads", path),
                before_selection=_handles(raw, "before_selection", path),
                after_selection=_handles(raw, "after_selection", path),
            )
        )
    events = []
    for index, raw in enumerate(_require_list(value, "events", "history")):
        path = f"history.events[{index}]"
        if not isinstance(raw, Mapping):
            raise ProjectFormatError(f"{path}: expected event object")
        _keys(
            raw,
            {
                "event_id",
                "action",
                "action_id",
                "timestamp",
                "before_cursor",
                "after_cursor",
            },
            path,
        )
        action = _require_str(raw, "action", path)
        if action not in ("commit", "undo", "redo"):
            raise ProjectFormatError(f"{path}.action: expected commit, undo, or redo")
        events.append(
            HistoryEvent(
                event_id=_require_str(raw, "event_id", path),
                action=cast(Literal["commit", "undo", "redo"], action),
                action_id=_require_str(raw, "action_id", path),
                timestamp=_require_str(raw, "timestamp", path),
                before_cursor=_require_int(raw, "before_cursor", path),
                after_cursor=_require_int(raw, "after_cursor", path),
            )
        )
    return HistoryState(
        initialized=_require_bool(value, "initialized", "history"),
        baseline_heads=_require_str_tuple(value, "baseline_heads", "history"),
        baseline_selection=_handles(value, "baseline_selection", "history"),
        groups=tuple(groups),
        timeline=_require_str_tuple(value, "timeline", "history"),
        cursor=_require_int(value, "cursor", "history"),
        events=tuple(events),
    )


def _unique(values: tuple[str, ...], path: str) -> None:
    if len(set(values)) != len(values) or any(not value for value in values):
        raise ProjectFormatError(f"{path}: IDs must be nonempty and unique")


def validate_history(project: Project) -> None:
    """Verify semantic group references and replay the append-only cursor audit."""
    state = project.history
    groups = {group.action_id: group for group in state.groups}
    _unique(tuple(group.action_id for group in state.groups), "history.groups")
    _unique(tuple(event.event_id for event in state.events), "history.events")
    _unique(state.timeline, "history.timeline")
    if type(state.cursor) is not int or not 0 <= state.cursor <= len(state.timeline):
        raise ProjectFormatError("history.cursor: outside timeline bounds")
    if not set(state.timeline) <= groups.keys():
        raise ProjectFormatError("history.timeline: unknown action")
    if not state.initialized and (
        state.groups or state.events or state.timeline or state.cursor
    ):
        raise ProjectFormatError("history: uninitialized state cannot contain actions")
    known = {obj.object_id for obj in project.objects} | {
        output for op in project.graph.operations for output in op.outputs
    }
    ops = {op.op_id: op for op in project.graph.operations}
    successful = {
        ex.op_id for ex in project.executions if ex.status in ("succeeded", "success")
    }
    materialized = {obj.object_id for obj in project.objects}

    def check_view(
        heads: tuple[str, ...], handles: tuple[Mapping[str, Any], ...]
    ) -> None:
        _unique(heads, "history heads")
        if not set(heads) <= known:
            raise ProjectFormatError("history heads: unknown object")
        closure = object_closure(project, heads)
        for handle in handles:
            if handle["object_id"] not in closure:
                raise ProjectFormatError(
                    "history selection: object is outside active closure"
                )

    check_view(state.baseline_heads, state.baseline_selection)
    used_ops: set[str] = set()
    for group in state.groups:
        _unique(group.operation_ids, "history operation_ids")
        if not group.operation_ids or not set(group.operation_ids) <= ops.keys():
            raise ProjectFormatError(
                "history operation_ids: missing or unknown operation"
            )
        if used_ops.intersection(group.operation_ids):
            raise ProjectFormatError("history: operation belongs to multiple actions")
        used_ops.update(group.operation_ids)
        if not set(group.operation_ids) <= successful:
            raise ProjectFormatError(
                "history: action operations must have successful executions"
            )
        outputs = {
            output for op_id in group.operation_ids for output in ops[op_id].outputs
        }
        if not outputs <= materialized:
            raise ProjectFormatError(
                "history: successful action outputs must be materialized"
            )
        allowed = object_closure(project, group.before_heads) | outputs
        if any(
            value not in allowed
            for op_id in group.operation_ids
            for value in ops[op_id].inputs.values()
        ):
            raise ProjectFormatError("history: action depends on inactive object")
        if not set(group.after_heads) <= set(group.before_heads) | outputs:
            raise ProjectFormatError("history: action activates unrelated object")
        check_view(group.before_heads, group.before_selection)
        check_view(group.after_heads, group.after_selection)
    timeline: tuple[str, ...] = ()
    cursor = 0
    committed: list[str] = []
    for event in state.events:
        if event.action_id not in groups or event.before_cursor != cursor:
            raise ProjectFormatError(
                "history.events: invalid action or cursor transition"
            )
        if event.action == "commit":
            group = groups[event.action_id]
            heads = (
                state.baseline_heads
                if cursor == 0
                else groups[timeline[cursor - 1]].after_heads
            )
            if group.before_heads != heads or event.action_id in committed:
                raise ProjectFormatError("history.events: invalid action commit")
            committed.append(event.action_id)
            timeline = (*timeline[:cursor], event.action_id)
            cursor += 1
        elif event.action == "undo":
            if cursor == 0 or timeline[cursor - 1] != event.action_id:
                raise ProjectFormatError("history.events: invalid undo")
            cursor -= 1
        elif event.action == "redo":
            if cursor == len(timeline) or timeline[cursor] != event.action_id:
                raise ProjectFormatError("history.events: invalid redo")
            cursor += 1
        else:
            raise ProjectFormatError("history.events: invalid event action")
        if event.after_cursor != cursor:
            raise ProjectFormatError("history.events: invalid resulting cursor")
    if (
        timeline != state.timeline
        or cursor != state.cursor
        or tuple(committed) != tuple(groups)
    ):
        raise ProjectFormatError("history: timeline does not match event audit")


def source_manifests_from_dict(
    value: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Validate compact, complete-input fingerprints keyed by read operation ID."""
    result: dict[str, Mapping[str, Any]] = {}
    for op_id, raw in value.items():
        path = f"source_manifests.{op_id}"
        if not isinstance(op_id, str) or not isinstance(raw, Mapping):
            raise ProjectFormatError(
                f"{path}: expected operation ID and evidence object"
            )
        _keys(
            raw,
            {
                "schema_version",
                "algorithm",
                "content_sha256",
                "inventory_sha256",
                "root_count",
                "entry_count",
                "total_bytes",
                "max_bytes",
                "max_entries",
            },
            path,
        )
        if (
            type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or raw["algorithm"] != "sha256"
        ):
            raise ProjectFormatError(f"{path}: unsupported source fingerprint format")
        for name in ("content_sha256", "inventory_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", _require_str(raw, name, path)) is None:
                raise ProjectFormatError(f"{path}.{name}: expected sha256 digest")
        for name in (
            "root_count",
            "entry_count",
            "total_bytes",
            "max_bytes",
            "max_entries",
        ):
            minimum = 1 if name in ("max_bytes", "max_entries") else 0
            if _require_int(raw, name, path) < minimum:
                raise ProjectFormatError(f"{path}.{name}: invalid count or bound")
        _validate_json(raw, path)
        result[op_id] = dict(raw)
    return result
