"""Absolute-deadline contracts for the worker client transport."""

from __future__ import annotations

import inspect
import json
import math
import threading
import time
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from gwexpy_studio.errors import WorkerTimeoutError
from gwexpy_studio.worker.client import WaitFunction, WorkerClient, WorkerLifecycle
from gwexpy_studio.worker.protocol import UUID4, RequestEnvelope


def _capability_document() -> dict[str, object]:
    """Return a valid, probe-free capability handshake for this test double."""
    from gwexpy_studio.ops.io_capabilities import (
        load_capability_manifest,
        unprobed_effective_capabilities,
    )

    return unprobed_effective_capabilities(
        load_capability_manifest()
    ).document()


def _request() -> RequestEnvelope:
    return {
        "protocol": 2,
        "request_id": UUID4(str(uuid.uuid4())),
        "type": "ping",
        "payload": {},
    }


class _Connection:
    """Script correlated responses and optionally block selected sends."""

    def __init__(self) -> None:
        self.block_types: set[str] = set()
        self.closed = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.responses: list[bytes] = []
        self.send_events: list[tuple[float, str]] = []

    def send_bytes(self, data: bytes) -> None:
        message = json.loads(data.decode("utf-8"))
        message_type = str(message["type"])
        self.send_events.append((time.monotonic(), message_type))
        if message_type in self.block_types:
            self.entered.set()
            self.release.wait(5.0)
            if self.closed:
                raise OSError("connection closed during blocked send")
        if message_type == "shutdown":
            self.responses.append(b"shutdown-ack")
            return
        payload: dict[str, object] = {}
        if message_type == "ping":
            payload["io_capabilities"] = _capability_document()
        response = {
            "protocol": 2,
            "request_id": message["request_id"],
            "type": "result",
            "payload": payload,
        }
        self.responses.append(
            json.dumps(response, separators=(",", ":")).encode("utf-8")
        )

    def recv_bytes(self) -> bytes:
        assert self.responses
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True
        self.release.set()


class _Process:
    """Record cleanup order and each process-wait budget."""

    def __init__(self) -> None:
        self.sentinel = object()
        self.alive = False
        self.events: list[str] = []
        self.joins: list[tuple[float, float | None]] = []

    def start(self) -> None:
        self.alive = True
        self.events.append("start")

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.events.append("terminate")

    def join(self, timeout: float | None = None) -> None:
        self.events.append("join")
        self.joins.append((time.monotonic(), timeout))
        if timeout and timeout > 0:
            time.sleep(min(timeout, 0.005))

    def kill(self) -> None:
        self.events.append("kill")
        self.alive = False

    def close(self) -> None:
        self.events.append("close")


class _RecordingClient(WorkerClient):
    """Retain existing private seams while observing receive budgets."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.receive_budgets: list[tuple[float, float]] = []

    def _recv_frame_within(self, conn: Any, timeout_s: float) -> bytes:
        self.receive_budgets.append((time.monotonic(), timeout_s))
        return super()._recv_frame_within(conn, timeout_s)


class _Harness:
    """Build one complete injected worker-client boundary."""

    def __init__(self) -> None:
        self.connection = _Connection()
        self.process = _Process()
        self.waits: list[tuple[float, float | None]] = []

    def process_factory(self, *, target: Any, args: Sequence[Any]) -> _Process:
        del target, args
        return self.process

    def wait(
        self, targets: Sequence[Any], timeout: float | None = None
    ) -> Sequence[Any]:
        del targets
        self.waits.append((time.monotonic(), timeout))
        return [self.connection] if self.connection.responses else []

    def client(self) -> _RecordingClient:
        return _RecordingClient(
            process_factory=self.process_factory,
            connection_factory=lambda: self.connection,
            wait_function=self.wait,
            startup_timeout_s=60.0,
            request_timeout_s=60.0,
            join_timeout_s=5.0,
        )


def _assert_budget_uses_deadline(
    observed_at: float, budget: float | None, deadline: float
) -> None:
    assert budget is not None
    assert 0.0 <= budget <= max(deadline - observed_at, 0.0) + 0.015


def _run_blocked(
    action: Any,
    connection: _Connection,
) -> tuple[BaseException | None, float]:
    outcome: list[BaseException] = []

    def invoke() -> None:
        try:
            action()
        except BaseException as exc:
            outcome.append(exc)

    started = time.monotonic()
    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    try:
        assert connection.entered.wait(0.25), "send did not enter its blocking seam"
        thread.join(0.5)
        assert not thread.is_alive(), "deadline did not return from blocked send"
    finally:
        connection.release.set()
        thread.join(0.5)
    return (outcome[0] if outcome else None, time.monotonic() - started)


@pytest.mark.contract("C-CLIENT-023")
def test_deadline_scope_is_nested_and_bounds_start_and_request_transport() -> None:
    """One active deadline bounds transport and restores nested context safely."""
    assert list(inspect.signature(WorkerClient.start).parameters) == ["self"]
    assert list(inspect.signature(WorkerClient.shutdown).parameters) == ["self"]
    assert list(inspect.signature(WorkerClient.request).parameters) == [
        "self",
        "message",
        "timeout_s",
    ]
    assert WaitFunction.__name__ == "WaitFunction"

    scoped_harness = _Harness()
    scoped = scoped_harness.client()
    outer_deadline = time.monotonic() + 1.0
    earlier_deadline = time.monotonic() + 0.4
    later_deadline = time.monotonic() + 2.0

    class _SyntheticExit(BaseException):
        pass

    with scoped.deadline_scope(outer_deadline):
        scoped.start()
        with scoped.deadline_scope(later_deadline):
            scoped.request(_request(), timeout_s=30.0)
        with scoped.deadline_scope(earlier_deadline):
            scoped.request(_request(), timeout_s=30.0)
        with pytest.raises(_SyntheticExit):
            with scoped.deadline_scope(time.monotonic() + 0.2):
                raise _SyntheticExit
        scoped.request(_request(), timeout_s=30.0)

    assert len(scoped_harness.waits) == 4
    for index, (observed_at, budget) in enumerate(scoped_harness.waits):
        expected = earlier_deadline if index == 2 else outer_deadline
        _assert_budget_uses_deadline(observed_at, budget, expected)
    for index, (observed_at, budget) in enumerate(scoped.receive_budgets):
        expected = earlier_deadline if index == 2 else outer_deadline
        _assert_budget_uses_deadline(observed_at, budget, expected)

    scoped_harness.waits.clear()
    scoped.request(_request(), timeout_s=0.25)
    assert scoped_harness.waits == [(scoped_harness.waits[0][0], 0.25)]
    scoped.shutdown()

    for invalid in (0.0, -1.0, math.inf, math.nan, True):
        with pytest.raises((TypeError, ValueError)):
            with scoped.deadline_scope(invalid):  # type: ignore[arg-type]
                pass

    for block_type, action_name in (("ping", "start"), ("ping", "request")):
        blocked_harness = _Harness()
        blocked = blocked_harness.client()
        if action_name == "request":
            blocked.start()
            blocked_harness.connection.block_types.add(block_type)
        else:
            blocked_harness.connection.block_types.add(block_type)

        def action() -> None:
            with blocked.deadline_scope(time.monotonic() + 0.05):
                if action_name == "start":
                    blocked.start()
                else:
                    blocked.request(_request())

        error, elapsed = _run_blocked(action, blocked_harness.connection)
        assert isinstance(error, WorkerTimeoutError)
        assert error.code == "worker_timeout"
        assert elapsed < 0.5
        assert blocked.state is WorkerLifecycle.CRASHED
        assert blocked_harness.connection.closed

    retry_harness = _Harness()
    retry_client = retry_harness.client()
    retry_calls: list[tuple[float, float]] = []

    def positional_then_keyword(
        targets: Sequence[Any], *args: float, **kwargs: float
    ) -> Sequence[Any]:
        del targets
        budget = args[0] if args else kwargs["timeout"]
        retry_calls.append((time.monotonic(), budget))
        if args:
            time.sleep(0.06)
            raise TypeError("keyword-only timeout")
        return [retry_harness.connection]

    retry_client.wait_function = positional_then_keyword
    retry_deadline = time.monotonic() + 0.05
    with pytest.raises(WorkerTimeoutError):
        with retry_client.deadline_scope(retry_deadline):
            retry_client.start()
    assert len(retry_calls) == 2
    _assert_budget_uses_deadline(*retry_calls[1], retry_deadline)


@pytest.mark.contract("C-CLIENT-024")
def test_shutdown_and_cleanup_consume_one_active_deadline() -> None:
    """Shutdown send, ack, and both joins never reset an active deadline."""
    harness = _Harness()
    client = harness.client()
    client.start()
    harness.waits.clear()
    client.receive_budgets.clear()
    deadline = time.monotonic() + 0.2
    with client.deadline_scope(deadline):
        client.shutdown()

    assert client.state is WorkerLifecycle.CLOSED
    assert len(harness.waits) == 1
    assert len(client.receive_budgets) == 1
    assert len(harness.process.joins) == 2
    observed = [
        harness.waits[0],
        client.receive_budgets[0],
        *harness.process.joins,
    ]
    for observed_at, budget in observed:
        _assert_budget_uses_deadline(observed_at, budget, deadline)
    budgets = [float(budget) for _at, budget in observed if budget is not None]
    assert budgets == sorted(budgets, reverse=True)
    assert harness.process.events[:6] == [
        "start",
        "terminate",
        "join",
        "kill",
        "join",
        "close",
    ]

    blocked_harness = _Harness()
    blocked = blocked_harness.client()
    blocked.start()
    blocked_harness.connection.block_types.add("shutdown")

    def blocked_shutdown() -> None:
        with blocked.deadline_scope(time.monotonic() + 0.05):
            blocked.shutdown()

    error, elapsed = _run_blocked(blocked_shutdown, blocked_harness.connection)
    assert error is None
    assert elapsed < 0.5
    assert blocked.state is WorkerLifecycle.CLOSED
    assert blocked_harness.connection.closed
