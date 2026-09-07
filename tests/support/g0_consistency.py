"""Helpers for G0 worker and generated-Python consistency checks."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy import units as u

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import DataObjectRef, DataSourceRef, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.export.python_exporter import export_python
from gwexpy_studio.worker.client import WorkerClient
from gwexpy_studio.worker.service import worker_main
from tests.support.fixtures import CHANNEL_NAME, TEST_NAME, make_timeseries


def _client() -> WorkerClient:
    """Create a client targeting the real top-level worker entry point."""
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


def _request_id() -> str:
    """Return a request ID without importing worker internals."""
    return str(uuid.uuid4())


def _execute(
    client: WorkerClient,
    *,
    op_id: str,
    operation_id: str,
    params: dict[str, Any],
    inputs: dict[str, str],
    output: str,
) -> dict[str, Any]:
    """Execute one operation through the worker protocol."""
    response = client.request(
        {
            "protocol": 2,
            "request_id": _request_id(),
            "type": "execute",
            "payload": {
                "operation": {
                    "op_id": op_id,
                    "operation_id": operation_id,
                    "operation_schema": 1,
                    "inputs": inputs,
                    "params": params,
                    "outputs": [output],
                },
                "graph_id": "g0-acceptance",
                "operation_id": op_id,
                "input_object_ids": list(inputs.values()),
                "output_object_id": output,
            },
        }
    )
    assert response["type"] == "result", response
    return dict(response["payload"])


def _worker_snapshot(
    client: WorkerClient, payload: dict[str, Any], *, shm_prefix: str
) -> dict[str, Any]:
    """Copy a worker result and release its worker-owned shared memory."""
    object_ref = dict(payload["object_ref"])
    fetched = client.fetch_array(object_ref["object_id"])
    try:
        assert fetched.descriptor.name.startswith(shm_prefix)
        values = np.array(fetched.preview.values, copy=True)
    finally:
        fetched.preview.handle.close()
    client.release_array(fetched.descriptor.name)

    axes = dict(object_ref["axes"])
    t0 = float(axes["t0"]["value"])
    dt = float(axes["dt"]["value"])
    return {
        "value": values,
        "unit": str(u.Unit(object_ref["unit"])),
        "name": object_ref["name"],
        "channel": object_ref["channel"],
        "t0": t0,
        "dt": dt,
        "duration": float(values.size * dt),
        "axes": [t0 + index * dt for index in range(values.size)],
    }


def _public_snapshot(series: Any) -> dict[str, Any]:
    """Snapshot public GWexpy fields used by the G0 contract."""
    return {
        "value": np.array(series.value, copy=True),
        "unit": str(u.Unit(series.unit)),
        "name": str(series.name) if series.name is not None else None,
        "channel": str(series.channel) if series.channel is not None else None,
        "t0": float(series.t0.to_value("s")),
        "dt": float(series.dt.to_value("s")),
        "duration": float(series.duration.to_value("s")),
        "axes": [float(value) for value in series.times.to_value("s")],
    }


def _assert_same_snapshot(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    """Compare values and all G0 time-series metadata."""
    np.testing.assert_array_equal(actual["value"], expected["value"])
    for key in ("unit", "name", "channel"):
        assert actual[key] == expected[key], key
    for key in ("t0", "dt", "duration"):
        assert actual[key] == pytest.approx(expected[key]), key
    np.testing.assert_allclose(actual["axes"], expected["axes"], rtol=0, atol=0)


def _csv_project(source: Path) -> Project:
    """Build the smallest graph whose read operation uses Studio CSV format."""
    operation = Operation(
        op_id="op-read",
        operation_id="timeseries.read",
        operation_schema=1,
        params={
            "source": {"source_id": "source-1"},
            "format": "csv_enhanced",
            "name": TEST_NAME,
        },
        outputs=("object-1",),
    )
    return Project(
        project_id="g0-csv",
        sources=(
            DataSourceRef(
                source_id="source-1",
                uri=str(source),
                format="csv_enhanced",
                size_bytes=source.stat().st_size,
                mtime=source.stat().st_mtime,
            ),
        ),
        objects=(
            DataObjectRef(
                object_id="object-1",
                kind="TimeSeries",
                shape=(4,),
                dtype="float64",
                unit="m",
                name=TEST_NAME,
                channel=CHANNEL_NAME,
            ),
        ),
        graph=OperationGraph((operation,)),
    )


def _crop_project(
    source: Path, *, start: float | None = None, end: float | None = None
) -> Project:
    """Build a read-to-crop graph for generated Python."""
    crop_params = {
        key: value
        for key, value in (("start", start), ("end", end))
        if value is not None
    }
    operations = (
        Operation(
            op_id="op-read",
            operation_id="timeseries.read",
            operation_schema=1,
            params={
                "source": {"source_id": "source-1"},
                "format": "hdf5",
                "name": TEST_NAME,
            },
            outputs=("object-1",),
        ),
        Operation(
            op_id="op-crop",
            operation_id="timeseries.crop",
            operation_schema=1,
            inputs={"self": "object-1"},
            params=crop_params,
            outputs=("object-2",),
        ),
    )
    objects = (
        DataObjectRef(
            object_id="object-1",
            kind="TimeSeries",
            shape=(15360,),
            dtype="float64",
            unit="m",
            name=TEST_NAME,
            channel=CHANNEL_NAME,
        ),
        DataObjectRef(
            object_id="object-2",
            kind="TimeSeries",
            shape=(15360,),
            dtype="float64",
            unit="m",
            name=TEST_NAME,
            channel=CHANNEL_NAME,
            produced_by="op-crop",
        ),
    )
    return Project(
        project_id="g0-crop",
        sources=(
            DataSourceRef(
                source_id="source-1",
                uri=str(source),
                format="hdf5",
                size_bytes=source.stat().st_size,
                mtime=source.stat().st_mtime,
            ),
        ),
        objects=objects,
        graph=OperationGraph(operations),
    )


def _generated_source(source: str, *, crop: bool) -> str:
    """Append a JSON snapshot to generated Python source."""
    target = "object_2" if crop else "object_1"
    return (
        source
        + f"""
print(json.dumps({{
    'value': {target}.value.tolist(), 'unit': str({target}.unit),
    'name': str({target}.name) if {target}.name is not None else None,
    'channel': str({target}.channel) if {target}.channel is not None else None,
    't0': float({target}.t0.to_value('s')), 'dt': float({target}.dt.to_value('s')),
    'duration': float({target}.duration.to_value('s')),
    'axes': [float(value) for value in {target}.times.to_value('s')]
}}, sort_keys=True))
"""
    )


def _run_generated(
    source: str, script: Path, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run generated Python in a fresh interpreter."""
    script.write_text(source, encoding="utf-8")
    environment = os.environ.copy()
    environment["MPLCONFIGDIR"] = str(cwd / "mplconfig")
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _generated_snapshot(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Decode the last JSON line emitted by generated Python."""
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _pid_exists(pid: int) -> bool:
    """Check a process identity without signalling it."""
    return Path(f"/proc/{pid}").exists()


def _shm_names(prefix: str) -> set[str]:
    """Return POSIX shared-memory names owned by one G0 worker scope."""
    try:
        return {
            entry.name
            for entry in Path("/dev/shm").iterdir()
            if entry.name.startswith(prefix)
        }
    except FileNotFoundError:
        return set()


@contextmanager
def _owned_shm_prefix() -> Iterator[str]:
    """Isolate one spawned G0 worker from unrelated shared-memory activity."""
    variable = "GWEXPY_STUDIO_SHM_PREFIX"
    previous = os.environ.get(variable)
    prefix = f"g0-{uuid.uuid4().hex}-"
    os.environ[variable] = prefix
    try:
        yield prefix
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


def assert_csv_worker_and_export(tmp_path: Path) -> None:
    """Compare CSV values and metadata across worker, export, and oracle."""
    with _owned_shm_prefix() as shm_prefix:
        csv_source = tmp_path / "g0-source.csv"
        make_timeseries(np.arange(4.0)).write(csv_source, format="csv")
        import gwexpy

        gwexpy.register_all(include_io=True)
        from gwexpy.timeseries import TimeSeries

        oracle = _public_snapshot(
            TimeSeries.read(csv_source, format="csv", name=TEST_NAME)
        )
        client = _client()
        try:
            client.start()
            payload = _execute(
                client,
                op_id="op-read",
                operation_id="timeseries.read",
                params={
                    "source": str(csv_source),
                    "format": "csv_enhanced",
                    "name": TEST_NAME,
                },
                inputs={},
                output="object-1",
            )
            _assert_same_snapshot(
                _worker_snapshot(client, payload, shm_prefix=shm_prefix), oracle
            )
        finally:
            pid = getattr(getattr(client, "_process", None), "pid", None)
            client.shutdown()
            if pid is not None:
                assert not _pid_exists(pid)
        assert not _shm_names(shm_prefix)

        output = tmp_path / "g0-exported.npy"
        generated = export_python(
            _csv_project(csv_source),
            value_dumps={"object-1": output},
            deterministic=True,
        )
        completed = _run_generated(
            _generated_source(generated, crop=False),
            tmp_path / "g0-generated.py",
            tmp_path,
        )
        assert completed.returncode == 0, completed.stderr
        exported = _generated_snapshot(completed)
        exported["value"] = np.load(output, allow_pickle=False)
        _assert_same_snapshot(exported, oracle)


def assert_crop_worker_and_export_parity(
    source: Path, timeseries: Any, tmp_path: Path
) -> None:
    """Compare four crop forms and out-of-range ValueError semantics."""
    with _owned_shm_prefix() as shm_prefix:
        t0 = float(timeseries.t0.to_value("s"))
        t1 = t0 + float(timeseries.duration.to_value("s"))
        cases: tuple[tuple[str, dict[str, float], bool], ...] = (
            ("start-only", {"start": t0 + 1.0}, True),
            ("end-only", {"end": t0 + 10.0}, True),
            ("both", {"start": t0 + 1.0, "end": t0 + 10.0}, True),
            ("omitted", {}, True),
            ("start-outside", {"start": t0 - 1.0}, False),
            ("end-outside", {"end": t1 + 1.0}, False),
        )
        for case_name, params, succeeds in cases:
            import gwexpy

            gwexpy.register_all(include_io=False)
            client = _client()
            try:
                client.start()
                _execute(
                    client,
                    op_id="op-read",
                    operation_id="timeseries.read",
                    params={"source": str(source), "format": "hdf5", "name": TEST_NAME},
                    inputs={},
                    output="object-1",
                )
                request = {
                    "protocol": 2,
                    "request_id": _request_id(),
                    "type": "execute",
                    "payload": {
                        "operation": {
                            "op_id": "op-crop",
                            "operation_id": "timeseries.crop",
                            "operation_schema": 1,
                            "inputs": {"self": "object-1"},
                            "params": params,
                            "outputs": ["object-2"],
                        },
                        "graph_id": "g0-acceptance",
                        "operation_id": "op-crop",
                        "input_object_ids": ["object-1"],
                        "output_object_id": "object-2",
                    },
                }
                response = client.request(request)
                if succeeds:
                    assert response["type"] == "result", response
                    worker = _worker_snapshot(
                        client,
                        dict(response["payload"]),
                        shm_prefix=shm_prefix,
                    )
                    _assert_same_snapshot(
                        worker, _public_snapshot(timeseries.crop(**params))
                    )
                else:
                    assert response["type"] == "error", response
                    assert response["code"] == "operation_failed"
                    worker_message = str(response["message"])
                    outer, base_cause = worker_message.split(": ", 1)
                    assert outer == "Execution failed for operation op-crop"
                    assert base_cause.startswith("Crop ")
                    assert "is outside series span" in base_cause
            finally:
                pid = getattr(getattr(client, "_process", None), "pid", None)
                client.shutdown()
                if pid is not None:
                    assert not _pid_exists(pid), case_name
                assert not _shm_names(shm_prefix), case_name

            output = tmp_path / f"g0-{case_name}.npy"
            generated = export_python(
                _crop_project(source, **params),
                value_dumps={"object-2": output},
                deterministic=True,
            )
            completed = _run_generated(
                _generated_source(generated, crop=True),
                tmp_path / f"g0-{case_name}.py",
                tmp_path,
            )
            if succeeds:
                assert completed.returncode == 0, completed.stderr
                exported = _generated_snapshot(completed)
                exported["value"] = np.load(output, allow_pickle=False)
                _assert_same_snapshot(
                    exported, _public_snapshot(timeseries.crop(**params))
                )
            else:
                assert completed.returncode != 0, case_name
                assert "ValueError" in completed.stderr
                assert base_cause in completed.stderr


def assert_crop_value_error_worker_and_export(
    source: Path, timeseries: Any, tmp_path: Path
) -> None:
    """Compatibility entry point for the existing crop contract test."""
    assert_crop_worker_and_export_parity(source, timeseries, tmp_path)
