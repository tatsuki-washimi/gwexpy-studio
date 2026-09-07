"""Spike 1 contracts for spawn isolation, crash recovery, and cleanup."""

from __future__ import annotations

import functools
import gc
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import (
    PrototypeNotImplementedError,
    WorkerCrashedError,
    WorkerTimeoutError,
)
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient, WorkerLifecycle
from gwexpy_studio.worker.service import worker_main
from tests.support.sentinel import invoke_preserving_sentinel

pytestmark = pytest.mark.integration


def _request(
    request_id: str, message_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Build one deterministic v1 request for the real worker boundary."""
    return {
        "protocol": 2,
        "request_id": request_id,
        "type": message_type,
        "payload": payload,
    }


def _real_client(worker_target: Any = worker_main) -> WorkerClient:
    """Build a spawn-backed client using the production worker entry point."""
    context = multiprocessing.get_context("spawn")
    return WorkerClient(
        context=context,
        process_factory=context.Process,
        connection_factory=context.Pipe,
        worker_target=worker_target,
        startup_timeout_s=60.0,
        request_timeout_s=10.0,
        join_timeout_s=5.0,
    )


def _capability_document() -> dict[str, object]:
    """Return a valid, probe-free capability handshake for test workers."""
    from gwexpy_studio.ops.io_capabilities import (
        load_capability_manifest,
        unprobed_effective_capabilities,
    )

    return unprobed_effective_capabilities(
        load_capability_manifest()
    ).document()


def _blocking_worker(received_event: Any, connection: Any) -> None:
    """Keep one execute request in flight until the parent kills this child."""
    while True:
        try:
            request = json.loads(connection.recv_bytes().decode("utf-8"))
        except EOFError:
            return
        request_id = request["request_id"]
        if request["type"] == "ping":
            response = {
                "protocol": 2,
                "request_id": request_id,
                "type": "result",
                "payload": {
                    "pid": multiprocessing.current_process().pid,
                    "io_capabilities": _capability_document(),
                },
            }
            connection.send_bytes(json.dumps(response).encode("utf-8"))
        elif request["type"] == "execute":
            received_event.set()
            time.sleep(60.0)
        elif request["type"] == "shutdown":
            response = {
                "protocol": 2,
                "request_id": request_id,
                "type": "result",
                "payload": {},
            }
            connection.send_bytes(json.dumps(response).encode("utf-8"))
            connection.close()
            return


class _ReceiptConnection:
    """Child-side wrapper that acknowledges receipt before production dispatch."""

    def __init__(self, connection: Any, received_event: Any) -> None:
        self._connection = connection
        self._received_event = received_event

    def recv_bytes(self) -> bytes:
        value = self._connection.recv_bytes()
        request = json.loads(value.decode("utf-8"))
        if request["type"] == "execute":
            self._received_event.set()
        return value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _acknowledging_worker(received_event: Any, connection: Any) -> None:
    """Run the real worker entry point while exposing a child receipt event."""
    worker_main(_ReceiptConnection(connection, received_event))


def _silent_worker(_connection: Any) -> None:
    """Remain alive without a handshake so startup timeout cleanup is testable."""
    while True:
        time.sleep(1.0)


def _spike1_silent_worker(_connection: Any) -> None:
    """Spawn-importable silent target for the supervised real timeout probe."""
    while True:
        time.sleep(1.0)


def _spike1_write_source(path: str) -> None:
    """Create a real named HDF5 source from a spawn-importable child."""
    from tests.support.fixtures import make_timeseries, random_values, write_named_hdf5

    write_named_hdf5(make_timeseries(random_values()), Path(path))


def _run_bounded_probe(probe: str, *arguments: str, timeout_s: float = 20.0) -> bytes:
    """Run a complete fresh scenario in a supervised process group."""
    repository_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    source_paths = [str(repository_root / "src"), str(repository_root)]
    if environment.get("PYTHONPATH"):
        source_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(source_paths)
    child = subprocess.Popen(
        [sys.executable, "-c", probe, *arguments],
        cwd=repository_root / "tests",
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = child.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3.0)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        assert child.poll() is not None, "supervisor survived timeout cleanup"
        raise AssertionError("fresh worker scenario exceeded watchdog") from error
    assert child.returncode == 0, stderr.decode("utf-8", errors="replace")
    return stdout


def _fresh_scientific_free_probe() -> None:
    """Prove the parent import path is clean in a new interpreter."""
    repository_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    source_paths = [str(repository_root / "src"), str(repository_root)]
    if environment.get("PYTHONPATH"):
        source_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(source_paths)
    probe = """
import sys
from gwexpy_studio.worker.client import WorkerClient
from gwexpy_studio.worker.service import worker_main
assert WorkerClient is not None
assert worker_main is not None
assert 'gwexpy' not in sys.modules
assert 'gwpy' not in sys.modules
assert 'PySide6' not in sys.modules
assert 'PyQt6' not in sys.modules
assert 'qtpy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repository_root / "tests",
        env=environment,
        capture_output=True,
        text=False,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def _fresh_parent_session_replay_probe(source: Path) -> None:
    """Run a real named-HDF5 graph/replay workflow in a fresh parent process."""
    repository_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    source_paths = [str(repository_root / "src"), str(repository_root)]
    if environment.get("PYTHONPATH"):
        source_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(source_paths)
    probe = r"""
import json
import multiprocessing
import os
import sys
import time

forbidden = ("gwexpy", "gwpy", "PySide6", "PyQt6", "qtpy")
before = {name: name in sys.modules for name in forbidden}
from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import DataSourceRef, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient
from gwexpy_studio.worker.service import worker_main

source = sys.argv[1]

class ProbeGraph(OperationGraph):
    def __init__(self, operations):
        self._operations = tuple(operations)

    @property
    def operations(self):
        return self._operations

    def producer_of(self, object_id):
        return next(
            (
                operation
                for operation in self._operations
                if object_id in operation.outputs
            ),
            None,
        )

    def ancestors(self, target_object_ids):
        selected = set()
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

graph = ProbeGraph(
    (
        Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            params={"source": source, "format": "hdf5", "name": "X1:STUDIO-TEST"},
            outputs=("obj-1",),
        ),
        Operation(
            op_id="op-2",
            operation_id="timeseries.asd",
            operation_schema=1,
            inputs={"self": "obj-1"},
            params={"fftlength": {"value": 4.0, "unit": "s"}},
            outputs=("obj-2",),
        ),
    )
)
project = Project(
    project_id="spike-1-parent-probe",
    sources=(
        DataSourceRef(
            source_id="src-001",
            uri=source,
            format="hdf5",
            size_bytes=os.stat(source).st_size,
            mtime=os.stat(source).st_mtime,
        ),
    ),
    graph=graph,
)
context = multiprocessing.get_context("spawn")
client = WorkerClient(
    context=context,
    process_factory=context.Process,
    connection_factory=context.Pipe,
    worker_target=worker_main,
    startup_timeout_s=5.0,
    request_timeout_s=5.0,
    join_timeout_s=5.0,
)
session = StudioSession(project=project, graph=graph, client=client)
baseline_children = {
    child.pid for child in multiprocessing.active_children() if child.pid is not None
}
sentinels = []
materialized = False
worker_pid = None
try:
    try:
        session.start()
    except PrototypeNotImplementedError as error:
        sentinels.append({"code": error.code, "owner": error.owner})
    else:
        try:
            replay_result = session.replay(targets=("obj-2",))
        except PrototypeNotImplementedError as error:
            sentinels.append({"code": error.code, "owner": error.owner})
        else:
            assert replay_result, "fresh replay returned no result"
            replay_payload = (
                replay_result.get("payload", replay_result)
                if isinstance(replay_result, dict)
                else replay_result
            )
            returned_ids = (
                replay_payload.get("object_ids", replay_payload.get("object_id"))
                if isinstance(replay_payload, dict)
                else None
            )
            if isinstance(returned_ids, str):
                returned_ids = (returned_ids,)
            assert tuple(returned_ids or ()) == ("obj-2",)
            assert tuple(value.object_id for value in project.objects) == (
                "obj-1",
                "obj-2",
            )
            assert tuple(record.op_id for record in project.executions) == (
                "op-1",
                "op-2",
            )
            assert all(
                record.status == "succeeded" for record in project.executions
            )
            ping = client.request({
                "protocol": 2,
                "request_id": "00000000-0000-4000-8000-000000000202",
                "type": "ping",
                "payload": {},
            })
            assert ping["type"] == "result"
            worker_pid = ping["payload"]["pid"]
            assert worker_pid != os.getpid()
            listed = client.request({
                "protocol": 2,
                "request_id": "00000000-0000-4000-8000-000000000203",
                "type": "list_objects",
                "payload": {},
            })
            assert listed["type"] == "result"
            assert tuple(listed["payload"]["object_ids"]) == (
                "obj-1",
                "obj-2",
            )
            materialized = True
finally:
    try:
        session.close()
    except PrototypeNotImplementedError as error:
        sentinels.append({"code": error.code, "owner": error.owner})
    if materialized:
        assert client.state.name == "CLOSED"
    else:
        try:
            client.shutdown()
        except PrototypeNotImplementedError as error:
            sentinels.append({"code": error.code, "owner": error.owner})
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        current = {
            child.pid
            for child in multiprocessing.active_children()
            if child.pid is not None
        }
        if current <= baseline_children:
            break
        time.sleep(0.05)
    assert {
        child.pid
        for child in multiprocessing.active_children()
        if child.pid is not None
    } <= baseline_children

after = {name: name in sys.modules for name in forbidden}
assert not any(after.values()), (before, after)
print(json.dumps({
    "before": before,
    "after": after,
    "materialized": materialized,
    "worker_pid": worker_pid,
    "sentinels": sentinels,
}))
"""
    report = json.loads(
        _run_bounded_probe(probe, str(source)).decode("utf-8").splitlines()[-1]
    )
    assert report["after"] == {
        "gwexpy": False,
        "gwpy": False,
        "PySide6": False,
        "PyQt6": False,
        "qtpy": False,
    }
    if report["materialized"]:
        assert report["worker_pid"] != os.getpid()
    else:
        assert report["sentinels"]
        assert all(
            item["code"] == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
            for item in report["sentinels"]
        )
        assert any(
            item["owner"] == "gwexpy_studio.session.StudioSession.start"
            for item in report["sentinels"]
        )


class _TimeoutEndpoint:
    """Control endpoint that records close without ever producing a ping."""

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    def send_bytes(self, _value: bytes) -> None:
        assert not self.closed

    def recv_bytes(self) -> bytes:
        raise TimeoutError("startup probe intentionally has no response")

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _TimeoutProcess:
    """Process double matching the accepted C-CLIENT-020 lifecycle recorder."""

    sentinel = object()

    def __init__(self) -> None:
        self.lifecycle_calls: list[tuple[str, Any]] = []
        self.alive = False
        self.terminated = False
        self.killed = False
        self.closed = False

    def start(self) -> None:
        self.lifecycle_calls.append(("start", None))
        self.alive = True

    def is_alive(self) -> bool:
        assert not self.closed, "is_alive called after Process.close"
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        assert not self.closed, "join called after Process.close"
        self.lifecycle_calls.append(("join", timeout))
        if self.killed or not self.terminated:
            self.alive = False

    def terminate(self) -> None:
        assert not self.closed
        self.lifecycle_calls.append(("terminate", None))
        self.terminated = True

    def kill(self) -> None:
        assert not self.closed
        self.lifecycle_calls.append(("kill", None))
        self.killed = True
        self.alive = False

    def close(self) -> None:
        self.lifecycle_calls.append(("close", None))
        self.closed = True


class _TimeoutContext:
    """Injected context for a deterministic startup-timeout contract."""

    def __init__(self, process: _TimeoutProcess) -> None:
        self.process = process

    def Pipe(self, *_args: Any, **_kwargs: Any) -> tuple[Any, Any]:
        return _TimeoutEndpoint(), _TimeoutEndpoint()

    def Process(self, *_args: Any, **_kwargs: Any) -> _TimeoutProcess:
        return self.process


def _fresh_real_start_timeout_probe() -> dict[str, Any]:
    """Run real startup timeout handling under an external process supervisor."""
    probe = r"""
import json
import multiprocessing

from gwexpy_studio.errors import PrototypeNotImplementedError, WorkerTimeoutError
from gwexpy_studio.worker.client import WorkerClient

def silent_worker(_connection):
    import time
    while True:
        time.sleep(1.0)

def closed_flag(value):
    return bool(
        getattr(value, "closed", False)
        or getattr(value, "_closed", False)
    )

def main():
    from tests.spikes.test_spike1_worker_isolation import _spike1_silent_worker

    context = multiprocessing.get_context("spawn")
    original_process = context.Process
    original_pipe = context.Pipe
    captured_processes = []
    captured_endpoints = []

    def capture_process(*args, **kwargs):
        process = original_process(*args, **kwargs)
        captured_processes.append(process)
        return process

    def capture_pipe(*args, **kwargs):
        endpoints = original_pipe(*args, **kwargs)
        captured_endpoints.extend(endpoints)
        return endpoints

    client = WorkerClient(
        context=context,
        process_factory=capture_process,
        connection_factory=capture_pipe,
        worker_target=_spike1_silent_worker,
        startup_timeout_s=0.5,
        request_timeout_s=1.0,
        join_timeout_s=5.0,
    )
    try:
        client.start()
    except PrototypeNotImplementedError as error:
        report = {
            "status": "sentinel",
            "code": error.code,
            "owner": error.owner,
            "captured_processes": len(captured_processes),
            "captured_endpoints": len(captured_endpoints),
        }
    except WorkerTimeoutError as error:
        report = {
            "status": "timeout",
            "code": error.code,
            "state": client.state.name,
            "captured_processes": len(captured_processes),
            "captured_endpoints": len(captured_endpoints),
            "process_closed": [closed_flag(item) for item in captured_processes],
            "endpoint_closed": [closed_flag(item) for item in captured_endpoints],
        }
    else:
        raise AssertionError("silent startup unexpectedly returned")
    print(json.dumps(report))

main()
"""
    output = _run_bounded_probe(probe, timeout_s=15.0)
    return json.loads(output.decode("utf-8").splitlines()[-1])


def _child_generated_source(tmp_path: Path) -> Path:
    """Return HDF5 written by a child owned by an external supervisor."""
    source = tmp_path / "spike-1-source.h5"
    probe = r"""
import multiprocessing
import sys

def write_source(path):
    from tests.support.fixtures import make_timeseries, random_values, write_named_hdf5
    write_named_hdf5(make_timeseries(random_values()), path)

def finish_child(child, started):
    if not started:
        return
    if child.is_alive():
        child.terminate()
        child.join(timeout=5.0)
    if child.is_alive():
        child.kill()
        child.join(timeout=5.0)
    assert not child.is_alive(), "source writer child leaked"
    child.close()

def main():
    from tests.spikes.test_spike1_worker_isolation import _spike1_write_source

    context = multiprocessing.get_context("spawn")
    child = context.Process(target=_spike1_write_source, args=(sys.argv[1],))
    started = False
    try:
        child.start()
        started = True
        child.join(timeout=30.0)
        exitcode = child.exitcode
        finish_child(child, started)
        started = False
        assert exitcode == 0
        print("ok")
    finally:
        finish_child(child, started)

main()
"""
    output = _run_bounded_probe(probe, str(source), timeout_s=40.0)
    assert output.decode("utf-8").splitlines()[-1] == "ok"
    assert source.is_file()
    return source


def _process_of(client: WorkerClient) -> Any:
    """Expose the implementation-owned child handle for lifecycle assertions."""
    process = getattr(client, "_process", None)
    if process is None:
        process = getattr(client, "process", None)
    assert process is not None, "WorkerClient must retain its child process handle"
    return process


def _connection_of(client: WorkerClient) -> Any:
    """Expose the implementation-owned control endpoint for cleanup checks."""
    connection = getattr(client, "_connection", None)
    if connection is None:
        connection = getattr(client, "connection", None)
    assert connection is not None, "WorkerClient must retain its control endpoint"
    return connection


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


def _fd_snapshot() -> Counter[str]:
    """Snapshot live parent fd targets while preserving target multiplicity."""
    if not sys.platform.startswith("linux"):
        return Counter()
    targets: Counter[str] = Counter()
    for name in os.listdir("/proc/self/fd"):
        try:
            targets[os.readlink(f"/proc/self/fd/{name}")] += 1
        except FileNotFoundError:
            continue
    return targets


def _active_child_pids() -> frozenset[int]:
    """Return active child PIDs without retaining a process object."""
    return frozenset(
        child.pid
        for child in multiprocessing.active_children()
        if child.pid is not None
    )


def _graceful_close(client: WorkerClient, session: StudioSession) -> tuple[int, int]:
    """Close through the production session before using the force backstop."""
    process = _process_of(client)
    connection = _connection_of(client)
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
    assert sentinel_fd >= 0
    return sentinel_fd, connection_fd


def _shutdown_if_started(client: WorkerClient) -> None:
    """Force bounded child cleanup without invoking a second foundation sentinel."""
    process = getattr(client, "_process", None)
    if process is None:
        process = getattr(client, "process", None)
    if process is None:
        return
    if getattr(process, "closed", False) or getattr(process, "_closed", False):
        # Production close is already asserted by the caller.  The backstop
        # must not close the endpoint a second time or make cleanup appear to
        # have been performed by the test.
        return
    process_pid = getattr(process, "pid", None)
    if process.is_alive():
        process.kill()
    if process_pid is not None:
        process.join(timeout=2.0)
    assert not process.is_alive(), "worker child survived cleanup watchdog"
    close = getattr(process, "close", None)
    if callable(close) and process_pid is not None:
        close()
    connection = getattr(client, "_connection", None)
    if connection is None:
        connection = getattr(client, "connection", None)
    close_connection = getattr(connection, "close", None)
    if callable(close_connection):
        close_connection()


@pytest.mark.contract("I-S1-001")
def test_spawn_worker_startup_ping_crash_restart_and_replay_preserve_identity(
    tmp_path: Path,
) -> None:
    """A real spawned worker survives client crash and replays stable outputs."""
    hdf5_source = _child_generated_source(tmp_path)
    receipt_context = multiprocessing.get_context("spawn")
    receipt_event = receipt_context.Event()
    client = _real_client(functools.partial(_acknowledging_worker, receipt_event))
    parent_fds = _fd_snapshot()
    parent_children = _active_child_pids()
    session: StudioSession | None = None
    sentinel_fd: int | None = None
    try:
        _fresh_scientific_free_probe()
        _fresh_parent_session_replay_probe(hdf5_source)
        invoke_preserving_sentinel(
            client.start,
            owner="gwexpy_studio.worker.client.WorkerClient.start",
        )
        _fresh_scientific_free_probe()
        _fresh_parent_session_replay_probe(hdf5_source)

        graph = OperationGraph()
        graph.add(
            Operation(
                op_id="op-1",
                operation_id="timeseries.read",
                operation_schema=1,
                params={
                    "source": str(hdf5_source),
                    "format": "hdf5",
                    "name": "X1:STUDIO-TEST",
                },
                outputs=("obj-1",),
            )
        )
        graph.add(
            Operation(
                op_id="op-2",
                operation_id="timeseries.asd",
                operation_schema=1,
                inputs={"self": "obj-1"},
                params={"fftlength": {"value": 4.0, "unit": "s"}},
                outputs=("obj-2",),
            )
        )
        project = Project(
            project_id="spike-1",
            created="2026-08-16T00:00:00Z",
            modified="2026-08-16T00:00:00Z",
            graph=graph,
        )
        session = StudioSession(project=project, graph=graph, client=client)
        before_graph = _operation_snapshot(graph.operations)

        ping = client.request(
            _request(
                "00000000-0000-4000-8000-000000000301",
                "ping",
                {},
            )
        )
        assert ping["type"] == "result"
        assert ping["payload"]["pid"] != os.getpid()
        assert {"pid", "python", "gwexpy_version", "gwexpy_path"} <= set(
            ping["payload"]
        )
        initial_execution_count = len(project.executions)
        initial_replay = session.replay(targets=("obj-2",))
        assert initial_replay
        assert _object_ids_from_replay(initial_replay) == ("obj-2",)
        assert receipt_event.wait(timeout=2.0), "child did not acknowledge execute"
        initial_records = project.executions[initial_execution_count:]
        assert tuple(record.op_id for record in initial_records) == ("op-1", "op-2")
        assert all(record.status == "succeeded" for record in initial_records)
        refs_before_crash = client.request(
            _request(
                "00000000-0000-4000-8000-000000000302",
                "list_objects",
                {},
            )
        )
        assert refs_before_crash["type"] == "result"
        assert tuple(refs_before_crash["payload"]["object_ids"]) == (
            "obj-1",
            "obj-2",
        )
        refs_before_payload = refs_before_crash["payload"]

        process = _process_of(client)
        sentinel_fd = process.sentinel
        connection = _connection_of(client)
        connection_fd = connection.fileno()
        process.kill()
        process.join(timeout=5.0)
        assert not process.is_alive()
        with pytest.raises(WorkerCrashedError) as crashed:
            client.request(
                _request(
                    "00000000-0000-4000-8000-000000000303",
                    "list_objects",
                    {},
                ),
                timeout_s=2.0,
            )
        assert crashed.value.code == "worker_crashed"
        assert client.state is WorkerLifecycle.CRASHED
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
        _fresh_scientific_free_probe()
        _fresh_parent_session_replay_probe(hdf5_source)
        receipt_event.clear()
        replay_count_before = len(project.executions)
        replayed = session.replay(targets=("obj-2",))
        assert replayed
        assert _object_ids_from_replay(replayed) == ("obj-2",)
        assert receipt_event.wait(timeout=2.0), (
            "restarted child did not acknowledge execute"
        )
        replay_records = project.executions[replay_count_before:]
        assert tuple(record.op_id for record in replay_records) == ("op-1", "op-2")
        assert all(record.status == "succeeded" for record in replay_records)
        assert _operation_snapshot(graph.operations) == before_graph
        refs_after_replay = client.request(
            _request(
                "00000000-0000-4000-8000-000000000304",
                "list_objects",
                {},
            )
        )
        assert tuple(refs_after_replay["payload"]["object_ids"]) == (
            "obj-1",
            "obj-2",
        )
        assert refs_after_replay["payload"] == refs_before_payload
        assert tuple(record.op_id for record in project.executions) == (
            "op-1",
            "op-2",
            "op-1",
            "op-2",
        )
        assert tuple(record.status for record in project.executions) == (
            "succeeded",
            "succeeded",
            "succeeded",
            "succeeded",
        )
        _fresh_parent_session_replay_probe(hdf5_source)
    finally:
        if session is not None and client.state is WorkerLifecycle.RUNNING:
            try:
                sentinel_fd, _connection_fd = _graceful_close(client, session)
            finally:
                _shutdown_if_started(client)
        else:
            _shutdown_if_started(client)
        if sentinel_fd is not None:
            with pytest.raises(OSError):
                os.fstat(sentinel_fd)
        client.worker_target = None
        del receipt_event
        gc.collect()
        assert _fd_snapshot() <= parent_fds
        assert _active_child_pids() <= parent_children


@pytest.mark.contract("I-S1-002")
def test_inflight_kill_does_not_escape_client_and_cleans_child(
    tmp_path: Path,
) -> None:
    """Killing an executing child returns a bounded crash error to the client."""
    hdf5_source = _child_generated_source(tmp_path)
    context = multiprocessing.get_context("spawn")
    received_event = context.Event()
    client = WorkerClient(
        context=context,
        process_factory=context.Process,
        connection_factory=context.Pipe,
        worker_target=functools.partial(_blocking_worker, received_event),
        startup_timeout_s=5.0,
        request_timeout_s=30.0,
        join_timeout_s=2.0,
    )
    parent_fds = _fd_snapshot()
    parent_children = _active_child_pids()
    request_thread: threading.Thread | None = None
    try:
        _fresh_scientific_free_probe()
        _fresh_parent_session_replay_probe(hdf5_source)
        invoke_preserving_sentinel(
            client.start,
            owner="gwexpy_studio.worker.client.WorkerClient.start",
        )

        ping = client.request(
            _request(
                "00000000-0000-4000-8000-000000000305",
                "ping",
                {},
            )
        )
        assert ping["type"] == "result"
        assert ping["payload"]["pid"] != os.getpid()
        _fresh_scientific_free_probe()
        _fresh_parent_session_replay_probe(hdf5_source)

        errors: list[BaseException] = []

        def request_in_flight() -> None:
            try:
                client.request(
                    _request(
                        "00000000-0000-4000-8000-000000000307",
                        "execute",
                        {"operation": "timeseries.test_blocking"},
                    ),
                    timeout_s=10.0,
                )
            except BaseException as error:
                errors.append(error)

        request_thread = threading.Thread(target=request_in_flight)
        request_thread.start()
        deadline = time.monotonic() + 2.0
        assert received_event.wait(timeout=2.0), "child did not receive execute request"
        while not request_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert request_thread.is_alive(), "request did not become in-flight"

        process = _process_of(client)
        sentinel_fd = process.sentinel
        connection = _connection_of(client)
        connection_fd = connection.fileno()
        process.kill()
        process.join(timeout=5.0)
        request_thread.join(timeout=5.0)
        assert not process.is_alive()
        assert not request_thread.is_alive(), "crash detection exceeded watchdog"
        assert len(errors) == 1
        assert isinstance(errors[0], WorkerCrashedError)
        assert errors[0].code == "worker_crashed"
        assert client.state is WorkerLifecycle.CRASHED
        assert getattr(connection, "closed", False) is True
        with pytest.raises(OSError):
            os.fstat(connection_fd)
        assert bool(
            getattr(process, "closed", False) or getattr(process, "_closed", False)
        ), "crash handling must close the production process handle"
        with pytest.raises(OSError):
            os.fstat(sentinel_fd)
    finally:
        try:
            _shutdown_if_started(client)
        finally:
            if request_thread is not None:
                request_thread.join(timeout=2.0)
                assert not request_thread.is_alive(), "request thread leaked"
            _fresh_scientific_free_probe()
            client.worker_target = None
            del received_event
            gc.collect()
            assert _fd_snapshot() <= parent_fds
            assert _active_child_pids() <= parent_children


@pytest.mark.contract("I-S1-003")
def test_start_timeout_terminates_and_closes_timed_out_child() -> None:
    """A handshake timeout cannot leave a silent spawned child behind."""
    report = _fresh_real_start_timeout_probe()
    timeout_client: WorkerClient | None = None

    try:
        if report["status"] == "sentinel":
            assert report["code"] == "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
            assert report["owner"] == ("gwexpy_studio.worker.client.WorkerClient.start")
            raise PrototypeNotImplementedError(
                code=report["code"], owner=report["owner"]
            )
        assert report["status"] == "timeout"
        assert report["code"] == "worker_timeout"
        assert report["state"] == "CRASHED"
        assert report["captured_processes"] == 1
        assert report["captured_endpoints"] == 2
        assert report["process_closed"] == [True]
        assert report["endpoint_closed"] == [True, True]

        timeout_process = _TimeoutProcess()
        timeout_context = _TimeoutContext(timeout_process)
        timeout_client = WorkerClient(
            context=timeout_context,
            process_factory=timeout_context.Process,
            connection_factory=timeout_context.Pipe,
            worker_target=_silent_worker,
            startup_timeout_s=0.5,
            request_timeout_s=1.0,
            join_timeout_s=5.0,
            wait_function=lambda _targets, _timeout: [],
        )
        with pytest.raises(WorkerTimeoutError) as timeout:
            timeout_client.start()
        assert timeout.value.code == "worker_timeout"
        assert timeout_client.state is WorkerLifecycle.CRASHED
        timeout_connection = _connection_of(timeout_client)
        assert timeout_process.lifecycle_calls == [
            ("start", None),
            ("terminate", None),
            ("join", 5.0),
            ("kill", None),
            ("join", 5.0),
            ("close", None),
        ]
        assert timeout_process.closed is True
        assert timeout_process.alive is False
        assert timeout_connection.closed is True
        assert timeout_connection.close_calls == 1
    finally:
        if timeout_client is not None:
            _shutdown_if_started(timeout_client)
