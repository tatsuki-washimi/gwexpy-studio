"""Contract tests for Project aggregate, validation, and replay provenance."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import (
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
    PlotSpec,
)
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import OperationError, ProjectFormatError
from gwexpy_studio.session import StudioSession


class _GraphSnapshot:
    """Minimal graph view used to seed project invariants deterministically."""

    def __init__(self, operations: tuple[Operation, ...]) -> None:
        self._operations = operations

    @property
    def operations(self) -> tuple[Operation, ...]:
        return self._operations

    def producer_of(self, object_id: str) -> Operation | None:
        for operation in self._operations:
            if object_id in operation.outputs:
                return operation
        return None

    def ancestors(self, target_object_ids: tuple[str, ...]) -> tuple[Operation, ...]:
        selected: set[str] = set()
        pending = list(target_object_ids)
        while pending:
            object_id = pending.pop()
            operation = self.producer_of(object_id)
            if operation is None or operation.op_id in selected:
                continue
            selected.add(operation.op_id)
            pending.extend(operation.inputs.values())
        return tuple(
            operation for operation in self._operations if operation.op_id in selected
        )


class _ReplayClient:
    """Deterministic client double for replay identity assertions."""

    def __init__(self, responses: tuple[tuple[str, ...], ...]) -> None:
        self.requests: list[Mapping[str, Any]] = []
        self.returned_ids: list[tuple[str, ...]] = []
        self._responses = list(responses)

    def request(
        self, message: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> Mapping[str, Any]:
        del timeout_s
        self.requests.append(message)
        if not self._responses:
            raise AssertionError("replay issued more requests than expected")
        object_ids = self._responses.pop(0)
        self.returned_ids.append(object_ids)
        return {
            "protocol": 2,
            "request_id": message.get("request_id", "request-1"),
            "type": "result",
            "payload": {
                "object_ids": list(object_ids),
                "ref": {"object_ids": list(object_ids)},
                "duration_s": 0.25,
                "warnings": [],
                "environment": {"python": "3.12.12"},
            },
        }


def _operation(
    op_id: str,
    *,
    operation_id: str = "timeseries.detrend",
    inputs: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    outputs: tuple[str, ...],
) -> Operation:
    """Build one deterministic operation record."""
    return Operation(
        op_id=op_id,
        operation_id=operation_id,
        operation_schema=1,
        inputs=dict(inputs or {}),
        params=dict(params or {}),
        outputs=outputs,
    )


def _serialized_operation(operation: Operation) -> str:
    """Return an immutable snapshot of every Operation field and nested value."""
    return json.dumps(
        {
            "op_id": operation.op_id,
            "operation_id": operation.operation_id,
            "operation_schema": operation.operation_schema,
            "inputs": operation.inputs,
            "params": operation.params,
            "outputs": operation.outputs,
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _serialized_operation_graph(
    operations: tuple[Operation, ...],
) -> tuple[str, ...]:
    """Snapshot the complete ordered graph without retaining mutable mappings."""
    return tuple(_serialized_operation(operation) for operation in operations)


def _source() -> DataSourceRef:
    """Return the fixed source reference used by project contracts."""
    return DataSourceRef(
        source_id="src-1",
        uri="/data/source.h5",
        format="hdf5",
        size_bytes=42,
        mtime=123.0,
    )


def _objects() -> tuple[DataObjectRef, ...]:
    """Return materialized object snapshots for a small linear graph."""
    return (
        DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(15360,),
            dtype="float64",
            unit="m",
            name="X1:STUDIO-TEST",
            channel="X1:STUDIO-CHANNEL",
            axes={
                "t0": {"value": 1_000_000_000.0, "unit": "s"},
                "dt": {"value": 0.00390625, "unit": "s"},
            },
            produced_by=None,
        ),
        DataObjectRef(
            object_id="obj-2",
            kind="TimeSeries",
            shape=(15360,),
            dtype="float64",
            unit="m",
            name="X1:STUDIO-TEST-detrended",
            channel="X1:STUDIO-CHANNEL",
            axes={
                "t0": {"value": 1_000_000_000.0, "unit": "s"},
                "dt": {"value": 0.00390625, "unit": "s"},
            },
            produced_by="op-2",
        ),
        DataObjectRef(
            object_id="obj-3",
            kind="FrequencySeries",
            shape=(7681,),
            dtype="float64",
            unit="m / Hz**0.5",
            name="X1:STUDIO-TEST-asd",
            channel="X1:STUDIO-CHANNEL",
            axes={
                "f0": {"value": 0.0, "unit": "Hz"},
                "df": {"value": 0.25, "unit": "Hz"},
            },
            produced_by="op-3",
        ),
    )


def _operations() -> tuple[Operation, ...]:
    """Return the fixed operation graph represented by the project fixture."""
    return (
        _operation(
            "op-1",
            operation_id="timeseries.read",
            params={"source": "/data/source.h5", "format": "hdf5"},
            outputs=("obj-1",),
        ),
        _operation(
            "op-2",
            inputs={"self": "obj-1"},
            params={"detrend": "linear"},
            outputs=("obj-2",),
        ),
        _operation(
            "op-3",
            operation_id="timeseries.asd",
            inputs={"self": "obj-2"},
            params={"fftlength": {"value": 4.0, "unit": "s"}},
            outputs=("obj-3",),
        ),
    )


def _executions() -> tuple[ExecutionRecord, ...]:
    """Return deterministic execution provenance for every operation."""
    return tuple(
        ExecutionRecord(
            execution_id=f"exec-{index}",
            op_id=f"op-{index}",
            started_at="2026-08-16T00:00:00Z",
            duration_s=float(index),
            status="succeeded",
            warnings=(),
            error=None,
            environment={"python": "3.12.12", "gwexpy": "0.1.14"},
        )
        for index in (1, 2, 3)
    )


def _full_project() -> Project:
    """Return a project with every aggregate section populated."""
    operations = _operations()
    return Project(
        schema_version=1,
        project_id="project-a",
        created="2026-08-16T00:00:00Z",
        modified="2026-08-16T00:00:01Z",
        compatibility={"studio": "0.1.0", "gwexpy": "0.1.14"},
        sources=(_source(),),
        objects=_objects(),
        graph=_GraphSnapshot(operations),
        executions=_executions(),
        plots=(
            PlotSpec(
                plot_id="plot-1",
                kind="line",
                object_ids=("obj-2", "obj-3"),
                xscale="linear",
                yscale="log",
                xlim=(0.0, 60.0),
                ylim=(1e-12, 1.0),
                title="deterministic project",
                xlabel="GPS seconds",
                ylabel="amplitude",
                legend=True,
                styles={"color": "black", "linewidth": 1.0},
            ),
        ),
        ui_state={"selected_object": "obj-3", "panel": {"width": 320}},
    )


def _full_manifest() -> dict[str, Any]:
    """Return the JSON-primitive form of ``_full_project``."""
    return {
        "schema_version": 1,
        "project_id": "project-a",
        "created": "2026-08-16T00:00:00Z",
        "modified": "2026-08-16T00:00:01Z",
        "compatibility": {"studio": "0.1.0", "gwexpy": "0.1.14"},
        "sources": [
            {
                "source_id": "src-1",
                "uri": "/data/source.h5",
                "format": "hdf5",
                "size_bytes": 42,
                "mtime": 123.0,
            }
        ],
        "objects": [
            {
                "object_id": "obj-1",
                "kind": "TimeSeries",
                "shape": [15360],
                "dtype": "float64",
                "unit": "m",
                "name": "X1:STUDIO-TEST",
                "channel": "X1:STUDIO-CHANNEL",
                "axes": {
                    "t0": {"value": 1_000_000_000.0, "unit": "s"},
                    "dt": {"value": 0.00390625, "unit": "s"},
                },
                "produced_by": None,
            },
            {
                "object_id": "obj-2",
                "kind": "TimeSeries",
                "shape": [15360],
                "dtype": "float64",
                "unit": "m",
                "name": "X1:STUDIO-TEST-detrended",
                "channel": "X1:STUDIO-CHANNEL",
                "axes": {
                    "t0": {"value": 1_000_000_000.0, "unit": "s"},
                    "dt": {"value": 0.00390625, "unit": "s"},
                },
                "produced_by": "op-2",
            },
            {
                "object_id": "obj-3",
                "kind": "FrequencySeries",
                "shape": [7681],
                "dtype": "float64",
                "unit": "m / Hz**0.5",
                "name": "X1:STUDIO-TEST-asd",
                "channel": "X1:STUDIO-CHANNEL",
                "axes": {
                    "f0": {"value": 0.0, "unit": "Hz"},
                    "df": {"value": 0.25, "unit": "Hz"},
                },
                "produced_by": "op-3",
            },
        ],
        "operations": [
            {
                "op_id": "op-1",
                "operation_id": "timeseries.read",
                "operation_schema": 1,
                "inputs": {},
                "params": {"source": "/data/source.h5", "format": "hdf5"},
                "outputs": ["obj-1"],
            },
            {
                "op_id": "op-2",
                "operation_id": "timeseries.detrend",
                "operation_schema": 1,
                "inputs": {"self": "obj-1"},
                "params": {"detrend": "linear"},
                "outputs": ["obj-2"],
            },
            {
                "op_id": "op-3",
                "operation_id": "timeseries.asd",
                "operation_schema": 1,
                "inputs": {"self": "obj-2"},
                "params": {"fftlength": {"value": 4.0, "unit": "s"}},
                "outputs": ["obj-3"],
            },
        ],
        "executions": [
            {
                "execution_id": "exec-1",
                "op_id": "op-1",
                "started_at": "2026-08-16T00:00:00Z",
                "duration_s": 1.0,
                "status": "succeeded",
                "warnings": [],
                "error": None,
                "environment": {"python": "3.12.12", "gwexpy": "0.1.14"},
            },
            {
                "execution_id": "exec-2",
                "op_id": "op-2",
                "started_at": "2026-08-16T00:00:00Z",
                "duration_s": 2.0,
                "status": "succeeded",
                "warnings": [],
                "error": None,
                "environment": {"python": "3.12.12", "gwexpy": "0.1.14"},
            },
            {
                "execution_id": "exec-3",
                "op_id": "op-3",
                "started_at": "2026-08-16T00:00:00Z",
                "duration_s": 3.0,
                "status": "succeeded",
                "warnings": [],
                "error": None,
                "environment": {"python": "3.12.12", "gwexpy": "0.1.14"},
            },
        ],
        "plots": [
            {
                "plot_id": "plot-1",
                "kind": "line",
                "object_ids": ["obj-2", "obj-3"],
                "xscale": "linear",
                "yscale": "log",
                "xlim": [0.0, 60.0],
                "ylim": [1e-12, 1.0],
                "title": "deterministic project",
                "xlabel": "GPS seconds",
                "ylabel": "amplitude",
                "legend": True,
                "styles": {"color": "black", "linewidth": 1.0},
            }
        ],
        "ui_state": {"selected_object": "obj-3", "panel": {"width": 320}},
    }


def _expect_project_format_error(callback: Callable[[], Any]) -> None:
    """Accept only the public format error once implementation exists."""
    try:
        callback()
    except ProjectFormatError:
        return
    raise AssertionError("invalid project input was accepted")


@pytest.mark.contract("C-A-020")
def test_project_default_state_is_data_only() -> None:
    """The aggregate default contains only serializable state containers."""
    project = Project()

    assert project.schema_version == 3
    assert isinstance(project.graph, OperationGraph)
    assert project.sources == ()
    assert project.objects == ()
    assert project.executions == ()
    assert project.plots == ()
    assert project.ui_state == {}


@pytest.mark.contract("C-A-021")
def test_project_allocates_the_next_source_id() -> None:
    """Source IDs grow naturally after loading one-based IDs."""
    project = Project(
        sources=(
            _source(),
            DataSourceRef(
                source_id="src-999",
                uri="/data/other.h5",
                format="hdf5",
            ),
        )
    )

    assert project.new_source_id() == "src-1000"


@pytest.mark.contract("C-A-022")
def test_project_allocates_the_next_object_id() -> None:
    """Object IDs grow naturally after loading one-based IDs."""
    objects = (
        _objects()[0],
        DataObjectRef(
            object_id="obj-999",
            kind="TimeSeries",
            shape=(1,),
            dtype="float64",
            unit="m",
        ),
    )
    project = Project(objects=objects)

    assert project.new_object_id() == "obj-1000"


@pytest.mark.contract("C-A-023")
def test_project_allocates_the_next_operation_id() -> None:
    """Operation IDs grow from the graph rather than a process counter."""
    operations = (
        _operation("op-1", outputs=("obj-1",)),
        _operation("op-999", outputs=("obj-999",)),
    )
    project = Project(graph=_GraphSnapshot(operations))

    assert project.new_operation_id() == "op-1000"


@pytest.mark.parametrize(
    "project",
    [
        pytest.param(
            Project(
                graph=_GraphSnapshot(
                    (
                        _operation(
                            "op-1",
                            inputs={"self": "obj-missing"},
                            outputs=("obj-1",),
                        ),
                    )
                )
            ),
            id="unknown-input",
            marks=pytest.mark.contract("C-A-024"),
        ),
        pytest.param(
            Project(
                objects=(
                    DataObjectRef(
                        object_id="obj-1",
                        kind="TimeSeries",
                        shape=(1,),
                        dtype="float64",
                        unit="m",
                        produced_by="op-missing",
                    ),
                ),
                graph=_GraphSnapshot((_operation("op-1", outputs=("obj-1",)),)),
            ),
            id="produced-by-mismatch",
            marks=pytest.mark.contract("C-A-070"),
        ),
        pytest.param(
            Project(
                graph=_GraphSnapshot((_operation("op-1", outputs=("obj-1",)),)),
                executions=(
                    ExecutionRecord(
                        execution_id="exec-1",
                        op_id="op-missing",
                        started_at="2026-08-16T00:00:00Z",
                        duration_s=0.0,
                        status="succeeded",
                    ),
                ),
            ),
            id="unknown-execution-operation",
            marks=pytest.mark.contract("C-A-071"),
        ),
        pytest.param(
            Project(
                plots=(
                    PlotSpec(
                        plot_id="plot-1",
                        kind="line",
                        object_ids=("obj-missing",),
                    ),
                ),
            ),
            id="unknown-plot-object",
            marks=pytest.mark.contract("C-A-072"),
        ),
    ],
)
def test_project_validate_rejects_each_reference_integrity_violation(
    project: Project,
) -> None:
    """Each reference class is rejected independently of the others."""
    _expect_project_format_error(project.validate)


@pytest.mark.contract("C-A-025")
def test_project_validate_retains_failed_execution_without_materializing_output() -> (
    None
):
    """A failed attempt remains recorded while its declared output is absent."""
    operation = _operation("op-1", outputs=("obj-2",))
    failed = ExecutionRecord(
        execution_id="exec-1",
        op_id="op-1",
        started_at="2026-08-16T00:00:00Z",
        duration_s=0.5,
        status="failed",
        warnings=("deterministic warning",),
        error={"code": "operation_failed", "message": "deterministic failure"},
    )
    project = Project(graph=_GraphSnapshot((operation,)), executions=(failed,))

    project.validate()

    assert project.executions == (failed,)
    assert all(obj.object_id != "obj-2" for obj in project.objects)


@pytest.mark.contract("C-A-038")
def test_project_allows_operation_before_execution_record() -> None:
    """Graph provenance is recorded before an execution attempt is made."""
    operation = _operation("op-1", outputs=("obj-1",))
    project = Project(
        graph=_GraphSnapshot((operation,)),
        objects=(),
        executions=(),
    )

    project.validate()

    assert project.graph.operations == (operation,)
    assert project.executions == ()
    assert project.objects == ()
    assert all(
        obj.object_id != output_id
        for obj in project.objects
        for output_id in operation.outputs
    )


@pytest.mark.contract("C-A-026")
def test_project_validate_rejects_downstream_input_without_materialization() -> None:
    """Downstream operations can consume only materialized object references."""
    operations = (
        _operation("op-1", outputs=("obj-1",)),
        _operation("op-2", inputs={"self": "obj-1"}, outputs=("obj-2",)),
    )
    project = Project(
        graph=_GraphSnapshot(operations),
        objects=(
            DataObjectRef(
                object_id="obj-2",
                kind="TimeSeries",
                shape=(1,),
                dtype="float64",
                unit="m",
                produced_by="op-2",
            ),
        ),
    )

    _expect_project_format_error(project.validate)


@pytest.mark.contract("C-A-027")
def test_project_to_dict_round_trips_all_aggregate_sections() -> None:
    """Serialization retains every project section as JSON primitives."""
    project = _full_project()

    document = project.to_dict()

    assert document == _full_manifest()
    json.dumps(document, allow_nan=False, sort_keys=True)


@pytest.mark.contract("C-A-028")
def test_project_from_dict_round_trips_all_aggregate_sections() -> None:
    """Deserialization restores the seven-item project state without loss."""
    document = _full_manifest()

    restored = Project.from_dict(document)

    from dataclasses import replace

    assert restored.schema_version == 3
    assert replace(restored, schema_version=1).to_dict() == document
    assert Project.from_dict(restored.to_dict()).to_dict() == restored.to_dict()


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            copy.deepcopy(_full_manifest()) | {"schema_version": 3},
            id="future-schema",
            marks=pytest.mark.contract("C-A-029"),
        ),
        pytest.param(
            copy.deepcopy(_full_manifest()) | {"future_field": "reject-me"},
            id="unknown-field",
            marks=pytest.mark.contract("C-A-030"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "objects": [
                    {
                        **copy.deepcopy(_full_manifest())["objects"][0],
                        "kind": "SeriesMatrix",
                    },
                    *copy.deepcopy(_full_manifest())["objects"][1:],
                ],
            },
            id="invalid-kind",
            marks=pytest.mark.contract("C-A-031"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "objects": [
                    {
                        **copy.deepcopy(_full_manifest())["objects"][0],
                        "unit": "not a valid unit",
                    },
                    *copy.deepcopy(_full_manifest())["objects"][1:],
                ],
            },
            id="invalid-unit",
            marks=pytest.mark.contract("C-A-032"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "objects": [
                    {
                        **copy.deepcopy(_full_manifest())["objects"][0],
                        "shape": [True],
                    },
                    *copy.deepcopy(_full_manifest())["objects"][1:],
                ],
            },
            id="bool-as-int",
            marks=pytest.mark.contract("C-A-033"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "operations": [
                    {
                        **copy.deepcopy(_full_manifest())["operations"][0],
                        "outputs": ["obj-1", "obj-1"],
                    },
                    *copy.deepcopy(_full_manifest())["operations"][1:],
                ],
            },
            id="duplicate-output",
            marks=pytest.mark.contract("C-A-034"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "executions": [
                    {
                        **copy.deepcopy(_full_manifest())["executions"][0],
                        "op_id": "op-999",
                    },
                    *copy.deepcopy(_full_manifest())["executions"][1:],
                ],
            },
            id="unknown-execution-operation",
            marks=pytest.mark.contract("C-A-035"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "operations": [
                    {
                        **copy.deepcopy(_full_manifest())["operations"][0],
                        "params": {"bad": float("nan")},
                    },
                    *copy.deepcopy(_full_manifest())["operations"][1:],
                ],
            },
            id="nan-value",
            marks=pytest.mark.contract("C-A-036"),
        ),
        # The five cases below (C-A-084..088) close a gap left by commit
        # b7ca693's "L3c 型検査化": from_dict used to coerce field values
        # (str()/int()/float()/bool()) instead of type-checking them, so
        # each of these was silently accepted with a corrupted value rather
        # than rejected -- see the commit message's own "実測した欠陥"
        # corpus, which listed exactly these cases as manually probed but
        # never turned into a contract.
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "executions": [
                    {
                        **copy.deepcopy(_full_manifest())["executions"][0],
                        "duration_s": "NaN",
                    },
                    *copy.deepcopy(_full_manifest())["executions"][1:],
                ],
            },
            id="string-nan-duration",
            marks=pytest.mark.contract("C-A-084"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "executions": [
                    {
                        **copy.deepcopy(_full_manifest())["executions"][0],
                        "duration_s": "Infinity",
                    },
                    *copy.deepcopy(_full_manifest())["executions"][1:],
                ],
            },
            id="string-infinity-duration",
            marks=pytest.mark.contract("C-A-085"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "plots": [
                    {
                        **copy.deepcopy(_full_manifest())["plots"][0],
                        "legend": "false",
                    },
                ],
            },
            id="string-false-legend",
            marks=pytest.mark.contract("C-A-086"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "operations": [
                    {
                        k: v
                        for k, v in copy.deepcopy(_full_manifest())["operations"][
                            0
                        ].items()
                        if k != "op_id"
                    },
                    *copy.deepcopy(_full_manifest())["operations"][1:],
                ],
                # executions[0].op_id == "op-1" (the very operation being
                # mutated here) -- without clearing executions, a coercion
                # regression (op_id silently defaulting to "") is masked by
                # the unrelated "executions[0].op_id: references unknown
                # operation" check instead of the missing-key check this
                # contract targets (confirmed by mutation testing: the
                # non-isolated version passed even with the coercion bug
                # reintroduced).
                "executions": [],
            },
            id="missing-required-op-id",
            marks=pytest.mark.contract("C-A-087"),
        ),
        pytest.param(
            {
                **copy.deepcopy(_full_manifest()),
                "operations": [
                    {
                        **copy.deepcopy(_full_manifest())["operations"][0],
                        "operation_schema": 1.5,
                    },
                    *copy.deepcopy(_full_manifest())["operations"][1:],
                ],
            },
            id="non-integer-operation-schema",
            marks=pytest.mark.contract("C-A-088"),
        ),
    ],
)
def test_project_from_dict_rejects_invalid_semantic_and_schema_corpus(
    document: Mapping[str, Any],
) -> None:
    """Strict loading rejects future, malformed, and non-finite project data."""
    _expect_project_format_error(lambda: Project.from_dict(document))


@pytest.mark.contract("C-A-037")
def test_replay_reuses_declared_output_ids_and_keeps_graph_unchanged() -> None:
    """Replay runs the target closure and appends only execution provenance."""
    operations = _operations()
    failed_operation = _operation(
        "op-4",
        inputs={"self": "obj-3"},
        outputs=("obj-4",),
    )
    project = _full_project()
    graph = _GraphSnapshot((*operations, failed_operation))
    project.graph = graph
    failed_execution = ExecutionRecord(
        execution_id="exec-4",
        op_id="op-4",
        started_at="2026-08-16T00:00:00Z",
        duration_s=0.0,
        status="failed",
        error={"code": "operation_failed", "message": "deterministic failure"},
    )
    project.executions = (*project.executions, failed_execution)
    client = _ReplayClient((("obj-1",), ("obj-2",), ("obj-3",)))
    session = StudioSession(project=project, graph=graph, client=client)  # type: ignore[arg-type]
    graph_before = _serialized_operation_graph(project.graph.operations)
    before_objects = project.objects
    before_executions = project.executions
    before_sources = project.sources
    before_plots = project.plots
    before_ui_state = project.ui_state

    session.replay(targets=("obj-3",))

    assert len(client.requests) == 3
    requested_operations = tuple(
        message["payload"]["operation"] for message in client.requests
    )
    assert tuple(operation["op_id"] for operation in requested_operations) == (
        "op-1",
        "op-2",
        "op-3",
    )
    requested_output_ids = tuple(
        tuple(operation["outputs"]) for operation in requested_operations
    )
    declared_output_ids = tuple(operation.outputs for operation in operations)
    assert requested_output_ids == declared_output_ids
    assert client.returned_ids == [("obj-1",), ("obj-2",), ("obj-3",)]
    assert _serialized_operation_graph(project.graph.operations) == graph_before
    assert project.objects == before_objects
    assert project.sources == before_sources
    assert project.plots == before_plots
    assert project.ui_state == before_ui_state
    assert project.executions[: len(before_executions)] == before_executions
    appended = project.executions[len(before_executions) :]
    assert len(appended) == 3
    assert tuple(record.op_id for record in appended) == ("op-1", "op-2", "op-3")
    assert "obj-4" not in {obj.object_id for obj in project.objects}
    assert "op-4" not in {
        message["payload"]["operation"]["op_id"] for message in client.requests
    }


@pytest.mark.contract("C-A-089")
def test_replay_of_one_branch_preserves_untouched_sibling_branch_object() -> None:
    """A partial replay of one DAG branch never touches a sibling branch's object.

    ADR-0006 (Decision 1 / rejected alternatives) forbids a non-destructive
    operation from removing or mutating provenance it did not itself
    regenerate. Before this contract, ``StudioSession.replay(targets=...)``
    filtered ``Project.objects`` down to the replayed branch's ancestor
    closure, silently dropping an untouched sibling branch's already
    materialized ``DataObjectRef`` (REQ-A-068).
    """
    shared_read = _operation(
        "op-1",
        operation_id="timeseries.read",
        params={"source": "/data/source.h5", "format": "hdf5"},
        outputs=("obj-1",),
    )
    shared_detrend = _operation(
        "op-2",
        inputs={"self": "obj-1"},
        params={"detrend": "linear"},
        outputs=("obj-2",),
    )
    asd_branch = _operation(
        "op-3",
        operation_id="timeseries.asd",
        inputs={"self": "obj-2"},
        params={"fftlength": {"value": 4.0, "unit": "s"}},
        outputs=("obj-3",),
    )
    spectrogram_branch = _operation(
        "op-4",
        operation_id="timeseries.spectrogram",
        inputs={"self": "obj-2"},
        params={"stride": {"value": 4.0, "unit": "s"}},
        outputs=("obj-4",),
    )
    operations = (shared_read, shared_detrend, asd_branch, spectrogram_branch)
    graph = _GraphSnapshot(operations)

    # obj-4 is the sibling (spectrogram) branch's output: already
    # materialized from a prior run, but its producing op-4 is not part of
    # this replay's target closure.
    sibling_ref = DataObjectRef(
        object_id="obj-4",
        kind="Spectrogram",
        shape=(64, 128),
        dtype="float64",
        unit="m**2 / Hz",
        name="X1:STUDIO-TEST-spectrogram",
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": 1_000_000_000.0, "unit": "s"},
            "dt": {"value": 4.0, "unit": "s"},
            "f0": {"value": 0.0, "unit": "Hz"},
            "df": {"value": 0.25, "unit": "Hz"},
        },
        produced_by="op-4",
    )
    project = Project(
        schema_version=1,
        project_id="project-branches",
        created="2026-08-16T00:00:00Z",
        modified="2026-08-16T00:00:01Z",
        compatibility={"studio": "0.1.0", "gwexpy": "0.1.14"},
        sources=(_source(),),
        objects=(sibling_ref,),
        graph=graph,
        executions=(),
        plots=(),
        ui_state={},
    )

    # _ReplayClient never returns "object_ref" in its payload, so replaying
    # the asd branch cannot itself add any new DataObjectRef -- this isolates
    # the assertion to "was the sibling's ref removed", not "was a new ref
    # appended", matching the C-A-037 pattern.
    client = _ReplayClient((("obj-1",), ("obj-2",), ("obj-3",)))
    session = StudioSession(project=project, graph=graph, client=client)  # type: ignore[arg-type]
    before_objects = project.objects

    session.replay(targets=("obj-3",))

    # Only the replayed (asd) branch's ancestor closure was sent to the
    # client; the sibling (spectrogram) branch's op-4 was never executed.
    requested_op_ids = tuple(
        message["payload"]["operation"]["op_id"] for message in client.requests
    )
    assert requested_op_ids == ("op-1", "op-2", "op-3")
    assert "op-4" not in requested_op_ids

    # Project.objects is completely untouched -- same tuple identity, and
    # the sibling ref is the exact same object, not just an equal copy.
    assert project.objects is before_objects
    assert project.objects == (sibling_ref,)
    (surviving_ref,) = project.objects
    assert surviving_ref is sibling_ref
    assert surviving_ref.kind == "Spectrogram"
    assert surviving_ref.shape == (64, 128)
    assert surviving_ref.dtype == "float64"
    assert surviving_ref.unit == "m**2 / Hz"
    assert surviving_ref.name == "X1:STUDIO-TEST-spectrogram"
    assert surviving_ref.channel == "X1:STUDIO-CHANNEL"
    assert surviving_ref.axes == sibling_ref.axes
    assert surviving_ref.produced_by == "op-4"


@pytest.mark.contract("C-A-081")
def test_replay_without_a_client_raises_worker_unavailable_without_recording() -> None:
    """Replay with no worker client raises before any ExecutionRecord is appended.

    A missing client cannot be treated as a no-op success because that would
    create provenance without a worker execution or materialized object.
    """
    project = _full_project()
    graph = project.graph
    session = StudioSession(project=project, graph=graph, client=None)
    executions_before = project.executions
    objects_before = project.objects

    try:
        session.replay(targets=("obj-1",))
    except OperationError as error:
        assert error.code == "worker_unavailable"
    else:
        raise AssertionError("replay with no client silently succeeded")

    assert project.executions == executions_before
    assert project.objects == objects_before
    assert "op-1" not in session._successful_ops
