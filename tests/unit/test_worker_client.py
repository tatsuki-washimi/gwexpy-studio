"""Contract tests for the injectable worker-client lifecycle boundary."""

from __future__ import annotations

import inspect
import json
import os
import select
import threading
from collections.abc import Callable
from multiprocessing.connection import wait as multiprocessing_wait
from typing import Any, cast, get_type_hints

import pytest

from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.export.python_exporter import export_python
from gwexpy_studio.persistence.project_io import save_project
from gwexpy_studio.runtime.executor import execute_operation
from gwexpy_studio.worker.client import WorkerClient, WorkerLifecycle
from gwexpy_studio.worker.protocol import UUID4, RequestEnvelope
from gwexpy_studio.worker.shm import SharedMemoryDescriptor

pytestmark = pytest.mark.unit

_FOUNDATION_CODE = "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
_START_OWNER = "gwexpy_studio.worker.client.WorkerClient.start"
_REQUEST_OWNER = "gwexpy_studio.worker.client.WorkerClient.request"
_SHUTDOWN_OWNER = "gwexpy_studio.worker.client.WorkerClient.shutdown"
_RESTART_OWNER = "gwexpy_studio.worker.client.WorkerClient.restart"
_GET_ARRAY_OWNER = "gwexpy_studio.worker.client.WorkerClient.get_array"
_REQUEST_ID = "00000000-0000-4000-8000-000000000002"

#: Marker asking the scripted worker to omit a descriptor field entirely,
#: as distinct from sending it with a wrong-typed value.
_DROP_DESCRIPTOR_FIELD = object()


def _assert_sentinel(error: PrototypeNotImplementedError, owner: str) -> None:
    assert type(error) is PrototypeNotImplementedError
    assert error.code == _FOUNDATION_CODE
    assert error.owner == owner


def _invoke(owner: str, callback: Callable[[], Any]) -> Any:
    """Call the API while preserving prototype sentinel identity."""
    try:
        return callback()
    except PrototypeNotImplementedError as error:
        _assert_sentinel(error, owner)
        raise


def _require_coded_error(
    error: Exception,
    *,
    type_name: str,
    code: str,
) -> None:
    """Require a declared public worker error instead of accepting a no-op."""
    from gwexpy_studio import errors as error_module

    error_type = getattr(error_module, type_name, None)
    if error_type is None:
        pytest.fail(
            f"gwexpy_studio.errors.{type_name}(StudioError) must declare a .code field "
            f"for {code}."
        )
    assert isinstance(error, error_type)
    if not hasattr(error, "code"):
        pytest.fail(
            f"Add a non-empty .code attribute to gwexpy_studio.errors.{type_name}."
        )
    assert error.code == code


def _expect_client_error(
    owner: str,
    callback: Callable[[], Any],
    *,
    type_name: str,
    code: str,
) -> None:
    try:
        callback()
    except PrototypeNotImplementedError as error:
        _assert_sentinel(error, owner)
        raise
    except Exception as error:
        _require_coded_error(error, type_name=type_name, code=code)
    else:
        pytest.fail(f"client operation silently succeeded; expected {code}")


def _ping_request() -> RequestEnvelope:
    return {
        "protocol": 2,
        "request_id": UUID4(_REQUEST_ID),
        "type": "ping",
        "payload": {},
    }


def _execute_request() -> RequestEnvelope:
    return cast(
        RequestEnvelope,
        {
            "protocol": 2,
            "request_id": UUID4(_REQUEST_ID),
            "type": "execute",
            "payload": {"operation": "timeseries.detrend"},
        },
    )


def _developer_capability_snapshot() -> dict[str, object]:
    """Return the same minimal, validated worker handshake document as source mode."""
    from gwexpy_studio.ops.io_capabilities import (
        CapabilityManifest,
        probe_effective_capabilities,
    )

    return probe_effective_capabilities(
        CapabilityManifest(mode="developer")
    ).document()


class _ScriptedProcess:
    """Deterministic Process double with ordered lifecycle calls."""

    def __init__(self, events: list[tuple[str, Any]]) -> None:
        self.events = events
        self.lifecycle_calls: list[tuple[str, Any]] = []
        self.alive = False
        self.exitcode: int | None = None
        self._sentinel_read, self._sentinel_write = os.pipe()
        self.sentinel = self._sentinel_read
        self.terminated = False
        self.killed = False

    def start(self) -> None:
        self.events.append(("process.start", None))
        self.lifecycle_calls.append(("start", None))
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.events.append(("process.join", timeout))
        self.lifecycle_calls.append(("join", timeout))
        if not self.killed and self.terminated:
            return
        self.alive = False

    def terminate(self) -> None:
        self.events.append(("process.terminate", None))
        self.lifecycle_calls.append(("terminate", None))
        self.terminated = True
        self._signal_sentinel()

    def kill(self) -> None:
        self.events.append(("process.kill", None))
        self.lifecycle_calls.append(("kill", None))
        self.killed = True
        self.alive = False
        self.exitcode = -9
        self._signal_sentinel()

    def _signal_sentinel(self) -> None:
        try:
            os.write(self._sentinel_write, b"\0")
        except OSError:
            pass

    def close(self) -> None:
        self.events.append(("process.close", None))
        self.lifecycle_calls.append(("close", None))
        for descriptor in (self._sentinel_read, self._sentinel_write):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _response(
    request_id: str,
    *,
    response_type: str = "result",
    payload: dict[str, Any] | None = None,
    code: str | None = None,
    message: str = "",
) -> bytes:
    if response_type == "error":
        value: dict[str, Any] = {
            "protocol": 2,
            "request_id": request_id,
            "type": "error",
            "code": code or "operation_failed",
            "message": message or "worker error",
        }
    else:
        value = {
            "protocol": 2,
            "request_id": request_id,
            "type": "result",
            "payload": {} if payload is None else payload,
        }
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class _ScriptedConnection:
    """Connection double implementing the full send/poll/receive/close seam."""

    def __init__(
        self,
        events: list[tuple[str, Any]],
        mode: str,
        *,
        read_fd: int | None = None,
        write_fd: int | None = None,
        descriptor_patch: dict[str, Any] | None = None,
    ) -> None:
        self.events = events
        self.mode = mode
        self.descriptor_patch = descriptor_patch
        if read_fd is None or write_fd is None:
            read_fd, write_fd = os.pipe()
        self._read_fd = read_fd
        self._write_fd = write_fd
        self.calls: list[tuple[str, Any]] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.received_messages: list[dict[str, Any]] = []
        self._responses: list[bytes] = []
        self.closed = False
        self.send_event = threading.Event()
        self.request_event = threading.Event()
        self.release_response = threading.Event()
        self._overlap_waiting = False

    def fileno(self) -> int:
        return self._read_fd

    def _signal_ready(self) -> None:
        try:
            os.write(self._write_fd, b"\0")
        except OSError:
            pass

    def send_bytes(self, data: bytes) -> None:
        self.events.append(("connection.send_bytes", data))
        self.calls.append(("send_bytes", data))
        message = json.loads(data.decode("utf-8"))
        self.sent_messages.append(message)
        self.send_event.set()
        is_ping = message["type"] == "ping"
        if not is_ping:
            self.request_event.set()
        if self.mode == "broken_pipe" and not is_ping:
            raise BrokenPipeError("scripted broken pipe")
        if self.mode == "startup_timeout":
            return
        if self.mode in {"timeout", "eof"} and not is_ping:
            if self.mode == "eof":
                self._signal_ready()
            return
        request_id = str(message["request_id"])
        if self.mode == "mismatch" and not is_ping:
            request_id = "00000000-0000-4000-8000-000000000003"
        if self.mode == "busy" and message["type"] == "execute":
            self._responses.append(
                _response(
                    request_id,
                    response_type="error",
                    code="worker_busy",
                    message="worker is already executing",
                )
            )
        elif message["type"] == "get_array":
            object_id = str(message["payload"]["object_id"])
            preview_stride = message["payload"]["preview_stride"]
            descriptor: dict[str, Any] = {
                "name": f"preview-{object_id}-stride-{preview_stride}",
                "dtype": "float64",
                "shape": [3],
                "nbytes": 24,
                "order": "C",
                "unit": "m",
            }
            for field, value in (self.descriptor_patch or {}).items():
                if value is _DROP_DESCRIPTOR_FIELD:
                    descriptor.pop(field, None)
                else:
                    descriptor[field] = value
            self._responses.append(
                _response(
                    request_id,
                    payload={
                        "descriptor": descriptor,
                        "unit": "m",
                    },
                )
            )
        else:
            payload = (
                {
                    "ready": True,
                    "request_id": request_id,
                    **(
                        {}
                        if self.mode == "missing_capability_snapshot"
                        else {"io_capabilities": _developer_capability_snapshot()}
                    ),
                }
                if is_ping
                else {"echo_request_id": request_id, "message_type": message["type"]}
            )
            self._responses.append(_response(request_id, payload=payload))
        if self.mode == "overlap" and not is_ping:
            self._overlap_waiting = True
            return
        self._signal_ready()

    def release_overlap_response(self) -> None:
        """Release one held response and make its OS handle readable."""
        assert self.mode == "overlap"
        self.release_response.set()
        if self._overlap_waiting:
            self._overlap_waiting = False
            self._signal_ready()

    def recv_bytes(self) -> bytes:
        self.events.append(("connection.recv_bytes", None))
        self.calls.append(("recv_bytes", None))
        try:
            os.read(self._read_fd, 1)
        except OSError as error:
            raise EOFError("scripted closed connection") from error
        if self.mode == "eof" and not self._responses:
            raise EOFError("scripted EOF")
        if not self._responses:
            raise EOFError("scripted empty response")
        response = self._responses.pop(0)
        self.received_messages.append(json.loads(response.decode("utf-8")))
        return response

    def poll(self, timeout: float | None = None) -> bool:
        self.events.append(("connection.poll", timeout))
        self.calls.append(("poll", timeout))
        if self.mode == "overlap" and self._overlap_waiting:
            if self.release_response.is_set():
                self.release_overlap_response()
        ready, _, _ = select.select([self._read_fd], [], [], timeout)
        return bool(ready)

    def close(self) -> None:
        self.events.append(("connection.close", None))
        self.calls.append(("close", None))
        self.closed = True
        for descriptor in (self._read_fd, self._write_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


class _ScriptedContext:
    def __init__(self, harness: _ClientHarness) -> None:
        self.harness = harness

    def Pipe(
        self, *args: Any, **kwargs: Any
    ) -> tuple[_ScriptedConnection, _ScriptedConnection]:
        del args, kwargs
        return self.harness.pipe_factory()

    def Process(self, *args: Any, **kwargs: Any) -> _ScriptedProcess:
        return self.harness.process_factory(*args, **kwargs)


class _ClientHarness:
    """Factory/context bundle used by every client behavior scenario."""

    def __init__(
        self,
        mode: str = "normal",
        *,
        descriptor_patch: dict[str, Any] | None = None,
    ) -> None:
        self.mode = mode
        self.descriptor_patch = descriptor_patch
        self.events: list[tuple[str, Any]] = []
        self.connections: list[_ScriptedConnection] = []
        self.worker_connections: list[_ScriptedConnection] = []
        self.processes: list[_ScriptedProcess] = []
        self.process_factory_callable = self.process_factory
        self.connection_factory_callable = self.connection_factory
        self.context = _ScriptedContext(self)

    def process_factory(self, *args: Any, **kwargs: Any) -> _ScriptedProcess:
        del args, kwargs
        process = _ScriptedProcess(self.events)
        self.processes.append(process)
        return process

    def pipe_factory(self) -> tuple[_ScriptedConnection, _ScriptedConnection]:
        client_endpoint = _ScriptedConnection(
            self.events, self.mode, descriptor_patch=self.descriptor_patch
        )
        worker_endpoint = _ScriptedConnection(self.events, "worker")
        self.connections.append(client_endpoint)
        self.worker_connections.append(worker_endpoint)
        return client_endpoint, worker_endpoint

    def connection_factory(self, *args: Any, **kwargs: Any) -> _ScriptedConnection:
        del args, kwargs
        return self.pipe_factory()[0]


def _worker_target(*_args: Any, **_kwargs: Any) -> None:
    return None


def _client(harness: _ClientHarness, **timeouts: float) -> WorkerClient:
    return WorkerClient(
        context=harness.context,
        process_factory=harness.process_factory_callable,
        connection_factory=harness.connection_factory_callable,
        worker_target=_worker_target,
        **timeouts,
    )


class _WaitSpy:
    """Callable wait seam that delegates to multiprocessing.connection.wait."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[Any], float | None]] = []

    def __call__(
        self,
        targets: Any,
        timeout: float | None = None,
    ) -> list[Any]:
        target_list = list(targets)
        self.calls.append((target_list, timeout))
        return list(multiprocessing_wait(target_list, timeout=timeout))


def _client_with_wait_spy(
    harness: _ClientHarness,
    wait_spy: _WaitSpy,
    **timeouts: float,
) -> tuple[WorkerClient, str | None]:
    """Inject a future wait seam without requiring poll-based behavior."""
    parameters = inspect.signature(WorkerClient.__init__).parameters
    kwargs: dict[str, Any] = {
        "context": harness.context,
        "process_factory": harness.process_factory_callable,
        "connection_factory": harness.connection_factory_callable,
        "worker_target": _worker_target,
        **timeouts,
    }
    if "wait_function" in parameters:
        kwargs["wait_function"] = wait_spy
        return WorkerClient(**kwargs), "wait_function"
    if "wait_factory" in parameters:
        kwargs["wait_factory"] = lambda: wait_spy
        return WorkerClient(**kwargs), "wait_factory"
    return _client(harness, **timeouts), None


def _start_for_component(client: WorkerClient) -> None:
    """Start a real fake-backed client, or seed RUNNING after the current sentinel."""
    try:
        client.start()
    except PrototypeNotImplementedError as error:
        _assert_sentinel(error, _START_OWNER)
        client._state = WorkerLifecycle.RUNNING


def _assert_result_response(
    result: Any,
    request_id: str = _REQUEST_ID,
    *,
    expected_payload: dict[str, Any] | None = None,
    received: dict[str, Any] | None = None,
) -> None:
    assert isinstance(result, dict)
    assert set(result) == {"protocol", "request_id", "type", "payload"}
    assert result["protocol"] == 2
    assert result["request_id"] == request_id
    assert result["type"] == "result"
    assert isinstance(result["payload"], dict)
    if expected_payload is not None:
        assert result["payload"] == expected_payload
    if received is not None:
        assert result == received


def _messages_of_type(
    connection: _ScriptedConnection, message_type: str
) -> list[dict[str, Any]]:
    return [
        message
        for message in connection.sent_messages
        if message.get("type") == message_type
    ]


@pytest.mark.contract("C-CLIENT-001")
def test_worker_lifecycle_has_exact_states() -> None:
    """The client exposes only the four approved lifecycle states."""
    assert set(WorkerLifecycle.__members__) == {
        "NEW",
        "RUNNING",
        "CRASHED",
        "CLOSED",
    }


@pytest.mark.contract("C-CLIENT-002")
def test_worker_client_defaults_are_sixty_three_hundred_and_five_seconds() -> None:
    """Startup, request, and join defaults are stable public contract values."""
    signature = inspect.signature(WorkerClient.__init__)
    assert signature.parameters["startup_timeout_s"].default == 60.0
    assert signature.parameters["request_timeout_s"].default == 300.0
    assert signature.parameters["join_timeout_s"].default == 5.0


@pytest.mark.contract("C-CLIENT-003")
def test_worker_client_retains_injected_context_target_and_factories() -> None:
    """The process boundary is injectable without importing a backend eagerly."""
    harness = _ClientHarness()
    client = _client(
        harness,
        startup_timeout_s=1.5,
        request_timeout_s=2.5,
        join_timeout_s=3.5,
    )

    assert client.context is harness.context
    assert client.process_factory is harness.process_factory_callable
    assert client.connection_factory is harness.connection_factory_callable
    assert client.worker_target is _worker_target
    assert client.startup_timeout_s == 1.5
    assert client.request_timeout_s == 2.5
    assert client.join_timeout_s == 3.5

    client_endpoint, worker_endpoint = harness.context.Pipe(duplex=True)
    process: _ScriptedProcess | None = None
    try:
        assert client_endpoint is not worker_endpoint
        assert isinstance(client_endpoint.fileno(), int)
        assert isinstance(worker_endpoint.fileno(), int)
        os.fstat(client_endpoint.fileno())
        os.fstat(worker_endpoint.fileno())
        process = harness.process_factory()
        os.fstat(process.sentinel)
        client_endpoint._signal_ready()
        assert client_endpoint in multiprocessing_wait(
            [client_endpoint, process.sentinel], timeout=0
        )
    finally:
        client_endpoint.close()
        worker_endpoint.close()
        if process is not None:
            process.close()


@pytest.mark.contract("C-CLIENT-004")
def test_worker_client_starts_in_new_state() -> None:
    """A newly constructed client has not started or closed a worker."""
    assert _client(_ClientHarness()).state is WorkerLifecycle.NEW


@pytest.mark.contract("C-CLIENT-005")
def test_request_signature_has_a_per_call_timeout_override() -> None:
    """Requests can override the configured timeout without changing defaults."""
    signature = inspect.signature(WorkerClient.request)
    assert signature.parameters["timeout_s"].default is None
    hints = get_type_hints(WorkerClient.request)
    assert hints["message"] is RequestEnvelope


@pytest.mark.contract("C-CLIENT-018")
def test_preview_stride_is_absent_from_science_export_and_save_boundaries() -> None:
    """The display ``[::n]`` option is not scientific, export, or save state."""
    assert "preview_stride" in inspect.signature(WorkerClient.get_array).parameters
    assert "preview_stride" not in inspect.signature(execute_operation).parameters
    assert "preview_stride" not in inspect.signature(export_python).parameters
    assert "preview_stride" not in inspect.signature(save_project).parameters


@pytest.mark.contract("C-CLIENT-006")
def test_start_performs_ping_handshake_and_enters_running_state() -> None:
    """Startup starts the fake process, sends ping, validates response, and runs."""
    harness = _ClientHarness()
    client = _client(harness)
    _invoke(_START_OWNER, client.start)
    assert client.state is WorkerLifecycle.RUNNING
    assert len(harness.processes) == 1
    assert harness.processes[0].lifecycle_calls == [("start", None)]
    assert len(harness.connections) == 1
    assert harness.connections[0].sent_messages[0]["type"] == "ping"
    assert harness.connections[0].sent_messages[0]["request_id"]
    ping_id = harness.connections[0].sent_messages[0]["request_id"]
    assert harness.connections[0].received_messages == [
        {
            "protocol": 2,
            "request_id": ping_id,
            "type": "result",
            "payload": {
                "ready": True,
                "request_id": ping_id,
                "io_capabilities": _developer_capability_snapshot(),
            },
        }
    ]
    assert client.capability_snapshot is not None
    assert client.capability_snapshot.mode == "developer"


@pytest.mark.contract("C-CLIENT-036")
def test_start_fails_closed_when_worker_omits_capability_snapshot() -> None:
    """Open/export cannot become available without worker capabilities."""
    from gwexpy_studio.errors import WorkerCrashedError

    client = _client(_ClientHarness("missing_capability_snapshot"))

    with pytest.raises(WorkerCrashedError, match="capability"):
        client.start()

    assert client.state is WorkerLifecycle.CRASHED
    assert client.capability_snapshot is None


@pytest.mark.contract("C-CLIENT-037")
def test_start_rejects_a_worker_snapshot_that_does_not_match_static_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A child that loses the frozen policy cannot silently enter source mode."""
    from gwexpy_studio.errors import WorkerCrashedError
    from gwexpy_studio.ops.io_capabilities import CAPABILITY_ENVIRONMENT

    policy = tmp_path / "io-capabilities.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "datatype": "TimeSeries",
                        "format": "csv",
                        "direction": "read",
                        "tier": "A",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, str(policy))
    client = _client(_ClientHarness())

    with pytest.raises(WorkerCrashedError, match="capability"):
        client.start()

    assert client.state is WorkerLifecycle.CRASHED
    assert client.capability_snapshot is None


@pytest.mark.contract("C-CLIENT-007")
def test_request_allows_only_one_in_flight_request() -> None:
    """A second request is rejected while the first controlled request waits."""
    harness = _ClientHarness("overlap")
    client = _client(harness, request_timeout_s=1.0)
    _start_for_component(client)
    first_result: list[Any] = []
    first_error: list[BaseException] = []

    def first_request() -> None:
        try:
            first_result.append(client.request(_execute_request()))
        except BaseException as error:
            first_error.append(error)

    thread = threading.Thread(target=first_request)
    thread.start()
    connection = harness.connections[-1] if harness.connections else None
    if connection is None or not connection.request_event.wait(0.2):
        thread.join()
        if first_error:
            raise first_error[0]
        pytest.fail("first request never reached the scripted connection")

    process = harness.processes[-1]
    probe = _ScriptedConnection(harness.events, "overlap")
    probe_request = json.dumps(
        _execute_request(),
        separators=(",", ":"),
    ).encode("utf-8")
    probe_targets = cast(Any, [probe, process.sentinel])
    try:
        probe.send_bytes(probe_request)
        assert probe not in multiprocessing_wait(probe_targets, timeout=0)
        probe.release_overlap_response()
        assert probe in multiprocessing_wait(probe_targets, timeout=0.2)
        probe.recv_bytes()
    finally:
        probe.close()
    assert thread.is_alive()
    assert not first_result
    assert not first_error

    second_result: list[Any] = []
    second_error: list[BaseException] = []

    def second_request() -> None:
        try:
            second_result.append(client.request(_execute_request()))
        except BaseException as error:
            second_error.append(error)

    second_thread = threading.Thread(target=second_request)
    second_thread.start()
    second_thread.join(timeout=0.2)
    if second_thread.is_alive():
        connection.release_overlap_response()
        second_thread.join(timeout=1.0)
        thread.join(timeout=1.0)
        pytest.fail("second request blocked instead of rejecting worker_busy")
    if second_result:
        pytest.fail("second in-flight request returned a result")
    assert second_error
    error = second_error[0]
    if isinstance(error, PrototypeNotImplementedError):
        _assert_sentinel(error, _REQUEST_OWNER)
        raise error
    assert isinstance(error, Exception)
    _require_coded_error(
        error,
        type_name="WorkerBusyError",
        code="worker_busy",
    )
    connection.release_overlap_response()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    if first_error:
        raise first_error[0]
    ping_messages = _messages_of_type(connection, "ping")
    execute_messages = _messages_of_type(connection, "execute")
    assert len(ping_messages) == 1
    assert len(execute_messages) == 1
    assert len(connection.sent_messages) == 2
    assert len(connection.received_messages) == 2
    assert (
        connection.received_messages[0]["request_id"] == ping_messages[0]["request_id"]
    )
    assert (
        connection.received_messages[1]["request_id"]
        == execute_messages[0]["request_id"]
    )
    _assert_result_response(
        first_result[0],
        received=connection.received_messages[-1],
    )
    assert client.state is WorkerLifecycle.RUNNING


@pytest.mark.contract("C-CLIENT-008")
def test_request_uses_the_per_call_timeout_override() -> None:
    """The exact timeout reaches wait(connection, process.sentinel)."""
    harness = _ClientHarness()
    wait_spy = _WaitSpy()
    client, wait_seam = _client_with_wait_spy(harness, wait_spy)
    _start_for_component(client)
    result = _invoke(
        _REQUEST_OWNER,
        lambda: client.request(_ping_request(), timeout_s=0.25),
    )
    _assert_result_response(
        result,
        received=harness.connections[-1].received_messages[-1],
    )
    assert ("recv_bytes", None) in harness.connections[-1].calls
    if wait_seam is None:
        pytest.fail(
            "WorkerClient.__init__ must declare "
            "wait_function(targets, timeout) or wait_factory() -> wait_function "
            "so the client can use multiprocessing.connection.wait without "
            "requiring connection.poll()."
        )
    request_waits = [
        (targets, timeout) for targets, timeout in wait_spy.calls if timeout == 0.25
    ]
    assert len(request_waits) == 1
    targets, timeout = request_waits[0]
    assert timeout == 0.25
    assert targets == [harness.connections[-1], harness.processes[-1].sentinel]


@pytest.mark.contract("C-CLIENT-009")
def test_request_timeout_terminates_joins_kills_and_joins_again() -> None:
    """A timeout orders terminate, join(5), kill, join(5), and marks CRASHED."""
    harness = _ClientHarness("timeout")
    client = _client(harness, request_timeout_s=0.25, join_timeout_s=5.0)
    _start_for_component(client)
    _expect_client_error(
        _REQUEST_OWNER,
        lambda: client.request(_execute_request(), timeout_s=0.25),
        type_name="WorkerTimeoutError",
        code="worker_timeout",
    )
    process = harness.processes[-1]
    assert process.lifecycle_calls[:5] == [
        ("start", None),
        ("terminate", None),
        ("join", 5.0),
        ("kill", None),
        ("join", 5.0),
    ]
    assert all(call[0] == "close" for call in process.lifecycle_calls[5:])
    assert client.state is WorkerLifecycle.CRASHED
    assert ("recv_bytes", None) not in harness.connections[-1].calls


@pytest.mark.contract("C-CLIENT-010")
def test_double_start_is_rejected() -> None:
    """Starting a running client raises a coded lifecycle error and does not respawn."""
    harness = _ClientHarness()
    client = _client(harness)
    _start_for_component(client)
    _expect_client_error(
        _START_OWNER,
        client.start,
        type_name="WorkerLifecycleError",
        code="worker_already_started",
    )
    assert len(harness.processes) == 1
    assert client.state is WorkerLifecycle.RUNNING


@pytest.mark.contract("C-CLIENT-011")
def test_shutdown_is_idempotent_and_closes_children() -> None:
    """Shutdown closes connection/child once, reaches CLOSED, and repeats safely."""
    harness = _ClientHarness()
    client = _client(harness)
    _start_for_component(client)
    _invoke(_SHUTDOWN_OWNER, client.shutdown)
    _invoke(_SHUTDOWN_OWNER, client.shutdown)
    assert client.state is WorkerLifecycle.CLOSED
    if harness.connections:
        assert harness.connections[-1].closed
        assert harness.connections[-1].sent_messages[-1]["type"] == "shutdown"
        assert [
            message["type"] for message in harness.connections[-1].sent_messages
        ].count("shutdown") == 1
        assert (
            harness.connections[-1].received_messages[-1]["request_id"]
            == harness.connections[-1].sent_messages[-1]["request_id"]
        )
    if harness.processes:
        assert harness.processes[-1].alive is False
        assert [call[0] for call in harness.processes[-1].lifecycle_calls].count(
            "start"
        ) == 1


@pytest.mark.contract("C-CLIENT-012")
def test_restart_recovers_a_crashed_worker() -> None:
    """Restart cleans a crashed fake child, handshakes a new child, and runs."""
    harness = _ClientHarness()
    client = _client(harness)
    _start_for_component(client)
    if harness.processes:
        old_process = harness.processes[0]
        old_process.alive = False
        old_process.exitcode = 1
        client._state = WorkerLifecycle.CRASHED
    else:
        client._state = WorkerLifecycle.CRASHED

    _invoke(_RESTART_OWNER, client.restart)
    assert client.state is WorkerLifecycle.RUNNING
    if harness.processes:
        assert len(harness.processes) == 2
        assert harness.processes[0].alive is False
        assert harness.connections[0].closed
        assert harness.processes[1].lifecycle_calls == [("start", None)]
        assert harness.connections[1].sent_messages[0]["type"] == "ping"
        assert (
            harness.connections[1].received_messages[0]["request_id"]
            == harness.connections[1].sent_messages[0]["request_id"]
        )


@pytest.mark.contract("C-CLIENT-013")
def test_get_array_accepts_display_only_preview_stride() -> None:
    """Array retrieval sends preview_stride and returns a typed descriptor result."""
    harness = _ClientHarness()
    client = _client(harness)
    _start_for_component(client)
    result = _invoke(
        _GET_ARRAY_OWNER,
        lambda: client.get_array("obj-1", preview_stride=4),
    )
    assert isinstance(result, dict)
    assert set(result) == {"descriptor", "unit"}
    descriptor = result["descriptor"]
    assert isinstance(descriptor, SharedMemoryDescriptor)
    assert descriptor.name == "preview-obj-1-stride-4"
    assert descriptor.dtype == "float64"
    assert descriptor.shape == (3,)
    assert descriptor.nbytes == 24
    assert descriptor.order == "C"
    assert descriptor.unit == "m"
    request = harness.connections[-1].sent_messages[-1]
    assert request["type"] == "get_array"
    assert request["payload"]["preview"] is True
    assert request["payload"]["preview_stride"] == 4
    received = harness.connections[-1].received_messages[-1]
    assert received["request_id"] == request["request_id"]
    received_descriptor = received["payload"]["descriptor"]
    assert descriptor.name == received_descriptor["name"]
    assert descriptor.dtype == received_descriptor["dtype"]
    assert list(descriptor.shape) == received_descriptor["shape"]
    assert descriptor.nbytes == received_descriptor["nbytes"]
    assert descriptor.order == received_descriptor["order"]
    assert descriptor.unit == received_descriptor["unit"]
    assert result["unit"] == received["payload"]["unit"]
    for invalid_stride in (0, -1, True, 1.5):
        _expect_client_error(
            _GET_ARRAY_OWNER,
            cast(
                Callable[[], Any],
                lambda invalid_stride=invalid_stride: client.get_array(
                    "obj-1", preview_stride=cast(Any, invalid_stride)
                ),
            ),
            type_name="ProtocolValidationError",
            code="invalid_payload",
        )


@pytest.mark.contract("C-CLIENT-022")
def test_get_array_rejects_a_mistyped_descriptor_instead_of_coercing_it() -> None:
    """Descriptor fields are type-checked, never coerced, before use.

    ``get_array`` used to build its ``SharedMemoryDescriptor`` with
    ``str(...)``/``int(...)`` around each field, so a malformed worker response
    was silently repaired into a plausible-looking descriptor instead of being
    rejected: ``int("800")`` accepted a stringified size, ``nbytes=True``
    became ``1`` (``bool`` is an ``int`` subclass, so a bare
    ``isinstance(x, int)`` guard still lets it through), ``dtype=4`` became
    ``"4"``, and a missing key escaped as a raw ``KeyError`` rather than a
    coded boundary error. A worker response is untrusted control-plane input
    (ADR-0012) and this descriptor is the sole source of truth for a
    shared-memory copy-out, so every one of these must fail closed here --
    before any block is opened -- with the same ``shm_invalid_descriptor``
    code the shared-memory layer uses.
    """
    control = _ClientHarness()
    control_client = _client(control)
    _start_for_component(control_client)
    control_result = _invoke(
        _GET_ARRAY_OWNER,
        lambda: control_client.get_array("obj-control", preview_stride=4),
    )
    control_descriptor = control_result["descriptor"]
    assert isinstance(control_descriptor, SharedMemoryDescriptor)
    assert control_descriptor.shape == (3,)
    assert control_descriptor.nbytes == 24

    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        ("bool-shape-entry", {"shape": [True]}),
        ("bool-among-int-shape-entries", {"shape": [3, False]}),
        ("non-sequence-shape", {"shape": "3"}),
        ("negative-shape-entry", {"shape": [-3]}),
        ("stringified-nbytes", {"nbytes": "800"}),
        ("bool-nbytes", {"nbytes": True}),
        ("float-nbytes", {"nbytes": 24.0}),
        ("negative-nbytes", {"nbytes": -24}),
        ("non-string-name", {"name": 7}),
        ("non-string-dtype", {"dtype": 4}),
        ("non-string-order", {"order": 1}),
        ("non-string-unit", {"unit": 1}),
        ("missing-name", {"name": _DROP_DESCRIPTOR_FIELD}),
        ("missing-shape", {"shape": _DROP_DESCRIPTOR_FIELD}),
        ("missing-dtype", {"dtype": _DROP_DESCRIPTOR_FIELD}),
        ("missing-nbytes", {"nbytes": _DROP_DESCRIPTOR_FIELD}),
    )
    for case_id, descriptor_patch in cases:
        harness = _ClientHarness(descriptor_patch=descriptor_patch)
        client = _client(harness)
        _start_for_component(client)
        # The case id travels in the object_id so a regression names itself:
        # it comes back inside the scripted descriptor's `name` field.
        _expect_client_error(
            _GET_ARRAY_OWNER,
            cast(
                Callable[[], Any],
                lambda case_id=case_id: client.get_array(
                    f"obj-{case_id}", preview_stride=4
                ),
            ),
            type_name="SharedMemoryError",
            code="shm_invalid_descriptor",
        )


@pytest.mark.contract("C-CLIENT-014")
def test_request_reports_worker_busy_without_second_execution() -> None:
    """A server busy response is correlated and only one execute is sent."""
    harness = _ClientHarness("busy")
    client = _client(harness)
    _start_for_component(client)
    result = _invoke(_REQUEST_OWNER, lambda: client.request(_execute_request()))
    assert isinstance(result, dict)
    assert set(result) == {
        "protocol",
        "request_id",
        "type",
        "code",
        "message",
    }
    assert result["protocol"] == 2
    assert result["request_id"] == _REQUEST_ID
    assert result["type"] == "error"
    assert result["code"] == "worker_busy"
    connection = harness.connections[-1]
    ping_messages = _messages_of_type(connection, "ping")
    execute_messages = _messages_of_type(connection, "execute")
    assert len(ping_messages) == 1
    assert len(execute_messages) == 1
    assert len(connection.sent_messages) == 2
    assert len(connection.received_messages) == 2
    assert (
        connection.received_messages[0]["request_id"] == ping_messages[0]["request_id"]
    )
    assert (
        connection.received_messages[1]["request_id"]
        == execute_messages[0]["request_id"]
    )
    assert result == connection.received_messages[-1]


@pytest.mark.contract("C-CLIENT-015")
def test_request_translates_eof_as_a_worker_boundary_failure() -> None:
    """Unexpected EOF becomes worker_crashed and marks the client CRASHED."""
    harness = _ClientHarness("eof")
    client = _client(harness)
    _start_for_component(client)
    _expect_client_error(
        _REQUEST_OWNER,
        lambda: client.request(_execute_request()),
        type_name="WorkerCrashedError",
        code="worker_crashed",
    )
    assert client.state is WorkerLifecycle.CRASHED


@pytest.mark.contract("C-CLIENT-016")
def test_request_translates_broken_pipe_as_a_worker_boundary_failure() -> None:
    """A broken control pipe becomes worker_crashed without silent success."""
    harness = _ClientHarness("broken_pipe")
    client = _client(harness)
    _start_for_component(client)
    _expect_client_error(
        _REQUEST_OWNER,
        lambda: client.request(_execute_request()),
        type_name="WorkerCrashedError",
        code="worker_crashed",
    )
    assert client.state is WorkerLifecycle.CRASHED


@pytest.mark.contract("C-CLIENT-017")
def test_shutdown_after_a_kill_cleans_up_process_and_connection() -> None:
    """Shutdown after a killed fake child closes all remaining client resources."""
    harness = _ClientHarness()
    client = _client(harness)
    _start_for_component(client)
    if harness.processes:
        process = harness.processes[-1]
        process.kill()
        client._state = WorkerLifecycle.CRASHED
    else:
        client._state = WorkerLifecycle.CRASHED
    _invoke(_SHUTDOWN_OWNER, client.shutdown)
    assert client.state is WorkerLifecycle.CLOSED
    if harness.connections:
        assert harness.connections[-1].closed


@pytest.mark.contract("C-CLIENT-019")
def test_request_rejects_a_response_request_id_mismatch() -> None:
    """A response for another request returns request_id_mismatch."""
    harness = _ClientHarness("mismatch")
    client = _client(harness)
    _start_for_component(client)
    _expect_client_error(
        _REQUEST_OWNER,
        lambda: client.request(_execute_request()),
        type_name="ProtocolValidationError",
        code="request_id_mismatch",
    )
    assert client.state is WorkerLifecycle.RUNNING
    connection = harness.connections[-1]
    ping_messages = _messages_of_type(connection, "ping")
    execute_messages = _messages_of_type(connection, "execute")
    assert len(ping_messages) == 1
    assert len(execute_messages) == 1
    assert len(connection.sent_messages) == 2
    assert len(connection.received_messages) == 2
    assert (
        connection.received_messages[0]["request_id"] == ping_messages[0]["request_id"]
    )
    assert (
        connection.received_messages[-1]["request_id"]
        != execute_messages[0]["request_id"]
    )


@pytest.mark.contract("C-CLIENT-020")
def test_start_rejects_a_startup_timeout_and_cleans_up() -> None:
    """A missing ping response uses startup timeout cleanup and CRASHED state."""
    harness = _ClientHarness("startup_timeout")
    client = _client(harness, startup_timeout_s=0.25, join_timeout_s=5.0)
    _expect_client_error(
        _START_OWNER,
        client.start,
        type_name="WorkerTimeoutError",
        code="worker_timeout",
    )
    process = harness.processes[-1]
    assert process.lifecycle_calls[:5] == [
        ("start", None),
        ("terminate", None),
        ("join", 5.0),
        ("kill", None),
        ("join", 5.0),
    ]
    assert all(call[0] == "close" for call in process.lifecycle_calls[5:])
    assert client.state is WorkerLifecycle.CRASHED


@pytest.mark.contract("C-CLIENT-021")
def test_request_preserves_graph_and_provenance_payload_boundary() -> None:
    """The client forwards stable graph/provenance IDs without mutating them."""
    harness = _ClientHarness()
    client = _client(harness)
    _start_for_component(client)
    payload = {
        "operation": "timeseries.detrend",
        "graph_id": "graph-1",
        "provenance": {
            "operation_id": "op-1",
            "input_object_ids": ["obj-source"],
        },
    }
    message = cast(
        RequestEnvelope,
        {
            "protocol": 2,
            "request_id": UUID4(_REQUEST_ID),
            "type": "execute",
            "payload": payload,
        },
    )
    result = _invoke(_REQUEST_OWNER, lambda: client.request(message))
    _assert_result_response(
        result,
        expected_payload={
            "echo_request_id": _REQUEST_ID,
            "message_type": "execute",
        },
        received=harness.connections[-1].received_messages[-1],
    )
    sent = harness.connections[-1].sent_messages[-1]
    assert sent["request_id"] == _REQUEST_ID
    assert sent["payload"] == payload
    assert harness.connections[-1].received_messages[-1]["request_id"] == _REQUEST_ID
    assert client.state is WorkerLifecycle.RUNNING
