"""Project v3 visual and complete-input evidence serialization."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from gwexpy_studio.domain.model import DataObjectRef, Operation, PlotSpec
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import ProjectFormatError


def _project():
    project = Project(
        project_id="schema",
        created="2026-09-06T00:00:00Z",
        modified="2026-09-06T00:00:00Z",
        compatibility={"studio": "0.1", "gwexpy": "0.2"},
    )
    project.graph.add(
        Operation(
            op_id="op-1",
            operation_id="data.read",
            operation_schema=1,
            outputs=("obj-1",),
        )
    )
    project.objects = (
        DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(3,),
            dtype="float64",
            unit="m",
            produced_by="op-1",
        ),
    )
    return project


def _manifest():
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "content_sha256": "a" * 64,
        "inventory_sha256": "b" * 64,
        "root_count": 1,
        "entry_count": 2,
        "total_bytes": 100,
        "max_bytes": 1000,
        "max_entries": 10,
    }


@pytest.mark.contract("WSP-0077")
def test_bode_phase_limits_are_validated_and_roundtrip():
    assert "phase_ylim" in PlotSpec.__dataclass_fields__, (
        "Bode second axis needs persisted limits"
    )
    project = _project()
    project.plots = (
        PlotSpec(
            plot_id="plot-bode",
            kind="bode",
            object_ids=("obj-1",),
            ylim=(-60, 20),
            phase_ylim=(-270, 45),
        ),
    )
    document = project.to_dict()
    restored = Project.from_dict(document)
    assert restored.plots == project.plots
    for bad in (True, [1], [1, False], [float("inf"), 2], "0, 1"):
        document["plots"][0]["phase_ylim"] = bad
        with pytest.raises(ProjectFormatError, match="phase_ylim|Non-finite"):
            Project.from_dict(document)


@pytest.mark.contract("WSP-0078")
def test_v3_schema_accepts_workspace_evidence_and_interrupted_activities():
    from gwexpy_studio.domain.model import ActivityRecord

    project = _project()
    project.source_manifests = {"op-1": _manifest()}
    project.activities = (
        ActivityRecord(
            activity_id="session-uuid",
            action="interrupted_command",
            started_at=project.created,
            status="unknown",
            details={"pending": "write"},
        ),
    )
    schema_path = (
        Path(__file__).parents[2] / "src/gwexpy_studio/schemas/project-v3.schema.json"
    )
    assert schema_path.exists(), "Project v3 needs a published JSON schema"
    schema = json.loads(schema_path.read_text())
    Draft202012Validator(schema).validate(project.to_dict())
    assert (
        Project.from_dict(project.to_dict()).source_manifests
        == project.source_manifests
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param(
            "schema_version",
            True,
            marks=pytest.mark.contract("WSP-0079"),
            id="boolean-version",
        ),
        pytest.param(
            "algorithm",
            "md5",
            marks=pytest.mark.contract("WSP-0080"),
            id="wrong-algorithm",
        ),
        pytest.param(
            "content_sha256",
            "bad",
            marks=pytest.mark.contract("WSP-0081"),
            id="invalid-content-hash",
        ),
        pytest.param(
            "inventory_sha256",
            "F" * 64,
            marks=pytest.mark.contract("WSP-0082"),
            id="uppercase-inventory-hash",
        ),
        pytest.param(
            "root_count",
            True,
            marks=pytest.mark.contract("WSP-0083"),
            id="boolean-root-count",
        ),
        pytest.param(
            "entry_count",
            -1,
            marks=pytest.mark.contract("WSP-0084"),
            id="negative-entry-count",
        ),
        pytest.param(
            "total_bytes",
            1.1,
            marks=pytest.mark.contract("WSP-0085"),
            id="noninteger-total-bytes",
        ),
        pytest.param(
            "max_bytes", 0, marks=pytest.mark.contract("WSP-0086"), id="zero-byte-bound"
        ),
        pytest.param(
            "max_entries",
            False,
            marks=pytest.mark.contract("WSP-0087"),
            id="boolean-entry-bound",
        ),
        pytest.param(
            "extra", [], marks=pytest.mark.contract("WSP-0088"), id="extra-field"
        ),
    ],
)
def test_source_manifest_rejects_incomplete_or_malformed_evidence(key, value):
    project = _project()
    project.source_manifests = {"op-1": {**_manifest(), key: value}}
    with pytest.raises(ProjectFormatError, match="source_manifests"):
        Project.from_dict(project.to_dict())


@pytest.mark.contract("WSP-0089")
def test_source_manifest_requires_existing_operation_and_every_evidence_field():
    project = _project()
    for manifest in (
        {},
        {key: value for key, value in _manifest().items() if key != "root_count"},
    ):
        project.source_manifests = {"op-1": manifest}
        with pytest.raises(ProjectFormatError, match="source_manifests"):
            Project.from_dict(project.to_dict())
    with pytest.raises(ProjectFormatError, match="source_manifests"):
        replace(project, source_manifests={"op-missing": _manifest()}).validate()


@pytest.mark.parametrize(
    "version",
    [
        pytest.param(1, marks=pytest.mark.contract("WSP-0090"), id="v1"),
        pytest.param(2, marks=pytest.mark.contract("WSP-0091"), id="v2"),
    ],
)
def test_legacy_documents_reject_v3_phase_limits(version):
    project = _project()
    project.schema_version = version
    project.plots = (PlotSpec(plot_id="plot-line", kind="line", object_ids=("obj-1",)),)
    document = project.to_dict()
    document["plots"][0]["phase_ylim"] = [0, 1]
    with pytest.raises(ProjectFormatError, match="phase_ylim|v3"):
        Project.from_dict(document)


@pytest.mark.contract("WSP-0092")
def test_v3_rejects_non_data_ui_state():
    project = _project()
    document = project.to_dict()
    document["ui_state"] = {"array": object()}
    with pytest.raises(ProjectFormatError, match="JSON"):
        Project.from_dict(document)


@pytest.mark.contract("WSP-0093")
def test_v3_schema_accepts_worker_uuid_execution_ids_and_history():
    from gwexpy_studio.domain.history import begin_history, commit_history
    from gwexpy_studio.domain.model import ExecutionRecord

    project = _project()
    project.objects = ()
    from gwexpy_studio.domain.graph import OperationGraph

    operation = project.graph.operations[0]
    project.graph = OperationGraph()
    pending = begin_history(project, "Read Data")
    project.graph.add(operation)
    project.objects = _project().objects
    project.executions = (
        ExecutionRecord(
            execution_id="4936072f-d13f-4f5c-b8c2-33962a2f905d",
            op_id="op-1",
            started_at=project.created,
            duration_s=1,
            status="succeeded",
        ),
    )
    commit_history(project, pending, output_ids=("obj-1",))
    schema_path = (
        Path(__file__).parents[2] / "src/gwexpy_studio/schemas/project-v3.schema.json"
    )
    Draft202012Validator(json.loads(schema_path.read_text())).validate(
        project.to_dict()
    )
