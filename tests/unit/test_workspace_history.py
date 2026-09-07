"""Persistent semantic action groups and active analysis state."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from importlib import import_module

import pytest

from gwexpy_studio.domain.model import DataObjectRef, ExecutionRecord, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import ProjectFormatError


def _project():
    return Project(
        project_id="workspace",
        created="2026-09-06T00:00:00Z",
        modified="2026-09-06T00:00:00Z",
        compatibility={"studio": "0.1", "gwexpy": "0.2"},
    )


def _history():
    assert hasattr(Project(), "history"), "Project needs persistent semantic history"
    return import_module("gwexpy_studio.domain.history")


def _append(project, *, source=None, success=True):
    op_id, object_id = project.new_operation_id(), project.new_object_id()
    project.graph.add(
        Operation(
            op_id=op_id,
            operation_id="timeseries.detrend" if source else "timeseries.read",
            operation_schema=1,
            inputs={"self": source} if source else {},
            params={"type": "linear"}
            if source
            else {"source": "/tmp/a.h5", "format": "hdf5"},
            outputs=(object_id,),
        )
    )
    project.executions += (
        ExecutionRecord(
            execution_id=f"exec-{len(project.executions) + 1}",
            op_id=op_id,
            started_at=project.created,
            duration_s=0.0,
            status="succeeded" if success else "failed",
            details={"coefficients": [1, 2]},
        ),
    )
    if success:
        project.objects += (
            DataObjectRef(
                object_id=object_id,
                kind="TimeSeries",
                shape=(3,),
                dtype="float64",
                unit="m",
                produced_by=op_id,
            ),
        )
    return object_id


def _action(project, label, source=None):
    history = _history()
    pending = history.begin_history(project, label)
    output = _append(project, source=source)
    group = history.commit_history(
        project,
        pending,
        output_ids=(output,),
        selected_handles=({"object_id": output, "selector": None},),
    )
    return output, group


@pytest.mark.contract("WSP-0025")
def test_undo_redo_roundtrip_preserves_graph_execution_details_and_selection():
    history = _history()
    project = _project()
    raw, _ = _action(project, "Read Data")
    result, group = _action(project, "Apply", raw)
    before = project.to_dict()
    assert history.active_targets(project) == (result,)
    assert history.active_object_ids(project) == {raw, result}
    event = history.undo_history(project)
    assert event.action == "undo"
    assert history.active_targets(project) == (raw,)
    assert history.selected_handles(project)[0]["object_id"] == raw
    restored = Project.from_dict(project.to_dict())
    assert history.can_redo(restored)
    history.redo_history(restored)
    assert history.active_targets(restored) == (result,)
    assert restored.to_dict()["operations"] == before["operations"]
    assert restored.to_dict()["executions"] == before["executions"]
    assert restored.history.groups[1] == group
    assert [event.action for event in restored.history.events] == [
        "commit",
        "commit",
        "undo",
        "redo",
    ]


@pytest.mark.contract("WSP-0026")
def test_failed_action_retains_redo_and_successful_branch_retains_old_provenance():
    history = _history()
    project = _project()
    raw, _ = _action(project, "Read")
    old, old_group = _action(project, "Old", raw)
    history.undo_history(project)
    pending = history.begin_history(project, "Failed")
    failed = _append(project, source=raw, success=False)
    with pytest.raises(ProjectFormatError, match="successful"):
        history.commit_history(project, pending, output_ids=(failed,))
    assert history.can_redo(project)
    new, _ = _action(project, "New", raw)
    assert not history.can_redo(project)
    assert old_group in project.history.groups
    assert {old, failed}.isdisjoint(history.active_object_ids(project))
    assert history.active_targets(project) == (new,)
    assert len(project.executions) == 4
    assert Project.from_dict(project.to_dict()).to_dict() == project.to_dict()


@pytest.mark.contract("WSP-0027")
def test_whole_group_is_inactive_until_commit_and_undo_hides_all_children():
    history = _history()
    project = _project()
    pending = history.begin_history(project, "Read individual")
    first, second = _append(project), _append(project)
    assert history.active_object_ids(project) == set()
    group = history.commit_history(project, pending, output_ids=(first, second))
    assert len(group.operation_ids) == 2
    assert history.active_object_ids(project) == {first, second}
    history.undo_history(project)
    assert history.active_object_ids(project) == set()


@pytest.mark.contract("WSP-0028")
def test_legacy_raw_graph_defaults_remain_dynamic_until_explicit_transaction():
    history = _history()
    project = _project()
    raw = _append(project)
    assert history.active_targets(project) == (raw,)
    result = _append(project, source=raw)
    assert history.active_targets(project) == (result,)
    assert not history.can_undo(project)
    pending = history.begin_history(project, "New")
    newer = _append(project, source=result)
    history.commit_history(project, pending, output_ids=(newer,))
    history.undo_history(project)
    assert history.active_targets(project) == (result,)


@pytest.mark.contract("WSP-0029")
def test_never_run_graph_retains_declared_leaf_default_targets():
    history = _history()
    project = _project()
    project.graph.add(
        Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            outputs=("obj-1",),
        )
    )
    assert history.active_targets(project) == ("obj-1",)
    assert history.active_object_ids(project) == {"obj-1"}


@pytest.mark.contract("WSP-0030")
def test_action_selection_snapshots_are_immutable_and_independent():
    history = _history()
    project = _project()
    pending = history.begin_history(project, "Read")
    output = _append(project)
    handle = {"object_id": output, "selector": {"index": 0}}
    group = history.commit_history(
        project, pending, output_ids=(output,), selected_handles=(handle,)
    )
    handle["selector"]["index"] = 3
    assert group.after_selection[0]["selector"]["index"] == 0
    with pytest.raises((TypeError, FrozenInstanceError)):
        group.after_selection[0]["selector"]["index"] = 5
    with pytest.raises(FrozenInstanceError):
        group.label = "Changed"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            lambda doc: doc["history"].update(cursor=100),
            marks=pytest.mark.contract("WSP-0031"),
            id="cursor-range",
        ),
        pytest.param(
            lambda doc: doc["history"]["timeline"].append("missing"),
            marks=pytest.mark.contract("WSP-0032"),
            id="missing-timeline-group",
        ),
        pytest.param(
            lambda doc: doc["history"]["groups"][0]["after_heads"].append("missing"),
            marks=pytest.mark.contract("WSP-0033"),
            id="missing-after-head",
        ),
        pytest.param(
            lambda doc: doc["history"]["events"][0].update(action="execute"),
            marks=pytest.mark.contract("WSP-0034"),
            id="unknown-event-action",
        ),
        pytest.param(
            lambda doc: doc["history"].update(unknown=True),
            marks=pytest.mark.contract("WSP-0035"),
            id="unknown-history-key",
        ),
        pytest.param(
            lambda doc: doc["history"]["groups"][0]["after_selection"][0].update(
                object_id="missing"
            ),
            marks=pytest.mark.contract("WSP-0036"),
            id="missing-selected-object",
        ),
    ],
)
def test_history_parser_rejects_corrupt_state(change):
    _history()
    project = _project()
    _action(project, "Read")
    document = project.to_dict()
    change(document)
    with pytest.raises(ProjectFormatError):
        Project.from_dict(document)


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(1, marks=pytest.mark.contract("WSP-0037"), id="v1"),
        pytest.param(2, marks=pytest.mark.contract("WSP-0038"), id="v2"),
    ],
)
def test_legacy_migration_sets_baseline_without_guessing_actions(version):
    history = _history()
    project = _project()
    output = _append(project)
    project.schema_version = version
    restored = Project.from_dict(project.to_dict())
    assert restored.schema_version == 3
    assert history.active_targets(restored) == (output,)
    assert not history.can_undo(restored)
    assert restored.history.groups == ()
    assert restored.source_manifests == {}


@pytest.mark.contract("WSP-0039")
def test_commit_invalid_selection_is_rejected_before_publishing():
    history = _history()
    project = _project()
    pending = history.begin_history(project, "Read")
    output = _append(project)
    before = project.history
    with pytest.raises(ProjectFormatError, match="selection"):
        history.commit_history(
            project,
            pending,
            output_ids=(output,),
            selected_handles=({"object_id": "missing", "selector": None},),
        )
    assert project.history == before


@pytest.mark.contract("WSP-0040")
def test_undo_restores_selection_at_gesture_start_even_after_view_selection_changes():
    history = _history()
    project = _project()
    first, _ = _action(project, "Read first")
    second, _ = _action(project, "Read second")
    before_selection = ({"object_id": first, "selector": None},)
    pending = history.begin_history(project, "Apply", selected_handles=before_selection)
    output = _append(project, source=first)
    history.commit_history(project, pending, output_ids=(output,))
    history.undo_history(project)
    assert history.selected_handles(project) == before_selection
    assert set(history.active_targets(project)) == {first, second}


@pytest.mark.contract("WSP-0041")
def test_pending_snapshot_can_commit_against_roundtrip_clone_and_rejects_stale_commit():
    history = _history()
    project = _project()
    pending = history.begin_history(project, "Read")
    clone = Project.from_dict(project.to_dict())
    output = _append(clone)
    history.commit_history(clone, pending, output_ids=(output,))
    with pytest.raises(ProjectFormatError, match="stale"):
        history.commit_history(clone, pending, output_ids=(output,))
    other = _project()
    other.project_id = "other"
    with pytest.raises(ProjectFormatError, match="stale"):
        history.commit_history(other, pending, output_ids=(output,))


@pytest.mark.contract("WSP-0042")
def test_empty_history_undo_redo_are_rejected():
    history = _history()
    project = _project()
    for operation in (history.undo_history, history.redo_history):
        with pytest.raises(ProjectFormatError, match="No analysis"):
            operation(project)


@pytest.mark.contract("WSP-0043")
def test_apply_cannot_publish_a_dependency_from_an_undone_branch():
    history = _history()
    project = _project()
    raw, _ = _action(project, "Read")
    old, _ = _action(project, "Old", raw)
    history.undo_history(project)
    pending = history.begin_history(project, "Invalid")
    output = _append(project, source=old)
    with pytest.raises(ProjectFormatError, match="inactive"):
        history.commit_history(project, pending, output_ids=(output,))
    assert history.can_redo(project)
