"""Data projection models and UI formatting helpers for Studio."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem

from ..domain.history import active_object_ids, active_source_ids
from ..domain.model import DataObjectRef, ExecutionRecord, MemberRef, Operation
from ..domain.project import Project
from ..domain.sources import source_ids_for_operation

MEMBER_ROLE = int(Qt.ItemDataRole.UserRole) + 1
MORE_ROLE = int(Qt.ItemDataRole.UserRole) + 2
LEAF_KINDS = frozenset({"TimeSeries", "FrequencySeries", "Spectrogram"})


def populate_source_tree(tree: QTreeWidget, project: Project) -> None:
    """Populate source tree widget with sources and their derived data objects."""
    tree.clear()

    op_map: dict[str, Operation] = {op.op_id: op for op in project.graph.operations}
    source_items: dict[str, QTreeWidgetItem] = {}
    active = active_object_ids(project) if project.history.initialized else None
    sources = active_source_ids(project) if project.history.initialized else None

    for src in project.sources:
        if sources is not None and src.source_id not in sources:
            continue
        item = QTreeWidgetItem(tree, [src.uri, src.format])
        item.setData(0, Qt.ItemDataRole.UserRole, src.source_id)
        source_items[src.source_id] = item

    for obj in project.objects:
        if active is not None and obj.object_id not in active:
            continue
        parent_item: QTreeWidgetItem | None = None
        curr_op_id: str | None = obj.produced_by
        while curr_op_id and curr_op_id in op_map:
            op = op_map[curr_op_id]
            source_ids = source_ids_for_operation(project, op)
            if source_ids and source_ids[0] in source_items:
                parent_item = source_items[source_ids[0]]
                break
            input_obj_ids = list(op.inputs.values())
            if input_obj_ids:
                in_obj = next(
                    (o for o in project.objects if o.object_id == input_obj_ids[0]),
                    None,
                )
                curr_op_id = in_obj.produced_by if in_obj else None
            else:
                break

        parent = parent_item if parent_item is not None else tree
        obj_item = QTreeWidgetItem(parent, [obj.object_id, obj.kind])
        obj_item.setData(0, Qt.ItemDataRole.UserRole, obj.object_id)
        if obj.kind not in LEAF_KINDS:
            obj_item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            )
            if obj.members:
                populate_member_items(obj_item, obj.object_id, obj.members)


def populate_member_items(
    parent: QTreeWidgetItem, object_id: str, members: tuple[MemberRef, ...]
) -> None:
    """Project lazy native members onto tree rows without creating graph objects."""
    parent.takeChildren()
    for member in members:
        item = QTreeWidgetItem(parent, [member.label, member.kind])
        item.setData(0, Qt.ItemDataRole.UserRole, object_id)
        item.setData(0, MEMBER_ROLE, member)
    parent.setChildIndicatorPolicy(
        QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
    )


def populate_metadata_tree(
    tree: QTreeWidget, obj: DataObjectRef | MemberRef | None
) -> None:
    """Populate metadata tree widget with detailed properties of selected object."""
    tree.clear()
    if obj is None:
        return

    props: list[tuple[str, str]] = [
        ("Object ID", getattr(obj, "object_id", "<member>")),
        ("Kind", obj.kind),
        ("Shape", str(obj.shape)),
        ("Dtype", obj.dtype or "<per member>"),
        ("Unit", obj.unit or "<per member>"),
        ("Name", obj.name or "<None>"),
        ("Channel", obj.channel or "<None>"),
        ("Produced By", getattr(obj, "produced_by", None) or "<Root>"),
    ]

    for axis_name, axis_info in sorted(obj.axes.items()):
        val = axis_info.get("value")
        unit = axis_info.get("unit", "")
        props.append((f"Axis: {axis_name}", f"{val} {unit}".strip()))

    for name, value in obj.metadata.items():
        props.append((str(name), str(value)))
    if isinstance(obj, MemberRef):
        props.append(("Selector", str(dict(obj.selector))))

    for prop_name, prop_val in props:
        QTreeWidgetItem(tree, [prop_name, prop_val])


def populate_history_list(
    list_widget: QListWidget,
    project: Project,
) -> None:
    """Populate scientific history list using OperationGraph insertion order."""
    list_widget.clear()
    latest_by_op: dict[str, ExecutionRecord] = {}
    for record in project.executions:
        latest_by_op[record.op_id] = record
    active = active_object_ids(project) if project.history.initialized else None

    for op in project.graph.operations:
        latest_execution = latest_by_op.get(op.op_id)
        status = latest_execution.status if latest_execution is not None else "not-run"
        label = f"{op.operation_id} [{status}] (id={op.op_id})"
        if active is not None and not active.intersection(op.outputs):
            label += " (inactive)"
        if latest_execution is not None and latest_execution.status == "failed":
            error = latest_execution.error or {}
            code = error.get("code") or "<unknown>"
            message = error.get("message") or "<none>"
            label = f"{label} code={code} message={message}"
        item = QListWidgetItem(label, list_widget)
        item.setData(Qt.ItemDataRole.UserRole, op.op_id)

    for activity in project.activities:
        category = (
            "Data write"
            if activity.action == "write_data"
            else "Restore data"
            if activity.action == "restore_project"
            else activity.action.replace("_", " ").capitalize()
        )
        label = f"{category}: {activity.target or ''} [{activity.status}]"
        if activity.error:
            label += (
                f" {activity.error.get('code', '')}: "
                f"{activity.error.get('message', '')}"
            )
        item = QListWidgetItem(label, list_widget)
        item.setData(Qt.ItemDataRole.UserRole, activity.activity_id)
