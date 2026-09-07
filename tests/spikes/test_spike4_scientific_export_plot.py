"""Spike 4 contracts for public science, export round-trip, and Agg plotting."""

from __future__ import annotations

import ast
import copy
import inspect
import json
import multiprocessing
import os
import subprocess
import sys
from dataclasses import dataclass
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import pytest
from astropy import units as u

matplotlib.use("Agg", force=True)

from matplotlib.figure import Figure

from gwexpy_studio.domain.model import (
    DataObjectRef,
    DataSourceRef,
    Operation,
    PlotSpec,
)
from gwexpy_studio.domain.project import Project
from gwexpy_studio.export.python_exporter import export_python
from gwexpy_studio.plotting.renderer import render_plot
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient, WorkerLifecycle
from gwexpy_studio.worker.service import worker_main
from gwexpy_studio.worker.shm import attach_block
from tests.support.fixtures import (
    SAMPLE_RATE_HZ,
    T0_GPS,
    TEST_NAME,
    write_named_hdf5,
)
from tests.support.sentinel import invoke_preserving_sentinel

pytestmark = pytest.mark.integration


class _GraphFixture:
    """Graph view sufficient to express one branch without production behavior."""

    def __init__(self, operations: tuple[Operation, ...]) -> None:
        self._operations = operations

    @property
    def operations(self) -> tuple[Operation, ...]:
        return self._operations

    def producer_of(self, object_id: str) -> Operation | None:
        return next(
            (
                operation
                for operation in self._operations
                if object_id in operation.outputs
            ),
            None,
        )

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

    def add(self, operation: Operation) -> None:
        """Allow the downstream declaration only after its input has failed."""
        self._operations = (*self._operations, operation)


def _public_reference(source: Path, branch: str) -> Any:
    """Compute the scientific oracle directly from the named public API."""
    import gwexpy

    gwexpy.register_all(include_io=False)
    from gwexpy.timeseries import TimeSeries

    raw = TimeSeries.read(source, format="hdf5", name=TEST_NAME)
    cropped = raw.crop(T0_GPS + 1.0, T0_GPS + 10.0)
    detrended = cropped.detrend("linear")
    if branch == "asd":
        return detrended.asd(fftlength=4.0)
    assert branch == "spectrogram"
    return detrended.spectrogram(stride=4.0, fftlength=2.0)


@dataclass(frozen=True)
class _CopiedScientificResult:
    """Renderer-facing snapshot made from the worker data-plane copy-out."""

    value: np.ndarray[Any, Any]
    coordinates: np.ndarray[Any, Any]
    unit: Any
    axes: Any
    object_id: str
    dtype: str
    shape: tuple[int, ...]
    produced_by: str | None
    frequencies: Any | None = None
    times: Any | None = None
    f0: Any | None = None
    df: Any | None = None
    t0: Any | None = None
    dt: Any | None = None
    name: str | None = None
    channel: str | None = None


def _axis_quantity(ref: DataObjectRef, name: str) -> Any:
    """Build one worker coordinate from the returned DataObjectRef metadata."""
    axis = ref.axes[name]
    return u.Quantity(axis["value"], axis["unit"])


def _quantity_snapshot(value: Any | None) -> tuple[np.ndarray[Any, Any], Any] | None:
    """Deep-copy a renderer Quantity, preserving the explicit None case."""
    if value is None:
        return None
    return np.array(value.value, copy=True), u.Unit(value.unit)


def _assert_quantity_snapshot(
    snapshot: tuple[np.ndarray[Any, Any], Any] | None, value: Any | None
) -> None:
    """Prove a renderer Quantity was not mutated, including absent axes."""
    if snapshot is None:
        assert value is None
        return
    assert value is not None
    assert u.Unit(value.unit) == snapshot[1]
    np.testing.assert_array_equal(np.asarray(value.value), snapshot[0])


def _plot_for_branch(branch: str, target_id: str) -> PlotSpec:
    """Declare a plot only after the target has materialized."""
    return PlotSpec(
        plot_id="plot-1",
        kind="line" if branch == "asd" else "spectrogram",
        object_ids=(target_id,),
        xscale="linear",
        yscale="log" if branch == "asd" else "linear",
        title=branch,
        xlabel="Frequency [Hz]" if branch == "asd" else "Time [s]",
        ylabel="ASD [m / sqrt(Hz)]" if branch == "asd" else "Frequency [Hz]",
        styles={"color": "black"} if branch == "asd" else {"cmap": "viridis"},
    )


def _assert_raw_coordinates(
    values: np.ndarray[Any, Any], coordinates: np.ndarray[Any, Any]
) -> None:
    """Enforce the shared-memory raw-coordinate descriptor contract."""
    assert coordinates.shape == (values.shape[0],)
    assert coordinates.dtype == np.dtype(np.int64)
    assert np.isfinite(coordinates).all()
    if coordinates.size > 1:
        assert np.all(np.diff(coordinates) > 0)
    np.testing.assert_array_equal(
        coordinates,
        np.arange(values.shape[0], dtype=np.int64),
    )


def _branch_project(source: Path, branch: str) -> Project:
    """Build graph declarations and materialized refs for one scientific branch."""
    target_id = "obj-4" if branch == "asd" else "obj-5"
    operations = (
        Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            params={"source": str(source), "format": "hdf5", "name": TEST_NAME},
            outputs=("obj-1",),
        ),
        Operation(
            op_id="op-2",
            operation_id="timeseries.crop",
            operation_schema=1,
            inputs={"self": "obj-1"},
            params={"start": T0_GPS + 1.0, "end": T0_GPS + 10.0},
            outputs=("obj-2",),
        ),
        Operation(
            op_id="op-3",
            operation_id="timeseries.detrend",
            operation_schema=1,
            inputs={"self": "obj-2"},
            params={"detrend": "linear"},
            outputs=("obj-3",),
        ),
        Operation(
            op_id="op-4" if branch == "asd" else "op-5",
            operation_id=f"timeseries.{branch}",
            operation_schema=1,
            inputs={"self": "obj-3"},
            params=(
                {"fftlength": {"value": 4.0, "unit": "s"}}
                if branch == "asd"
                else {
                    "stride": {"value": 4.0, "unit": "s"},
                    "fftlength": {"value": 2.0, "unit": "s"},
                }
            ),
            outputs=(target_id,),
        ),
        Operation(
            op_id="op-6",
            operation_id="timeseries.failed",
            operation_schema=1,
            inputs={"self": "obj-3"},
            params={},
            outputs=("obj-6",),
        ),
    )
    source_ref = DataSourceRef(
        source_id="src-1",
        uri=str(source),
        format="hdf5",
        size_bytes=source.stat().st_size,
        mtime=source.stat().st_mtime,
    )
    raw = DataObjectRef(
        object_id="obj-1",
        kind="TimeSeries",
        shape=(15360,),
        dtype="float64",
        unit="m",
        name=TEST_NAME,
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": T0_GPS, "unit": "s"},
            "dt": {"value": 1.0 / SAMPLE_RATE_HZ, "unit": "s"},
        },
        produced_by=None,
    )
    cropped = DataObjectRef(
        object_id="obj-2",
        kind="TimeSeries",
        shape=(2304,),
        dtype="float64",
        unit="m",
        name=TEST_NAME,
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": T0_GPS + 1.0, "unit": "s"},
            "dt": {"value": 1.0 / SAMPLE_RATE_HZ, "unit": "s"},
        },
        produced_by="op-2",
    )
    detrended = DataObjectRef(
        object_id="obj-3",
        kind="TimeSeries",
        shape=(2304,),
        dtype="float64",
        unit="m",
        name=TEST_NAME,
        channel="X1:STUDIO-CHANNEL",
        axes={
            "t0": {"value": T0_GPS + 1.0, "unit": "s"},
            "dt": {"value": 1.0 / SAMPLE_RATE_HZ, "unit": "s"},
        },
        produced_by="op-3",
    )
    # The branch output is deliberately *not* inserted here.  Its metadata and
    # values must be materialized by the worker replay, not copied from oracle.
    return Project(
        project_id=f"project-spike-4-{branch}",
        created="2026-08-16T00:00:00Z",
        modified="2026-08-16T00:00:00Z",
        compatibility={"studio": "0.1.0", "gwexpy": "0.1.14"},
        sources=(source_ref,),
        objects=(raw, cropped, detrended),
        graph=_GraphFixture(operations),
        executions=(),
        plots=(),
    )


def _downstream_operation() -> Operation:
    """Declare the downstream operation only after op-6 has failed."""
    return Operation(
        op_id="op-7",
        operation_id="timeseries.crop",
        operation_schema=1,
        inputs={"self": "obj-6"},
        params={"start": T0_GPS + 1.0, "end": T0_GPS + 2.0},
        outputs=("obj-7",),
    )


def _operation_snapshot(
    operations: tuple[Operation, ...],
) -> tuple[dict[str, Any], ...]:
    """Serialize every nested Operation field before replay mutates anything."""
    return tuple(
        json.loads(
            json.dumps(
                {
                    "op_id": operation.op_id,
                    "operation_id": operation.operation_id,
                    "operation_schema": operation.operation_schema,
                    "inputs": dict(operation.inputs),
                    "params": dict(operation.params),
                    "outputs": list(operation.outputs),
                },
                sort_keys=True,
            )
        )
        for operation in operations
    )


def _assert_exported_values(output: Path, reference: Any) -> None:
    """Compare fresh-process values only after finite and unit normalization checks."""
    dumped = np.asarray(np.load(output, allow_pickle=False))
    expected = np.asarray(reference.value)
    assert np.isfinite(dumped).all()
    assert np.isfinite(expected).all()
    assert dumped.shape == expected.shape
    unit_payload = json.loads(
        output.with_suffix(".unit.json").read_text(encoding="utf-8")
    )
    dumped_unit = u.Unit(unit_payload["unit"])
    reference_unit = u.Unit(reference.unit)
    assert dumped_unit == reference_unit
    dumped_in_reference_unit = (dumped * dumped_unit).to_value(reference_unit)
    tolerance = 1e-12 * max(1.0, float(np.max(np.abs(expected))))
    np.testing.assert_allclose(
        dumped_in_reference_unit,
        expected,
        rtol=1e-12,
        atol=tolerance,
    )


def _assert_spectral_invariants(reference: Any, branch: str) -> None:
    """Check finite axes, Nyquist/df, monotonicity, nonnegativity, and orientation."""
    values = np.asarray(reference.value)
    assert np.isfinite(values).all()
    frequencies = np.asarray(reference.frequencies.to_value("Hz"))
    assert np.isfinite(frequencies).all()
    assert np.all(np.diff(frequencies) > 0.0)
    assert frequencies[-1] == pytest.approx(SAMPLE_RATE_HZ / 2.0, abs=1e-12)
    expected_df = 0.25 if branch == "asd" else 0.5
    assert float(reference.df.to_value("Hz")) == pytest.approx(expected_df, abs=1e-12)
    assert (values >= 0.0).all()
    if branch == "asd":
        assert frequencies[int(np.argmax(values))] == pytest.approx(32.0, abs=1e-12)
    else:
        assert values.ndim == 2
        times = np.asarray(reference.times.to_value("s"))
        assert np.isfinite(times).all()
        assert np.all(np.diff(times) > 0.0)
        peak = np.unravel_index(np.argmax(values), values.shape)
        assert frequencies[peak[1]] == pytest.approx(32.0, abs=1e-12)
        assert values.T.shape == (frequencies.size, times.size)


def _assert_worker_spectral_invariants(
    result: _CopiedScientificResult,
    actual_ref: DataObjectRef,
    reference: Any,
    branch: str,
) -> None:
    """Check scientific invariants on worker bytes and worker-declared axes."""
    values = np.asarray(result.value)
    assert np.isfinite(values).all()
    assert values.shape == actual_ref.shape
    assert actual_ref.dtype == str(values.dtype)
    assert result.frequencies is not None
    assert result.df is not None
    frequencies_quantity = result.frequencies
    df_quantity = result.df
    frequencies = np.asarray(frequencies_quantity.to_value("Hz"))
    assert np.isfinite(frequencies).all()
    assert np.all(np.diff(frequencies) > 0.0)
    np.testing.assert_allclose(
        frequencies,
        np.asarray(reference.frequencies.to_value("Hz")),
        rtol=1e-12,
        atol=1e-12,
    )
    assert frequencies[-1] == pytest.approx(SAMPLE_RATE_HZ / 2.0, abs=1e-12)
    expected_df = 0.25 if branch == "asd" else 0.5
    assert float(df_quantity.to_value("Hz")) == pytest.approx(expected_df, abs=1e-12)
    assert (values >= 0.0).all()
    assert float(_axis_quantity(actual_ref, "f0").to_value("Hz")) == pytest.approx(
        float(reference.f0.to_value("Hz")), abs=1e-12
    )
    assert float(_axis_quantity(actual_ref, "df").to_value("Hz")) == pytest.approx(
        float(reference.df.to_value("Hz")), abs=1e-12
    )
    if branch == "asd":
        assert frequencies[int(np.argmax(values))] == pytest.approx(32.0, abs=1e-12)
    else:
        assert result.times is not None
        times = np.asarray(result.times.to_value("s"))
        assert np.isfinite(times).all()
        assert np.all(np.diff(times) > 0.0)
        np.testing.assert_allclose(
            times,
            np.asarray(reference.times.to_value("s")),
            rtol=1e-12,
            atol=1e-12,
        )
        assert values.ndim == 2
        peak = np.unravel_index(np.argmax(values), values.shape)
        assert frequencies[peak[1]] == pytest.approx(32.0, abs=1e-12)
        assert values.T.shape == (frequencies.size, times.size)
        assert float(_axis_quantity(actual_ref, "t0").to_value("s")) == pytest.approx(
            float(reference.t0.to_value("s")), abs=1e-12
        )
        assert float(_axis_quantity(actual_ref, "dt").to_value("s")) == pytest.approx(
            float(reference.dt.to_value("s")), abs=1e-12
        )


def _scientific_call_names(source: str) -> list[str]:
    """Read generated public scientific calls in source order."""

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: list[str] = []

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "read",
                "crop",
                "detrend",
                "asd",
                "spectrogram",
            }:
                self.names.append(node.func.attr)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(ast.parse(source))
    return visitor.names


def _object_ids_from_replay(value: Any) -> tuple[str, ...]:
    """Normalize the replay response for exact declared-ID assertions."""
    payload = value.get("payload", value) if isinstance(value, dict) else value
    if isinstance(payload, dict):
        ids = payload.get("object_ids", payload.get("object_id"))
        if isinstance(ids, str):
            return (ids,)
        if ids is not None:
            return tuple(ids)
    return tuple()


def _assert_plot_declaration(figure: Figure, plot_spec: PlotSpec, branch: str) -> None:
    """Ensure Agg rendering honors the renderer-independent PlotSpec declaration."""
    axis = figure.axes[0]
    assert axis.get_xscale() == plot_spec.xscale
    assert axis.get_yscale() == plot_spec.yscale
    assert axis.get_title() == plot_spec.title
    assert axis.get_xlabel() == plot_spec.xlabel
    assert axis.get_ylabel() == plot_spec.ylabel
    if branch == "asd":
        assert axis.lines[0].get_color() == plot_spec.styles["color"]
    else:
        assert axis.images[0].get_cmap().name == plot_spec.styles["cmap"]


def _copy_worker_result(
    client: WorkerClient,
    object_id: str,
    actual_ref: DataObjectRef,
    reference: Any,
    owned_shm_names: set[str],
) -> _CopiedScientificResult:
    """Copy actual worker bytes, acknowledge release, and close the attachment."""
    result = client.get_array(object_id, preview_stride=None)
    assert set(result) == {"descriptor", "unit"}
    descriptor = result["descriptor"]
    owned_shm_names.add(descriptor.name)
    assert descriptor.shape == actual_ref.shape
    assert descriptor.shape == tuple(reference.shape)
    attached = attach_block(
        descriptor,
        preview_stride=None,
    )
    try:
        copied_values = np.array(attached.values, copy=True)
        copied_coordinates = np.array(attached.coordinates, copy=True)
    finally:
        attached.handle.close()
    _assert_raw_coordinates(copied_values, copied_coordinates)
    release = client.request(
        {
            "protocol": 2,
            "request_id": (
                "00000000-0000-4000-8000-000000000601"
                if object_id == "obj-4"
                else "00000000-0000-4000-8000-000000000602"
            ),
            "type": "release_shm",
            "payload": {"name": descriptor.name},
        }
    )
    assert release["type"] == "result"
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=descriptor.name)
    assert copied_values.shape == actual_ref.shape
    assert str(copied_values.dtype) == actual_ref.dtype
    if copied_values.ndim == 1:
        f0 = _axis_quantity(actual_ref, "f0")
        df = _axis_quantity(actual_ref, "df")
        frequencies = f0 + np.arange(actual_ref.shape[-1]) * df
        times = None
        t0 = None
        dt = None
    else:
        f0 = _axis_quantity(actual_ref, "f0")
        df = _axis_quantity(actual_ref, "df")
        t0 = _axis_quantity(actual_ref, "t0")
        dt = _axis_quantity(actual_ref, "dt")
        frequencies = f0 + np.arange(actual_ref.shape[-1]) * df
        times = t0 + np.arange(actual_ref.shape[0]) * dt
        assert np.isfinite(np.asarray(times.to_value("s"))).all()
    return _CopiedScientificResult(
        value=copied_values,
        coordinates=copied_coordinates,
        unit=u.Unit(result["unit"]),
        axes=copy.deepcopy(actual_ref.axes),
        object_id=actual_ref.object_id,
        dtype=actual_ref.dtype,
        shape=actual_ref.shape,
        produced_by=actual_ref.produced_by,
        frequencies=frequencies,
        times=times,
        f0=f0,
        df=df,
        t0=t0,
        dt=dt,
        name=actual_ref.name,
        channel=actual_ref.channel,
    )


def _worker_client() -> WorkerClient:
    """Build the real spawn-backed worker used for materialization."""
    context = multiprocessing.get_context("spawn")
    return WorkerClient(
        context=context,
        process_factory=context.Process,
        connection_factory=context.Pipe,
        worker_target=worker_main,
        startup_timeout_s=60.0,
        request_timeout_s=30.0,
        join_timeout_s=5.0,
    )


def _assert_owned_shm_absent(names: set[str]) -> None:
    """Inspect only exact names reported by this test's worker responses."""
    for name in names:
        try:
            handle = shared_memory.SharedMemory(name=name)
        except FileNotFoundError:
            continue
        handle.close()
        raise AssertionError(f"owned shared-memory block survived cleanup: {name}")


def _fd_targets() -> dict[str, int]:
    """Snapshot parent fd target multiplicity for graceful cleanup."""
    if not sys.platform.startswith("linux"):
        return {}
    targets: dict[str, int] = {}
    for entry in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{entry}")
            targets[target] = targets.get(target, 0) + 1
        except FileNotFoundError:
            pass
    return targets


def _graceful_worker_close(client: WorkerClient, session: StudioSession) -> None:
    """Use Session.close before the bounded force backstop."""
    process = getattr(client, "_process", getattr(client, "process", None))
    connection = getattr(client, "_connection", getattr(client, "connection", None))
    assert process is not None
    assert connection is not None
    sentinel_fd = process.sentinel
    connection_fd = connection.fileno()
    session.close()
    assert client.state is WorkerLifecycle.CLOSED
    assert getattr(connection, "closed", False) is True
    with pytest.raises(OSError):
        os.fstat(connection_fd)
    assert bool(
        getattr(process, "closed", False) or getattr(process, "_closed", False)
    ), "Session.close must close the production process handle"
    with pytest.raises(OSError):
        os.fstat(sentinel_fd)


def _force_worker_cleanup(client: WorkerClient) -> None:
    """Backstop only: graceful session close is asserted by the caller first."""
    process = getattr(client, "_process", getattr(client, "process", None))
    if process is None:
        return
    if getattr(process, "_closed", False) or getattr(process, "closed", False):
        return
    if process.is_alive():
        process.kill()
    if getattr(process, "pid", None) is not None:
        process.join(timeout=3.0)
    assert not process.is_alive()
    process.close()
    connection = getattr(client, "_connection", getattr(client, "connection", None))
    close = getattr(connection, "close", None)
    if callable(close):
        close()


@pytest.mark.parametrize(
    "branch",
    [
        pytest.param("asd", id="asd", marks=pytest.mark.contract("I-S4-001")),
        pytest.param(
            "spectrogram",
            id="spectrogram",
            marks=pytest.mark.contract("I-S4-002"),
        ),
    ],
)
def test_scientific_export_execution_and_plot_match_public_oracle(
    branch: str,
    sine_32hz: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each named-HDF5 branch exports, executes freshly, and renders in Agg."""
    source = write_named_hdf5(sine_32hz, tmp_path / f"named-{branch}.h5")
    source_snapshot = (source.read_bytes(), source.stat().st_mtime_ns)
    original_values = np.array(sine_32hz.value, copy=True)
    original_metadata = {
        "unit": u.Unit(sine_32hz.unit),
        "t0": float(sine_32hz.t0.to_value("s")),
        "dt": float(sine_32hz.dt.to_value("s")),
        "name": str(sine_32hz.name),
        "channel": str(sine_32hz.channel),
    }
    original_axes = {
        "t0": float(sine_32hz.t0.to_value("s")),
        "dt": float(sine_32hz.dt.to_value("s")),
    }
    reference = _public_reference(source, branch)
    _assert_spectral_invariants(reference, branch)
    project = _branch_project(source, branch)
    graph_before = _operation_snapshot(project.graph.operations)
    target_id = "obj-4" if branch == "asd" else "obj-5"
    output = tmp_path / f"{branch}.npy"

    # This is the exact current export sentinel.  No successful branch object
    # is injected to make this call pass.
    invoke_preserving_sentinel(
        lambda: export_python(Project(), deterministic=True),
        owner="gwexpy_studio.export.python_exporter.export_python",
    )

    client = _worker_client()
    session = StudioSession(project=project, graph=project.graph, client=client)
    parent_fds = _fd_targets()
    owned_shm_names: set[str] = set()
    try:
        session.start()
        execute_messages: list[dict[str, Any]] = []
        original_request = client.request

        def recording_request(
            message: dict[str, Any], *, timeout_s: float | None = None
        ) -> Any:
            if message.get("type") == "execute":
                execute_messages.append(copy.deepcopy(message))
            return original_request(message, timeout_s=timeout_s)

        monkeypatch.setattr(client, "request", recording_request)
        replay_result = session.replay(targets=(target_id,))
        assert replay_result
        assert _object_ids_from_replay(replay_result) == (target_id,)
        expected_operation_ids = (
            "op-1",
            "op-2",
            "op-3",
            "op-4" if branch == "asd" else "op-5",
        )
        initial_records = project.executions
        assert tuple(record.op_id for record in initial_records) == (
            *expected_operation_ids,
        )
        assert all(record.status == "succeeded" for record in initial_records)
        materialized = {value.object_id: value for value in project.objects}
        expected_object_ids = ("obj-1", "obj-2", "obj-3", target_id)
        listed = client.request(
            {
                "protocol": 2,
                "request_id": "00000000-0000-4000-8000-000000000603",
                "type": "list_objects",
                "payload": {},
            }
        )
        assert listed["type"] == "result"
        assert tuple(listed["payload"]["object_ids"]) == expected_object_ids
        assert target_id in materialized
        assert (
            tuple(value.object_id for value in project.objects) == expected_object_ids
        )
        actual_ref = materialized[target_id]
        assert actual_ref.produced_by == ("op-4" if branch == "asd" else "op-5")
        assert "obj-6" not in materialized
        assert actual_ref.object_id == target_id
        assert actual_ref.kind == (
            "FrequencySeries" if branch == "asd" else "Spectrogram"
        )
        assert actual_ref.dtype == np.asarray(reference.value).dtype.name
        assert u.Unit(actual_ref.unit) == u.Unit(reference.unit)
        assert actual_ref.name == str(reference.name)
        assert actual_ref.channel == str(reference.channel)
        worker_result = _copy_worker_result(
            client, target_id, actual_ref, reference, owned_shm_names
        )
        assert np.isfinite(worker_result.value).all()
        assert u.Unit(worker_result.unit) == u.Unit(reference.unit)
        actual_in_reference_unit = (worker_result.value * worker_result.unit).to_value(
            u.Unit(reference.unit)
        )
        expected_values = np.asarray(reference.value)
        tolerance = 1e-12 * max(1.0, float(np.max(np.abs(expected_values))))
        np.testing.assert_allclose(
            actual_in_reference_unit,
            expected_values,
            rtol=1e-12,
            atol=tolerance,
        )
        _assert_worker_spectral_invariants(worker_result, actual_ref, reference, branch)
        if branch == "spectrogram":
            assert worker_result.times is not None
            actual_times = np.asarray(worker_result.times.to_value("s"))
            assert np.isfinite(actual_times).all()
            assert np.all(np.diff(actual_times) > 0.0)

        project.plots = (_plot_for_branch(branch, target_id),)
        renderer_snapshot = (
            worker_result.value.copy(),
            worker_result.coordinates.copy(),
            u.Unit(worker_result.unit),
            copy.deepcopy(worker_result.axes),
            worker_result.name,
            worker_result.channel,
            (
                None
                if worker_result.frequencies is None
                else worker_result.frequencies.copy()
            ),
            None if worker_result.times is None else worker_result.times.copy(),
            _quantity_snapshot(worker_result.f0),
            _quantity_snapshot(worker_result.df),
            _quantity_snapshot(worker_result.t0),
            _quantity_snapshot(worker_result.dt),
        )

        # Default replay/export must omit the failed, unmaterialized leaf;
        # explicit branch export must contain only its graph closure.
        default_count_before = len(project.executions)
        default_replay = session.replay()
        assert default_replay
        default_records = project.executions[default_count_before:]
        assert tuple(record.op_id for record in default_records) == (
            "op-1",
            "op-2",
            "op-3",
            "op-4" if branch == "asd" else "op-5",
        )
        assert all(record.op_id != "op-6" for record in default_records)
        execute_before_failed = copy.deepcopy(execute_messages)
        failed_count_before = len(project.executions)
        with pytest.raises(Exception) as failed:
            session.replay(targets=("obj-6",))
        assert getattr(failed.value, "code", None) == "operation_failed"
        failed_records = project.executions[failed_count_before:]
        assert tuple(record.op_id for record in failed_records) == ("op-6",)
        assert all(record.status == "failed" for record in failed_records)
        assert len(execute_messages) == len(execute_before_failed) + 1
        assert "op-6" in json.dumps(execute_messages[-1])
        assert all(
            value.object_id not in {"obj-6", "obj-7"} for value in project.objects
        )
        project.graph.add(_downstream_operation())
        graph_after_downstream = _operation_snapshot(project.graph.operations)
        assert graph_after_downstream == (
            *graph_before,
            {
                "inputs": {"self": "obj-6"},
                "op_id": "op-7",
                "operation_id": "timeseries.crop",
                "operation_schema": 1,
                "outputs": ["obj-7"],
                "params": {"end": T0_GPS + 2.0, "start": T0_GPS + 1.0},
            },
        )
        execute_before_downstream = copy.deepcopy(execute_messages)
        executions_before_downstream = copy.deepcopy(project.executions)
        downstream_count_before = len(project.executions)
        with pytest.raises(Exception) as downstream_failed:
            session.replay(targets=("obj-7",))
        assert getattr(downstream_failed.value, "code", None) == "object_not_found"
        assert len(project.executions) == downstream_count_before
        assert project.executions == executions_before_downstream
        assert execute_messages == execute_before_downstream
        assert sum(record.op_id == "op-6" for record in project.executions) == 1
        assert all("op-7" not in json.dumps(message) for message in execute_messages)
        assert all(
            value.object_id not in {"obj-6", "obj-7"} for value in project.objects
        )
        exported = export_python(
            project,
            targets=(target_id,),
            value_dumps={target_id: output},
            deterministic=True,
        )
        default_export = export_python(project, deterministic=True)
        assert ast.parse(exported) is not None
        assert "gwexpy_studio" not in exported
        assert "obj-6" not in default_export
        assert _scientific_call_names(exported) == ["read", "crop", "detrend", branch]
        assert _scientific_call_names(default_export) == [
            "read",
            "crop",
            "detrend",
            branch,
        ]
        assert source.as_posix() in exported
        assert "timeseries.failed" not in default_export
        assert _operation_snapshot(project.graph.operations) == graph_after_downstream

        script = tmp_path / f"generated-{branch}.py"
        script.write_text(exported, encoding="utf-8")
        environment = os.environ.copy()
        source_paths = [str(Path(__file__).resolve().parents[2] / "src")]
        if environment.get("PYTHONPATH"):
            source_paths.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(source_paths)
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=False,
            check=False,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr.decode(
            "utf-8", errors="replace"
        )
        assert output.is_file()
        assert output.with_suffix(".unit.json").is_file()
        _assert_exported_values(output, reference)
        fresh_values = np.asarray(np.load(output, allow_pickle=False))
        fresh_unit_payload = json.loads(
            output.with_suffix(".unit.json").read_text(encoding="utf-8")
        )
        fresh_unit = u.Unit(fresh_unit_payload["unit"])
        np.testing.assert_allclose(
            (fresh_values * fresh_unit).to_value(worker_result.unit),
            worker_result.value,
            rtol=1e-12,
            atol=tolerance,
        )

        plot_spec = project.plots[0]
        figure = render_plot(plot_spec, {target_id: worker_result})
        assert isinstance(figure, Figure)
        _assert_plot_declaration(figure, plot_spec, branch)
        axis = figure.axes[0]
        if branch == "asd":
            assert len(axis.lines) == 1
            rendered_x = np.asarray(axis.lines[0].get_xdata())
            rendered_y = np.asarray(axis.lines[0].get_ydata())
            assert worker_result.frequencies is not None
            expected_x = np.asarray(worker_result.frequencies.to_value("Hz"))
            np.testing.assert_allclose(rendered_x, expected_x, rtol=1e-12, atol=1e-12)
            np.testing.assert_allclose(
                rendered_y, worker_result.value, rtol=1e-12, atol=tolerance
            )
        else:
            assert len(axis.images) == 1
            image = axis.images[0]
            rendered_values = np.asarray(image.get_array())
            assert np.isfinite(rendered_values).all()
            np.testing.assert_allclose(
                rendered_values,
                worker_result.value.T,
                rtol=1e-12,
                atol=tolerance,
            )
            assert worker_result.times is not None
            assert worker_result.frequencies is not None
            assert worker_result.dt is not None
            assert worker_result.df is not None
            times = np.asarray(worker_result.times.to_value("s"))
            frequencies = np.asarray(worker_result.frequencies.to_value("Hz"))
            assert np.isfinite(times).all()
            assert np.all(np.diff(times) > 0.0)
            expected_extent = (
                times[0],
                times[-1] + float(worker_result.dt.to_value("s")),
                frequencies[0],
                frequencies[-1] + float(worker_result.df.to_value("Hz")),
            )
            np.testing.assert_allclose(
                image.get_extent(), expected_extent, rtol=1e-12, atol=1e-12
            )
            assert image.origin == "lower"
            assert image.get_cmap().name == "viridis"
        figure.savefig(tmp_path / f"{branch}.png")
        assert (tmp_path / f"{branch}.png").stat().st_size > 0

        # Actual worker metadata, source bytes, and source stat are immutable
        # renderer/export inputs.
        assert actual_ref.name == str(reference.name)
        assert actual_ref.channel == "X1:STUDIO-CHANNEL"
        assert np.array_equal(renderer_snapshot[0], worker_result.value)
        assert np.array_equal(renderer_snapshot[1], worker_result.coordinates)
        assert u.Unit(renderer_snapshot[2]) == u.Unit(worker_result.unit)
        assert renderer_snapshot[3] == worker_result.axes
        assert renderer_snapshot[4] == worker_result.name
        assert renderer_snapshot[5] == worker_result.channel
        if renderer_snapshot[6] is not None:
            assert worker_result.frequencies is not None
            assert np.array_equal(renderer_snapshot[6], worker_result.frequencies)
        if renderer_snapshot[7] is not None:
            assert worker_result.times is not None
            assert np.array_equal(renderer_snapshot[7], worker_result.times)
        _assert_quantity_snapshot(renderer_snapshot[8], worker_result.f0)
        _assert_quantity_snapshot(renderer_snapshot[9], worker_result.df)
        _assert_quantity_snapshot(renderer_snapshot[10], worker_result.t0)
        _assert_quantity_snapshot(renderer_snapshot[11], worker_result.dt)
        assert source.read_bytes() == source_snapshot[0]
        assert source.stat().st_mtime_ns == source_snapshot[1]
        _assert_owned_shm_absent(owned_shm_names)
        assert np.array_equal(sine_32hz.value, original_values)
        assert u.Unit(sine_32hz.unit) == original_metadata["unit"]
        assert float(sine_32hz.t0.to_value("s")) == original_metadata["t0"]
        assert float(sine_32hz.dt.to_value("s")) == original_metadata["dt"]
        assert str(sine_32hz.name) == original_metadata["name"]
        assert str(sine_32hz.channel) == original_metadata["channel"]
        assert float(sine_32hz.t0.to_value("s")) == original_axes["t0"]
        assert float(sine_32hz.dt.to_value("s")) == original_axes["dt"]
    finally:
        if client.state is WorkerLifecycle.RUNNING:
            _graceful_worker_close(client, session)
        _force_worker_cleanup(client)
        if parent_fds:
            current_fds = _fd_targets()
            assert all(
                current_fds.get(target, 0) <= count
                for target, count in parent_fds.items()
            )
            assert set(current_fds) <= set(parent_fds)


@pytest.mark.contract("I-S4-003")
def test_scientific_oracle_does_not_reuse_studio_execution_layers() -> None:
    """The Spike 4 oracle is a direct gwexpy public-API implementation."""
    source = inspect.getsource(_public_reference)
    tree = ast.parse(source)
    assert "gwexpy_studio" not in source
    assert "REGISTRY" not in source
    assert "normalize_params" not in source
    assert "execute_operation" not in source
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read", "crop", "detrend", "asd", "spectrogram"}
    }
    assert calls == {"read", "crop", "detrend", "asd", "spectrogram"}
