"""Full headless workflow contracts, intentionally blocked at session startup."""

from __future__ import annotations

import ast
import copy
import json
import multiprocessing
import os
import subprocess
import sys
import time
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
from gwexpy_studio.errors import WorkerCrashedError
from gwexpy_studio.export.python_exporter import export_python
from gwexpy_studio.persistence.project_io import load_project, save_project
from gwexpy_studio.plotting.renderer import render_plot
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient, WorkerLifecycle
from gwexpy_studio.worker.service import worker_main
from gwexpy_studio.worker.shm import attach_block
from tests.support.fixtures import SAMPLE_RATE_HZ, T0_GPS, TEST_NAME, write_named_hdf5
from tests.support.sentinel import invoke_preserving_sentinel

pytestmark = pytest.mark.e2e


class _DeclaredGraph:
    """Pre-populated graph double so the E2E sentinel remains session.start."""

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
        """Declare downstream work only after its input failure is recorded."""
        self._operations = (*self._operations, operation)


def _oracle(source: Path, branch: str) -> Any:
    """Compute E2E expected data with direct gwexpy calls only."""
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


def _project(source: Path, branch: str) -> tuple[Project, tuple[Operation, ...]]:
    """Declare read, crop, detrend, one branch, and a failed output."""
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
            operation_id="timeseries.unknown",
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
    common_axes = {
        "t0": {"value": T0_GPS, "unit": "s"},
        "dt": {"value": 1.0 / SAMPLE_RATE_HZ, "unit": "s"},
    }
    objects = (
        DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(15360,),
            dtype="float64",
            unit="m",
            name=TEST_NAME,
            channel="X1:STUDIO-CHANNEL",
            axes=common_axes,
            produced_by=None,
        ),
        DataObjectRef(
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
        ),
        DataObjectRef(
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
        ),
        # The branch output is intentionally absent until worker replay
        # materializes the declared object ID.
    )
    graph = _DeclaredGraph(operations)
    return Project(
        project_id=f"project-e2e-{branch}",
        created="2026-08-16T00:00:00Z",
        modified="2026-08-16T00:00:00Z",
        compatibility={"studio": "0.1.0", "gwexpy": "0.1.14"},
        sources=(source_ref,),
        objects=objects,
        graph=graph,
        executions=(),
        plots=(),
        ui_state={"selected_object": target_id},
    ), operations


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


def _plot_for_branch(branch: str, target_id: str) -> PlotSpec:
    """Create a plot declaration only after its target is materialized."""
    return PlotSpec(
        plot_id="plot-1",
        kind="line" if branch == "asd" else "spectrogram",
        object_ids=(target_id,),
        xscale="linear",
        yscale="log" if branch == "asd" else "linear",
        title=f"E2E {branch}",
        xlabel="Frequency [Hz]" if branch == "asd" else "Time [s]",
        ylabel="ASD [m / sqrt(Hz)]" if branch == "asd" else "Frequency [Hz]",
        styles={"color": "black"} if branch == "asd" else {"cmap": "viridis"},
    )


def _client() -> WorkerClient:
    """Use an explicit spawn context and bounded lifecycle deadlines."""
    context = multiprocessing.get_context("spawn")
    return WorkerClient(
        context=context,
        process_factory=context.Process,
        connection_factory=context.Pipe,
        worker_target=worker_main,
        startup_timeout_s=60.0,
        request_timeout_s=10.0,
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
    """Snapshot parent file targets while preserving descriptor multiplicity."""
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


def _pid_is_live(pid: int) -> bool:
    """Return True only for a real, non-zombie Linux process (no proxy involved).

    Deliberately reads /proc instead of calling Process.is_alive(): the
    WorkerClient's _SafeProcessProxy reports is_alive() == False for a
    still-running child once close() has been called, so an assertion built
    on is_alive() proves nothing about real OS-level termination.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    # comm (field 2) may itself contain spaces/parens; split after the last ")".
    fields = raw.rpartition(b")")[2].split()
    return bool(fields) and fields[0] != b"Z"


def _assert_pids_terminated(pids: set[int], *, timeout_s: float = 5.0) -> None:
    """Assert every observed worker pid is gone at the OS level, not via is_alive()."""
    deadline = time.monotonic() + timeout_s
    survivors = {pid for pid in pids if _pid_is_live(pid)}
    while survivors and time.monotonic() < deadline:
        time.sleep(0.05)
        survivors = {pid for pid in survivors if _pid_is_live(pid)}
    assert not survivors, (
        f"worker pids survived cleanup at OS level: {sorted(survivors)}"
    )


_SHM_DIR = Path("/dev/shm")


def _studio_shm_residue() -> frozenset[str]:
    """Snapshot every /dev/shm entry as a residue baseline.

    There is no descriptor-carried sidecar file anymore (the transport now
    uses ``SharedMemoryDescriptor`` as the sole source of truth — see
    ``gwexpy_studio.worker.shm``), so there is no project-specific filename
    prefix left to scope this snapshot to. This intentionally trades the old
    "only entries this project creates" precision for catching any real
    leaked shared-memory segment that ``_assert_owned_shm_absent`` might miss
    (for example one whose name was never recorded into ``owned_shm_names``
    due to a bookkeeping bug in this test). A concurrent, unrelated /dev/shm
    tenant active during the same test window could in principle cause a
    false positive here; ``_assert_owned_shm_absent`` remains the precise,
    exact-name check this one supplements rather than replaces.
    """
    if not _SHM_DIR.is_dir():
        return frozenset()
    return frozenset(entry.name for entry in _SHM_DIR.iterdir())


def _assert_no_studio_shm_residue(baseline: frozenset[str]) -> None:
    """No shared-memory segment created during the run may outlive it."""
    leaked = _studio_shm_residue() - baseline
    assert not leaked, f"/dev/shm residue survived cleanup: {sorted(leaked)}"


@dataclass(frozen=True)
class _WorkerResult:
    """Actual worker copy-out plus metadata and coordinates from its ref."""

    value: np.ndarray[Any, Any]
    coordinates: np.ndarray[Any, Any]
    unit: Any
    axes: Any
    object_id: str
    dtype: str
    shape: tuple[int, ...]
    produced_by: str | None
    frequencies: Any
    times: Any | None
    f0: Any
    df: Any
    t0: Any | None
    dt: Any | None
    name: str | None
    channel: str | None


def _axis_quantity(ref: DataObjectRef, name: str) -> Any:
    """Build a coordinate from worker-returned DataObjectRef.axes."""
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


def _assert_worker_science(
    result: _WorkerResult,
    reference: Any,
    branch: str,
) -> None:
    """Validate finite worker values and worker-derived spectral coordinates."""
    values = np.asarray(result.value)
    assert np.isfinite(values).all()
    assert values.ndim in {1, 2}
    assert u.Unit(result.unit) == u.Unit(reference.unit)
    assert result.frequencies is not None
    assert result.df is not None
    frequencies = np.asarray(result.frequencies.to_value("Hz"))
    assert np.isfinite(frequencies).all()
    assert np.all(np.diff(frequencies) > 0.0)
    np.testing.assert_allclose(
        frequencies,
        np.asarray(reference.frequencies.to_value("Hz")),
        rtol=1e-12,
        atol=1e-12,
    )
    assert frequencies[-1] == pytest.approx(SAMPLE_RATE_HZ / 2.0, abs=1e-12)
    assert float(result.df.to_value("Hz")) == pytest.approx(
        0.25 if branch == "asd" else 0.5, abs=1e-12
    )
    assert float(result.f0.to_value("Hz")) == pytest.approx(
        float(reference.f0.to_value("Hz")), abs=1e-12
    )
    assert float(result.df.to_value("Hz")) == pytest.approx(
        float(reference.df.to_value("Hz")), abs=1e-12
    )
    assert (values >= 0.0).all()
    if branch == "asd":
        assert frequencies[int(np.argmax(values))] == pytest.approx(32.0, abs=1e-12)
    else:
        assert result.times is not None
        assert result.t0 is not None
        assert result.dt is not None
        times = np.asarray(result.times.to_value("s"))
        assert np.isfinite(times).all()
        assert np.all(np.diff(times) > 0.0)
        np.testing.assert_allclose(
            times,
            np.asarray(reference.times.to_value("s")),
            rtol=1e-12,
            atol=1e-12,
        )
        assert float(result.t0.to_value("s")) == pytest.approx(
            float(reference.t0.to_value("s")), abs=1e-12
        )
        assert float(result.dt.to_value("s")) == pytest.approx(
            float(reference.dt.to_value("s")), abs=1e-12
        )
        peak = np.unravel_index(np.argmax(values), values.shape)
        assert frequencies[peak[1]] == pytest.approx(32.0, abs=1e-12)
        assert values.T.shape == (frequencies.size, times.size)


def _copy_worker_result(
    client: WorkerClient,
    object_id: str,
    actual_ref: DataObjectRef,
    reference: Any,
    request_suffix: str,
    owned_shm_names: set[str],
) -> _WorkerResult:
    """Copy full worker output, close attachment, and acknowledge release_shm."""
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
        values = np.array(attached.values, copy=True)
        coordinates = np.array(attached.coordinates, copy=True)
    finally:
        attached.handle.close()
    _assert_raw_coordinates(values, coordinates)
    response = client.request(
        {
            "protocol": 2,
            "request_id": f"00000000-0000-4000-8000-{request_suffix}",
            "type": "release_shm",
            "payload": {"name": descriptor.name},
        }
    )
    assert response["type"] == "result"
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=descriptor.name)
    assert str(values.dtype) == actual_ref.dtype
    f0 = _axis_quantity(actual_ref, "f0")
    df = _axis_quantity(actual_ref, "df")
    frequencies = f0 + np.arange(actual_ref.shape[-1]) * df
    if values.ndim == 1:
        times = None
        t0 = None
        dt = None
    else:
        t0 = _axis_quantity(actual_ref, "t0")
        dt = _axis_quantity(actual_ref, "dt")
        times = t0 + np.arange(actual_ref.shape[0]) * dt
        assert np.isfinite(np.asarray(times.to_value("s"))).all()
    return _WorkerResult(
        value=values,
        coordinates=coordinates,
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


def _object_snapshot(project: Project) -> tuple[DataObjectRef, ...]:
    """Capture exact persisted metadata, including axes and provenance."""
    return copy.deepcopy(tuple(project.objects))


def _object_ids_from_replay(value: Any) -> tuple[str, ...]:
    """Normalize the accepted replay response for exact declared-ID checks."""
    payload = value.get("payload", value) if isinstance(value, dict) else value
    if isinstance(payload, dict):
        ids = payload.get("object_ids", payload.get("object_id"))
        if isinstance(ids, str):
            return (ids,)
        if ids is not None:
            return tuple(ids)
    return tuple()


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


def _assert_worker_object_ids(
    client: WorkerClient,
    request_id: str,
    expected_ids: tuple[str, ...],
) -> None:
    """Compare the worker store's exact IDs with Project.objects after replay."""
    response = client.request(
        {
            "protocol": 2,
            "request_id": request_id,
            "type": "list_objects",
            "payload": {},
        }
    )
    assert response["type"] == "result"
    assert tuple(response["payload"]["object_ids"]) == expected_ids


def _safe_pid(process: Any) -> int | None:
    """Read .pid defensively: a closed real Process raises ValueError, not
    AttributeError, so plain getattr(..., default) does not shield callers."""
    try:
        return process.pid
    except (ValueError, AttributeError):
        return None


def _cleanup(
    client: WorkerClient,
    session: StudioSession | None = None,
    observed_worker_pids: set[int] | None = None,
) -> None:
    """Gracefully close first; force kill/join/close is only the backstop."""
    process = getattr(client, "_process", None)
    if process is None:
        process = getattr(client, "process", None)
    connection = getattr(client, "_connection", None)
    if connection is None:
        connection = getattr(client, "connection", None)
    # Record the OS pid *before* any close() call below: multiprocessing's
    # real Process.close() drops its internal _popen handle, after which
    # .pid reads back as None, so this is the last point it is observable.
    if observed_worker_pids is not None:
        early_pid = _safe_pid(process) if process is not None else None
        if early_pid is not None:
            observed_worker_pids.add(early_pid)
    sentinel_fd = getattr(process, "sentinel", None)
    connection_fd = (
        connection.fileno()
        if connection is not None and hasattr(connection, "fileno")
        else None
    )
    if session is not None and client.state is WorkerLifecycle.RUNNING:
        assert process is not None
        assert connection is not None
        session.close()
        assert client.state is WorkerLifecycle.CLOSED
        assert getattr(connection, "closed", False) is True
        if connection_fd is not None:
            with pytest.raises(OSError):
                os.fstat(connection_fd)
        assert bool(
            getattr(process, "closed", False) or getattr(process, "_closed", False)
        ), "Session.close must close the production process handle"
    if process is None:
        return
    if getattr(process, "_closed", False) or getattr(process, "closed", False):
        if sentinel_fd is not None:
            with pytest.raises(OSError):
                os.fstat(sentinel_fd)
        return
    process_pid = getattr(process, "pid", None)
    if process.is_alive():
        process.kill()
    if process_pid is not None:
        process.join(timeout=3.0)
    assert not process.is_alive(), "worker child survived cleanup watchdog"
    close = getattr(process, "close", None)
    if callable(close):
        close()
    if sentinel_fd is not None:
        with pytest.raises(OSError):
            os.fstat(sentinel_fd)
    close_connection = getattr(connection, "close", None)
    if callable(close_connection):
        close_connection()


def _assert_finite_unit_values(output: Path, reference: Any) -> None:
    """Apply finite-first, unit-equal, scale-aware scientific comparison."""
    actual = np.asarray(np.load(output, allow_pickle=False))
    expected = np.asarray(reference.value)
    assert np.isfinite(actual).all()
    assert np.isfinite(expected).all()
    assert actual.shape == expected.shape
    unit_payload = json.loads(
        output.with_suffix(".unit.json").read_text(encoding="utf-8")
    )
    actual_unit = u.Unit(unit_payload["unit"])
    expected_unit = u.Unit(reference.unit)
    assert actual_unit == expected_unit
    converted = (actual * actual_unit).to_value(expected_unit)
    tolerance = 1e-12 * max(1.0, float(np.max(np.abs(expected))))
    np.testing.assert_allclose(converted, expected, rtol=1e-12, atol=tolerance)


@pytest.mark.parametrize(
    "branch",
    [
        pytest.param("asd", id="asd", marks=pytest.mark.contract("I-E2E-001")),
        pytest.param(
            "spectrogram",
            id="spectrogram",
            marks=pytest.mark.contract("I-E2E-002"),
        ),
    ],
)
def test_headless_pipeline_replays_without_graph_mutation_and_records_failures(
    branch: str,
    sine_32hz: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run read→analysis→export→replay→plot with stable graph provenance."""
    source = write_named_hdf5(sine_32hz, tmp_path / f"e2e-{branch}.h5")
    reference = _oracle(source, branch)
    project, _operations = _project(source, branch)
    target_id = "obj-4" if branch == "asd" else "obj-5"
    client = _client()
    session = StudioSession(project=project, graph=project.graph, client=client)
    loaded_client: WorkerClient | None = None
    loaded_session: StudioSession | None = None
    source_snapshot = (source.read_bytes(), source.stat().st_mtime_ns)
    owned_shm_names: set[str] = set()
    observed_worker_pids: set[int] = set()
    baseline_children = frozenset(
        child.pid
        for child in multiprocessing.active_children()
        if child.pid is not None
    )
    baseline_fds = _fd_targets()
    shm_baseline = _studio_shm_residue()
    try:
        invoke_preserving_sentinel(
            session.start,
            owner="gwexpy_studio.session.StudioSession.start",
        )

        graph_before = _operation_snapshot(project.graph.operations)
        execute_messages: list[dict[str, Any]] = []
        original_request = client.request

        def recording_request(
            message: dict[str, Any], *, timeout_s: float | None = None
        ) -> Any:
            if message.get("type") == "execute":
                execute_messages.append(copy.deepcopy(message))
            return original_request(message, timeout_s=timeout_s)

        monkeypatch.setattr(client, "request", recording_request)
        initial_execution_count = len(project.executions)
        initial_replay = session.replay(targets=(target_id,))
        assert initial_replay
        assert _object_ids_from_replay(initial_replay) == (target_id,)
        expected_object_ids = ("obj-1", "obj-2", "obj-3", target_id)
        _assert_worker_object_ids(
            client,
            "00000000-0000-4000-8000-000000000504",
            expected_object_ids,
        )
        initial_records = project.executions[initial_execution_count:]
        expected_operation_ids = (
            "op-1",
            "op-2",
            "op-3",
            "op-4" if branch == "asd" else "op-5",
        )
        assert tuple(record.op_id for record in initial_records) == (
            *expected_operation_ids,
        )
        assert all(record.status == "succeeded" for record in initial_records)
        assert _operation_snapshot(project.graph.operations) == graph_before

        initial_objects = _object_snapshot(project)
        target_ref = next(
            value for value in initial_objects if value.object_id == target_id
        )
        assert target_ref.object_id == target_id
        assert target_ref.kind == (
            "FrequencySeries" if branch == "asd" else "Spectrogram"
        )
        assert target_ref.dtype == np.asarray(reference.value).dtype.name
        assert u.Unit(target_ref.unit) == u.Unit(reference.unit)
        assert target_ref.name == str(reference.name)
        assert target_ref.channel == str(reference.channel)
        initial_result = _copy_worker_result(
            client,
            target_id,
            target_ref,
            reference,
            "000000000501",
            owned_shm_names,
        )
        initial_values = initial_result.value
        initial_unit = initial_result.unit
        initial_coordinates = initial_result.coordinates
        assert np.isfinite(initial_values).all()
        assert initial_unit == u.Unit(reference.unit)
        assert initial_values.shape == tuple(reference.shape)
        assert initial_coordinates.shape == (initial_values.shape[0],)
        tolerance = 1e-12 * max(1.0, float(np.max(np.abs(np.asarray(reference.value)))))
        np.testing.assert_allclose(
            (initial_values * initial_unit).to_value(u.Unit(reference.unit)),
            np.asarray(reference.value),
            rtol=1e-12,
            atol=tolerance,
        )
        _assert_worker_science(initial_result, reference, branch)
        assert target_ref.produced_by == ("op-4" if branch == "asd" else "op-5")
        assert all(value.object_id != "obj-6" for value in initial_objects)
        assert (
            tuple(value.object_id for value in initial_objects) == expected_object_ids
        )
        assert np.isfinite(np.asarray(initial_result.frequencies.to_value("Hz"))).all()
        assert np.all(np.diff(initial_result.frequencies.to_value("Hz")) > 0.0)
        assert initial_result.frequencies[-1].to_value("Hz") == pytest.approx(
            SAMPLE_RATE_HZ / 2.0, abs=1e-12
        )
        assert float(initial_result.df.to_value("Hz")) == pytest.approx(
            0.25 if branch == "asd" else 0.5, abs=1e-12
        )
        assert (initial_values >= 0.0).all()
        if branch == "asd":
            assert initial_result.frequencies[int(np.argmax(initial_values))].to_value(
                "Hz"
            ) == pytest.approx(32.0, abs=1e-12)
        if branch == "spectrogram":
            assert initial_result.times is not None
            times = np.asarray(initial_result.times.to_value("s"))
            assert np.isfinite(times).all()
            assert np.all(np.diff(times) > 0.0)
            peak = np.unravel_index(np.argmax(initial_values), initial_values.shape)
            assert initial_result.frequencies[peak[1]].to_value("Hz") == pytest.approx(
                32.0, abs=1e-12
            )
            assert initial_values.T.shape == (
                initial_result.frequencies.size,
                initial_result.times.size,
            )
        project.plots = (_plot_for_branch(branch, target_id),)

        output = tmp_path / f"e2e-{branch}.npy"
        exported = export_python(
            project,
            targets=(target_id,),
            value_dumps={target_id: output},
            deterministic=True,
        )
        default_export = export_python(project, deterministic=True)
        assert "gwexpy_studio" not in exported
        assert "obj-6" not in default_export
        assert "timeseries.unknown" not in default_export
        expected_export_calls = ["read", "crop", "detrend", branch]
        assert _scientific_call_names(exported) == expected_export_calls
        assert _scientific_call_names(default_export) == expected_export_calls
        default_constants = {
            value.value
            for value in ast.walk(ast.parse(default_export))
            if isinstance(value, ast.Constant)
        }
        assert "obj-6" not in default_constants
        script = tmp_path / f"e2e-generated-{branch}.py"
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
        _assert_finite_unit_values(output, reference)
        fresh_values = np.asarray(np.load(output, allow_pickle=False))
        fresh_unit_payload = json.loads(
            output.with_suffix(".unit.json").read_text(encoding="utf-8")
        )
        fresh_unit = u.Unit(fresh_unit_payload["unit"])
        np.testing.assert_allclose(
            (fresh_values * fresh_unit).to_value(initial_unit),
            initial_values,
            rtol=1e-12,
            atol=tolerance,
        )

        project_path = tmp_path / f"e2e-{branch}.gwxproj"
        save_project(project, project_path, clock=lambda: "2026-08-16T02:00:00Z")
        loaded = load_project(project_path)
        assert _operation_snapshot(loaded.graph.operations) == graph_before
        assert loaded.project_id == project.project_id
        assert loaded.sources[0].mtime == project.sources[0].mtime
        assert loaded.executions == project.executions
        assert _object_snapshot(loaded) == initial_objects

        process = getattr(client, "_process", getattr(client, "process", None))
        connection = getattr(client, "_connection", getattr(client, "connection", None))
        assert process is not None
        assert connection is not None
        sentinel_fd = process.sentinel
        connection_fd = connection.fileno()
        # Capture this pid before kill()/close() drop it: client.restart()
        # below replaces client._process with a fresh handle, so this
        # manually-killed process would otherwise never reach _cleanup.
        killed_pid = _safe_pid(process)
        if killed_pid is not None:
            observed_worker_pids.add(killed_pid)
        process.kill()
        process.join(timeout=5.0)
        assert not process.is_alive()
        with pytest.raises(WorkerCrashedError):
            client.request(
                {
                    "protocol": 2,
                    "request_id": "00000000-0000-4000-8000-000000000499",
                    "type": "list_objects",
                    "payload": {},
                },
                timeout_s=2.0,
            )
        assert getattr(connection, "closed", False) is True
        with pytest.raises(OSError):
            os.fstat(connection_fd)
        assert bool(
            getattr(process, "closed", False) or getattr(process, "_closed", False)
        ), "crash handling must close the production process handle"
        with pytest.raises(OSError):
            os.fstat(sentinel_fd)
        client.restart()
        assert client.state is WorkerLifecycle.RUNNING
        replay_count_before = len(project.executions)
        replay_result = session.replay(targets=(target_id,))
        assert replay_result
        assert _object_ids_from_replay(replay_result) == (target_id,)
        _assert_worker_object_ids(
            client,
            "00000000-0000-4000-8000-000000000505",
            expected_object_ids,
        )
        restart_ref = next(
            value for value in project.objects if value.object_id == target_id
        )
        restart_result = _copy_worker_result(
            client,
            target_id,
            restart_ref,
            reference,
            "000000000502",
            owned_shm_names,
        )
        restart_values = restart_result.value
        restart_unit = restart_result.unit
        restart_coordinates = restart_result.coordinates
        _assert_worker_science(restart_result, reference, branch)
        np.testing.assert_allclose(
            (restart_values * restart_unit).to_value(initial_unit),
            initial_values,
            rtol=1e-12,
            atol=tolerance,
        )
        assert np.array_equal(restart_coordinates, initial_coordinates)
        assert restart_unit == initial_unit
        assert restart_result.object_id == initial_result.object_id == target_id
        assert restart_result.axes == initial_result.axes
        assert restart_result.name == initial_result.name
        assert restart_result.channel == initial_result.channel
        assert restart_result.produced_by == initial_result.produced_by
        assert _object_snapshot(project) == initial_objects
        assert _operation_snapshot(project.graph.operations) == graph_before
        replay_records = project.executions[replay_count_before:]
        assert tuple(record.op_id for record in replay_records) == (
            *expected_operation_ids,
        )
        assert project.graph.operations[3].outputs == (target_id,)

        failed_count_before = len(project.executions)
        with pytest.raises(Exception) as failed:
            session.replay(targets=("obj-6",))
        assert getattr(failed.value, "code", None) == "operation_failed"
        failed_records = project.executions[failed_count_before:]
        assert failed_records
        assert any(
            record.op_id == "op-6" and record.status == "failed"
            for record in failed_records
        )
        assert all(value.object_id != "obj-6" for value in project.objects)
        execute_before_downstream = copy.deepcopy(execute_messages)
        executions_before_downstream = copy.deepcopy(project.executions)
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

        # Persist after the real failure record exists, then use this fresh
        # loaded project for the new-session replay below.
        save_project(project, project_path, clock=lambda: "2026-08-16T02:01:00Z")
        loaded = load_project(project_path)
        assert _operation_snapshot(loaded.graph.operations) == graph_after_downstream
        assert loaded.executions == project.executions
        assert loaded.plots == project.plots
        assert _operation_snapshot(project.graph.operations) == graph_after_downstream

        loaded_client = _client()
        loaded_session = StudioSession(
            project=loaded,
            graph=loaded.graph,
            client=loaded_client,
        )
        loaded_session.start()
        loaded_replay_count = len(loaded.executions)
        loaded_replay = loaded_session.replay(targets=(target_id,))
        assert loaded_replay
        assert _object_ids_from_replay(loaded_replay) == (target_id,)
        _assert_worker_object_ids(
            loaded_client,
            "00000000-0000-4000-8000-000000000506",
            expected_object_ids,
        )
        loaded_target_ref = next(
            value for value in loaded.objects if value.object_id == target_id
        )
        loaded_result = _copy_worker_result(
            loaded_client,
            target_id,
            loaded_target_ref,
            reference,
            "000000000503",
            owned_shm_names,
        )
        loaded_values = loaded_result.value
        loaded_unit = loaded_result.unit
        loaded_coordinates = loaded_result.coordinates
        _assert_worker_science(loaded_result, reference, branch)
        np.testing.assert_allclose(
            (loaded_values * loaded_unit).to_value(initial_unit),
            initial_values,
            rtol=1e-12,
            atol=tolerance,
        )
        assert np.array_equal(loaded_coordinates, initial_coordinates)
        assert loaded_unit == initial_unit
        assert loaded_result.object_id == initial_result.object_id == target_id
        assert loaded_result.axes == initial_result.axes
        assert loaded_result.name == initial_result.name
        assert loaded_result.channel == initial_result.channel
        assert loaded_result.produced_by == initial_result.produced_by
        assert tuple(
            record.op_id for record in loaded.executions[loaded_replay_count:]
        ) == (*expected_operation_ids,)
        assert all(
            record.status == "succeeded"
            for record in loaded.executions[loaded_replay_count:]
        )
        assert _object_snapshot(loaded) == initial_objects
        assert _operation_snapshot(loaded.graph.operations) == graph_after_downstream

        loaded_default_count = len(loaded.executions)
        loaded_default_replay = loaded_session.replay()
        assert loaded_default_replay
        assert _object_ids_from_replay(loaded_default_replay) == (target_id,)
        loaded_default_records = loaded.executions[loaded_default_count:]
        assert tuple(record.op_id for record in loaded_default_records) == (
            *expected_operation_ids,
        )
        assert all(record.status == "succeeded" for record in loaded_default_records)
        assert all(record.op_id != "op-6" for record in loaded_default_records)
        assert _operation_snapshot(loaded.graph.operations) == graph_after_downstream

        default_count_before = len(project.executions)
        default_replay = session.replay()
        assert default_replay
        assert _object_ids_from_replay(default_replay) == (target_id,)
        _assert_worker_object_ids(
            client,
            "00000000-0000-4000-8000-000000000507",
            expected_object_ids,
        )
        default_records = project.executions[default_count_before:]
        assert tuple(record.op_id for record in default_records) == (
            *expected_operation_ids,
        )
        assert all(record.status == "succeeded" for record in default_records)
        assert all(record.op_id != "op-6" for record in default_records)
        assert _operation_snapshot(project.graph.operations) == graph_after_downstream

        renderer_snapshot = (
            initial_values.copy(),
            initial_result.coordinates.copy(),
            u.Unit(initial_unit),
            copy.deepcopy(initial_result.axes),
            initial_result.name,
            initial_result.channel,
            initial_result.frequencies.copy(),
            None if initial_result.times is None else initial_result.times.copy(),
            _quantity_snapshot(initial_result.f0),
            _quantity_snapshot(initial_result.df),
            _quantity_snapshot(initial_result.t0),
            _quantity_snapshot(initial_result.dt),
        )
        figure = render_plot(
            project.plots[0],
            {target_id: initial_result},
        )
        assert isinstance(figure, Figure)
        axis = figure.axes[0]
        plot_spec = project.plots[0]
        assert axis.get_xscale() == plot_spec.xscale
        assert axis.get_yscale() == plot_spec.yscale
        assert axis.get_title() == plot_spec.title
        assert axis.get_xlabel() == plot_spec.xlabel
        assert axis.get_ylabel() == plot_spec.ylabel
        if branch == "asd":
            assert len(axis.lines) == 1
            assert axis.lines[0].get_color() == plot_spec.styles["color"]
        else:
            assert len(axis.images) == 1
            image = axis.images[0]
            assert image.get_cmap().name == plot_spec.styles["cmap"]
            assert image.origin == "lower"
            assert initial_result.times is not None
            assert np.isfinite(np.asarray(initial_result.times.to_value("s"))).all()
            np.testing.assert_allclose(
                np.asarray(image.get_array()),
                initial_values.T,
                rtol=1e-12,
                atol=tolerance,
            )
            times = np.asarray(initial_result.times.to_value("s"))
            frequencies = np.asarray(initial_result.frequencies.to_value("Hz"))
            assert np.all(np.diff(times) > 0.0)
            assert initial_result.dt is not None
            assert initial_result.df is not None
            np.testing.assert_allclose(
                image.get_extent(),
                np.asarray(
                    (
                        times[0],
                        times[-1] + float(initial_result.dt.to_value("s")),
                        frequencies[0],
                        frequencies[-1] + float(initial_result.df.to_value("Hz")),
                    )
                ),
                rtol=1e-12,
                atol=1e-12,
            )
        if branch == "asd":
            np.testing.assert_allclose(
                np.asarray(axis.lines[0].get_xdata()),
                np.asarray(initial_result.frequencies.to_value("Hz")),
                rtol=1e-12,
                atol=1e-12,
            )
            np.testing.assert_allclose(
                np.asarray(axis.lines[0].get_ydata()),
                initial_values,
                rtol=1e-12,
                atol=tolerance,
            )
        assert np.array_equal(renderer_snapshot[0], initial_values)
        assert np.array_equal(renderer_snapshot[1], initial_result.coordinates)
        assert u.Unit(renderer_snapshot[2]) == initial_unit
        assert renderer_snapshot[3] == initial_result.axes
        assert renderer_snapshot[4] == initial_result.name
        assert renderer_snapshot[5] == initial_result.channel
        assert np.array_equal(renderer_snapshot[6], initial_result.frequencies)
        if renderer_snapshot[7] is not None:
            assert initial_result.times is not None
            assert np.array_equal(renderer_snapshot[7], initial_result.times)
        _assert_quantity_snapshot(renderer_snapshot[8], initial_result.f0)
        _assert_quantity_snapshot(renderer_snapshot[9], initial_result.df)
        _assert_quantity_snapshot(renderer_snapshot[10], initial_result.t0)
        _assert_quantity_snapshot(renderer_snapshot[11], initial_result.dt)
        assert source.read_bytes() == source_snapshot[0]
        assert source.stat().st_mtime_ns == source_snapshot[1]
        figure_path = tmp_path / f"e2e-{branch}.png"
        figure.savefig(figure_path)
        assert figure_path.stat().st_size > 0
    finally:
        _cleanup(loaded_client, loaded_session, observed_worker_pids)
        _cleanup(client, session, observed_worker_pids)
        _assert_pids_terminated(observed_worker_pids)
        _assert_owned_shm_absent(owned_shm_names)
        _assert_no_studio_shm_residue(shm_baseline)
        assert (
            frozenset(
                child.pid
                for child in multiprocessing.active_children()
                if child.pid is not None
            )
            <= baseline_children
        )
        current_fds = _fd_targets()
        assert all(
            current_fds.get(target, 0) <= count
            for target, count in baseline_fds.items()
        )
        assert set(current_fds) <= set(baseline_fds)
