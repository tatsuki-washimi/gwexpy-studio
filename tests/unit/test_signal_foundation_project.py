"""Project v2 migration, scientific metadata, and activity persistence."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from gwexpy_studio.domain import model
from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import DataObjectRef, Operation, PlotSpec
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import ProjectFormatError
from gwexpy_studio.persistence.project_io import load_project, save_project


def _project():
    return Project(
        project_id="test",
        created="2026-09-05T00:00:00Z",
        modified="2026-09-05T00:00:00Z",
        compatibility={"studio": "0.1", "gwexpy": "0.2"},
    )


def _v1_document():
    return {
        "schema_version": 1,
        "project_id": "legacy",
        "created": "2026-09-05T00:00:00Z",
        "modified": "2026-09-05T00:00:00Z",
        "compatibility": {"studio": "0.1", "gwexpy": "0.1.14"},
        "sources": [],
        "objects": [
            {
                "object_id": "obj-1",
                "kind": "TimeSeries",
                "shape": [3],
                "dtype": "float64",
                "unit": "m",
                "name": "length",
                "channel": None,
                "axes": {
                    "t0": {"value": 1.0, "unit": "s"},
                    "dt": {"value": 0.5, "unit": "s"},
                },
                "produced_by": None,
            }
        ],
        "operations": [],
        "executions": [],
        "plots": [],
        "ui_state": {"selected": "obj-1"},
    }


@pytest.mark.contract("SIG-0015")
def test_new_projects_default_to_v2():
    assert _project().schema_version == 3


@pytest.mark.contract("SIG-0016")
def test_v1_is_strictly_validated_then_migrated_without_input_mutation():
    document = _v1_document()
    before = deepcopy(document)
    project = Project.from_dict(document)
    assert document == before
    assert project.schema_version == 3
    migrated = project.to_dict()
    assert migrated["schema_version"] == 3
    for key, value in before["objects"][0].items():
        assert migrated["objects"][0][key] == value
    assert migrated["ui_state"] == before["ui_state"]
    assert Project.from_dict(migrated).to_dict() == migrated


@pytest.mark.parametrize("bad", [True, 2.5, "2", 0, 3, None])
def test_unknown_schema_rejected(bad):
    with pytest.raises(ProjectFormatError):
        Project.from_dict({**_v1_document(), "schema_version": bad})


@pytest.mark.contract("SIG-0017")
def test_v1_unit_allowlist_is_not_bypassed_by_migration():
    document = _v1_document()
    document["objects"][0]["unit"] = "furlong / fortnight"
    with pytest.raises(ProjectFormatError, match="unit"):
        Project.from_dict(document)


@pytest.mark.contract("SIG-0018")
def test_v2_accepts_native_units_null_container_metadata_and_explicit_axes(tmp_path):
    project = _project()
    member = model.MemberRef(
        selector={"key": 42},
        label="forty two",
        kind="FrequencySeries",
        shape=(3,),
        dtype="complex128",
        unit="electron / pix",
        axes={
            "frequency": {
                "kind": "explicit",
                "unit": "Hz",
                "length": 3,
                "dtype": "float128",
            }
        },
    )
    project.objects = (
        DataObjectRef(
            object_id="obj-1",
            kind="FrequencySeriesDict",
            shape=(2,),
            dtype=None,
            unit=None,
            metadata={
                "native_class": "gwexpy.frequencyseries.FrequencySeriesDict",
                "member_count": 2,
            },
            members=(member,),
        ),
    )
    path = tmp_path / "project.gwxproj"
    save_project(project, path, clock=lambda: project.modified)
    restored = load_project(path)
    assert restored.objects == project.objects
    assert restored.objects[0].members[0].selector == {"key": 42}
    assert restored.to_dict() == project.to_dict()


@pytest.mark.contract("SIG-0019")
def test_activity_is_not_an_operation_and_execution_details_roundtrip():
    project = _project()
    project.activities = (
        model.ActivityRecord(
            activity_id="activity-1",
            action="write",
            started_at=project.created,
            status="succeeded",
            object_id="obj-1",
            target="/tmp/test.h5",
            details={"format": "hdf5", "member_selector": {"index": 2}},
        ),
    )
    project.graph = OperationGraph(
        [
            Operation(
                op_id="op-1",
                operation_id="timeseries.read",
                operation_schema=1,
                outputs=("obj-1",),
            )
        ]
    )
    project.executions = (
        model.ExecutionRecord(
            execution_id="exec-1",
            op_id="op-1",
            started_at=project.created,
            duration_s=0.1,
            status="succeeded",
            details={"coefficients": [1, 2, 3]},
            warnings=("native warning",),
        ),
    )
    restored = Project.from_dict(project.to_dict())
    assert restored.activities == project.activities
    assert restored.executions == project.executions
    assert len(restored.graph.operations) == 1
    assert restored.new_activity_id() == "activity-2"


@pytest.mark.contract("SIG-0020")
def test_bode_and_complex_plot_settings_roundtrip():
    project = _project()
    project.objects = (
        DataObjectRef(
            object_id="obj-1",
            kind="FrequencySeries",
            shape=(3,),
            dtype="complex128",
            unit="",
        ),
    )
    project.plots = (
        PlotSpec(
            plot_id="plot-1",
            kind="bode",
            object_ids=("obj-1",),
            component="abs",
            magnitude_scale="linear",
            db_reference=2.0,
            display_unit="mV",
            phase_unwrap=False,
        ),
    )
    assert Project.from_dict(project.to_dict()).plots == project.plots


@pytest.mark.parametrize(
    "key,bad",
    [
        ("component", "nonsense"),
        ("magnitude_scale", "log10"),
        ("db_reference", 0),
        ("db_reference", True),
        ("phase_unwrap", "false"),
        ("display_unit", 3),
    ],
)
def test_invalid_v2_plot_settings_rejected(key, bad):
    document = _project().to_dict()
    document["plots"] = [
        {**asdict(PlotSpec(plot_id="plot-1", kind="line", object_ids=())), key: bad}
    ]
    with pytest.raises(ProjectFormatError):
        Project.from_dict(document)


@pytest.mark.contract("SIG-0021")
def test_v2_published_schema_accepts_roundtrip_document():
    path = (
        Path(__file__).parents[2] / "src/gwexpy_studio/schemas/project-v3.schema.json"
    )
    assert path.exists()
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(_project().to_dict())) == []


@pytest.mark.contract("SIG-0022")
def test_v2_registered_format_strings_survive_and_v1_stays_strict():
    document = _v1_document()
    document["sources"] = [
        {
            "source_id": "src-1",
            "uri": "/data/series.txt",
            "format": "ascii.ecsv",
            "size_bytes": None,
            "mtime": None,
        }
    ]
    with pytest.raises(ProjectFormatError, match="format"):
        Project.from_dict(document)
    document["schema_version"] = 2
    assert Project.from_dict(document).sources[0].format == "ascii.ecsv"
    for invalid in ("", "   ", "x" * 129, [], 3):
        document["sources"][0]["format"] = invalid
        with pytest.raises(ProjectFormatError, match="format"):
            Project.from_dict(document)


@pytest.mark.contract("SIG-0023")
def test_public_object_reader_validates_finite_typed_metadata():
    from gwexpy_studio.domain.project_v2 import object_from_dict

    document = _v1_document()["objects"][0]
    assert object_from_dict(document).kind == "TimeSeries"
    for metadata in (
        {"member_count": True},
        {"value": float("nan")},
        {"value": object()},
    ):
        with pytest.raises(ProjectFormatError):
            object_from_dict({**document, "metadata": metadata})


@pytest.mark.parametrize(
    "axis",
    [
        {"kind": "explicit", "unit": "s", "length": 2, "dtype": "float64"},
        {"kind": "explicit", "unit": "s", "length": True, "dtype": "float64"},
        {
            "kind": "explicit",
            "unit": "s",
            "length": 3,
            "dtype": "float64",
            "values": [1, 2, 4],
        },
    ],
)
def test_v2_explicit_axis_descriptor_rejects_invalid_length_or_inline_arrays(axis):
    document = _v1_document()
    document["schema_version"] = 2
    document["objects"][0]["axes"] = {"time": axis}
    with pytest.raises(ProjectFormatError):
        Project.from_dict(document)


@pytest.mark.contract("SIG-0024")
def test_duplicate_activity_ids_are_rejected():
    project = _project()
    activity = model.ActivityRecord(
        activity_id="activity-1",
        action="write",
        started_at=project.created,
        status="succeeded",
    )
    document = project.to_dict()
    document["activities"] = [asdict(activity), asdict(activity)]
    with pytest.raises(ProjectFormatError, match="activity"):
        Project.from_dict(document)


@pytest.mark.parametrize(
    "kind,shape",
    [
        ("TimeSeries", [2, 3]),
        ("Spectrogram", [3]),
        ("TimeSeriesDict", [2, 3]),
        ("FrequencySeriesMatrix", [3]),
        ("SpectrogramMatrix", [2, 3]),
    ],
)
def test_v2_rejects_shapes_that_do_not_match_native_kind(kind, shape):
    from gwexpy_studio.domain.project_v2 import object_from_dict

    with pytest.raises(ProjectFormatError, match="shape"):
        object_from_dict(
            {**_v1_document()["objects"][0], "kind": kind, "shape": shape, "axes": {}}
        )


@pytest.mark.contract("SIG-0025")
def test_member_snapshots_require_a_valid_selector_for_the_parent():
    from gwexpy_studio.domain.project_v2 import object_from_dict

    member = model.MemberRef(
        selector={"index": 0},
        label="invalid dict selector",
        kind="TimeSeries",
        shape=(3,),
        dtype="float64",
        unit="m",
    )
    parent = DataObjectRef(
        object_id="obj-1",
        kind="TimeSeriesDict",
        shape=(1,),
        dtype="float64",
        unit="m",
        members=(member,),
    )
    with pytest.raises(ProjectFormatError, match="selector"):
        object_from_dict(asdict(parent))


@pytest.mark.contract("SIG-0026")
def test_v1_rejects_v2_only_scientific_fields_before_migration():
    document = _v1_document()
    document["objects"][0]["metadata"] = {"native_class": "native"}
    with pytest.raises(ProjectFormatError, match="v1"):
        Project.from_dict(document)
