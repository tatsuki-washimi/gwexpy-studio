"""Workspace export excludes undone branches and external side effects."""

from __future__ import annotations

import ast

import pytest

from gwexpy_studio.domain.history import (
    active_object_ids,
    active_source_ids,
    begin_history,
    commit_history,
    undo_history,
)
from gwexpy_studio.domain.model import (
    ActivityRecord,
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
)
from gwexpy_studio.domain.project import Project
from gwexpy_studio.export.python_exporter import export_python


def _project():
    return Project(
        project_id="export",
        created="2026-09-06T00:00:00Z",
        modified="2026-09-06T00:00:00Z",
        compatibility={"studio": "0.1", "gwexpy": "0.2"},
    )


def _append(project, name, source=None):
    op_id, object_id = project.new_operation_id(), project.new_object_id()
    project.graph.add(
        Operation(
            op_id=op_id,
            operation_id=name,
            operation_schema=1,
            inputs={"self": source} if source else {},
            outputs=(object_id,),
            params={"detrend": "linear"}
            if name.endswith("detrend")
            else {"selector": {"index": 0}}
            if name == "data.extract"
            else {"source": {"source_id": "src-1"}, "format": "hdf5"},
        )
    )
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
    project.executions += (
        ExecutionRecord(
            execution_id=f"exec-{len(project.executions) + 1}",
            op_id=op_id,
            started_at=project.created,
            duration_s=0,
            status="succeeded",
        ),
    )
    return object_id


def _action(project, name, source=None):
    pending = begin_history(project, name)
    output = _append(project, name, source)
    commit_history(project, pending, output_ids=(output,))
    return output


def _write(project, output, *, parent=None, selector=None, status="succeeded"):
    target = f"/tmp/{output}.h5"
    project.activities += (
        ActivityRecord(
            activity_id=project.new_activity_id(),
            action="write_data",
            started_at=project.created,
            status=status,
            object_id=output,
            target=target,
            details={
                "request": {"format": "hdf5"},
                **(
                    {"logical_handle": {"object_id": parent, "selector": selector}}
                    if parent
                    else {}
                ),
            },
        ),
    )
    return target


@pytest.mark.contract("WSP-0021")
def test_default_export_omits_undone_science_and_its_successful_write():
    project = _project()
    project.sources = (
        DataSourceRef(source_id="src-1", uri="/tmp/raw.h5", format="hdf5"),
    )
    raw = _action(project, "timeseries.read")
    result = _action(project, "timeseries.detrend", raw)
    target = _write(project, result)
    undo_history(project)
    script = export_python(project, include_data_writes=True)
    ast.parse(script)
    assert "# timeseries.detrend" not in script
    assert target not in script
    assert active_source_ids(project) == {"src-1"}


@pytest.mark.contract("WSP-0022")
def test_explicit_write_export_uses_active_logical_parent_without_activating_extract():
    project = _project()
    raw = _action(project, "timeseries.read")
    extracted = _append(project, "data.extract", raw)
    target = _write(project, extracted, parent=raw, selector={"index": 0})
    assert active_object_ids(project) == {raw}
    ordinary = export_python(project)
    assert target not in ordinary
    assert "# data.extract" not in ordinary
    included = export_python(project, include_data_writes=True)
    ast.parse(included)
    assert target in included
    assert "# data.extract" in included
    assert active_object_ids(project) == {raw}
    undo_history(project)
    assert target not in export_python(project, include_data_writes=True)


@pytest.mark.contract("WSP-0023")
def test_failed_writes_and_inconsistent_logical_handles_are_never_exported():
    project = _project()
    raw = _action(project, "timeseries.read")
    extracted = _append(project, "data.extract", raw)
    failed = _write(project, raw, status="failed")
    mismatch = _write(project, extracted, parent=raw, selector={"index": 2})
    script = export_python(project, include_data_writes=True)
    assert failed not in script
    assert mismatch not in script


@pytest.mark.contract("WSP-0024")
def test_legacy_write_extract_can_be_traced_to_active_parent():
    project = _project()
    raw = _action(project, "timeseries.read")
    extracted = _append(project, "data.extract", raw)
    target = _write(project, extracted)
    assert target in export_python(project, include_data_writes=True)
