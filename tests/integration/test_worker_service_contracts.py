"""Component-boundary contracts for the isolated worker service loop."""

from __future__ import annotations

import copy
import inspect
import json
from typing import Any

import pytest

from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.worker import service as service_module
from gwexpy_studio.worker.service import worker_main

pytestmark = pytest.mark.integration

_FOUNDATION_CODE = "STUDIO-FOUNDATION-NOT-IMPLEMENTED"
_SERVICE_OWNER = "gwexpy_studio.worker.service.worker_main"


def _assert_sentinel(error: PrototypeNotImplementedError) -> None:
    assert type(error) is PrototypeNotImplementedError
    assert error.code == _FOUNDATION_CODE
    assert error.owner == _SERVICE_OWNER


def _request(request_id: str, message_type: str, payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "protocol": 2,
            "request_id": request_id,
            "type": message_type,
            "payload": payload,
        },
        separators=(",", ":"),
    ).encode("utf-8")


class _RegistryFake:
    """Deterministic object registry whose graph snapshot must remain untouched."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {
            "obj-source": {
                "graph_id": "graph-1",
                "operation_id": "op-source",
                "provenance": {"source": "file:///tmp/source.hdf5"},
            },
            "obj-derived": {
                "graph_id": "graph-1",
                "operation_id": "op-1",
                "provenance": {"input_object_ids": ["obj-source"]},
            },
        }
        self.before = copy.deepcopy(self.objects)
        self.calls: list[tuple[str, Any]] = []

    def list_objects(self) -> list[str]:
        self.calls.append(("list_objects", None))
        return sorted(self.objects)

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("execute", copy.deepcopy(payload)))
        return {
            "object_id": payload["output_object_id"],
            "graph_id": payload["graph_id"],
            "operation_id": payload["operation_id"],
        }

    def delete_object(self, object_id: str) -> None:
        self.calls.append(("delete_object", object_id))
        self.objects.pop(object_id, None)


class _StoreFake:
    """Deterministic pending-SHM store for release and shutdown cleanup."""

    def __init__(self) -> None:
        self.pending = {"shm-1", "shm-2"}
        self.calls: list[tuple[str, Any]] = []

    def release_shm(self, name: str) -> None:
        self.calls.append(("release_shm", name))
        self.pending.discard(name)

    def cleanup(self) -> None:
        self.calls.append(("cleanup", None))
        self.pending.clear()


class _ScriptedConnection:
    """Scripted connection for worker_main component tests."""

    def __init__(self, requests: list[bytes]) -> None:
        self.requests = list(requests)
        self.sent: list[bytes] = []
        self.closed = False

    def recv_bytes(self) -> bytes:
        if not self.requests:
            raise EOFError("script exhausted before worker shutdown")
        return self.requests.pop(0)

    def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


def _scenario(
    name: str,
) -> tuple[_ScriptedConnection, list[str], _RegistryFake, _StoreFake]:
    registry = _RegistryFake()
    store = _StoreFake()
    if name == "ping-and-shutdown":
        ids = [
            "00000000-0000-4000-8000-000000000101",
            "00000000-0000-4000-8000-000000000102",
        ]
        requests = [
            _request(ids[0], "ping", {}),
            _request(ids[1], "shutdown", {}),
        ]
    elif name == "dispatch-error-loop":
        ids = [
            "00000000-0000-4000-8000-000000000103",
            "00000000-0000-4000-8000-000000000104",
            "00000000-0000-4000-8000-000000000105",
        ]
        requests = [
            _request(ids[0], "execute", {"unknown": True}),
            _request(ids[1], "ping", {}),
            _request(ids[2], "shutdown", {}),
        ]
    elif name == "stable-id-boundary":
        ids = [
            "00000000-0000-4000-8000-000000000106",
            "00000000-0000-4000-8000-000000000107",
        ]
        requests = [
            _request(
                ids[0],
                "execute",
                {
                    "graph_id": "graph-1",
                    "operation_id": "op-1",
                    "input_object_ids": ["obj-source"],
                    "output_object_id": "obj-derived",
                },
            ),
            _request(ids[1], "shutdown", {}),
        ]
    elif name == "release-cleanup":
        ids = [
            "00000000-0000-4000-8000-000000000108",
            "00000000-0000-4000-8000-000000000109",
        ]
        requests = [
            _request(ids[0], "release_shm", {"name": "shm-1"}),
            _request(ids[1], "shutdown", {}),
        ]
    elif name == "delete-cleanup":
        ids = [
            "00000000-0000-4000-8000-000000000110",
            "00000000-0000-4000-8000-000000000111",
        ]
        requests = [
            _request(ids[0], "delete_object", {"object_id": "obj-derived"}),
            _request(ids[1], "shutdown", {}),
        ]
    elif name == "list-preserves-graph":
        ids = [
            "00000000-0000-4000-8000-000000000112",
            "00000000-0000-4000-8000-000000000113",
        ]
        requests = [
            _request(ids[0], "list_objects", {}),
            _request(ids[1], "shutdown", {}),
        ]
    elif name == "shutdown-cleanup":
        ids = ["00000000-0000-4000-8000-000000000114"]
        requests = [_request(ids[0], "shutdown", {})]
    else:
        raise AssertionError(f"unknown worker service scenario: {name}")
    return _ScriptedConnection(requests), ids, registry, store


def _run_worker_main_with_dependencies(
    connection: _ScriptedConnection,
    registry: _RegistryFake,
    store: _StoreFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use only a declared service seam; never attach hidden deps to a pipe."""
    signature = inspect.signature(service_module.worker_main)
    if tuple(signature.parameters) != ("connection",):
        pytest.fail(
            "Preserve the spawn-compatible worker_main(connection) signature and "
            "declare module-level "
            "create_registry()/create_store() factories instead of keyword "
            "dependency parameters."
        )

    registry_factory = getattr(service_module, "create_registry", None)
    store_factory = getattr(service_module, "create_store", None)
    if callable(registry_factory) and callable(store_factory):
        monkeypatch.setattr(
            service_module,
            "create_registry",
            lambda *_args, **_kwargs: registry,
        )
        monkeypatch.setattr(
            service_module,
            "create_store",
            lambda *_args, **_kwargs: store,
        )
        service_module.worker_main(connection)
        return

    try:
        service_module.worker_main(connection)
    except PrototypeNotImplementedError as error:
        _assert_sentinel(error)
        raise
    pytest.fail(
        "Declare module-level create_registry() and create_store() factories for "
        "deterministic "
        "component dependency injection while keeping worker_main(connection) "
        "spawn-compatible."
    )


def _assert_response_envelope(
    value: dict[str, Any],
    *,
    request_id: str,
    expected_type: str,
    expected_code: str | None = None,
) -> None:
    assert value["protocol"] == 2
    assert value["request_id"] == request_id
    assert value["type"] == expected_type
    if expected_type == "result":
        assert set(value) == {"protocol", "request_id", "type", "payload"}
        assert isinstance(value["payload"], dict)
    else:
        assert set(value) == {
            "protocol",
            "request_id",
            "type",
            "code",
            "message",
        }
        assert isinstance(value["message"], str) and value["message"]
        assert expected_code is not None
        assert value["code"] == expected_code


@pytest.mark.contract("C-SERVICE-001")
def test_worker_main_is_a_top_level_spawn_entrypoint() -> None:
    """The service entry point remains importable by a future spawn adapter."""
    signature = inspect.signature(worker_main)
    assert tuple(signature.parameters) == ("connection",)
    assert worker_main.__module__ == "gwexpy_studio.worker.service"


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            "ping-and-shutdown",
            id="ping-and-shutdown",
            marks=pytest.mark.contract("C-SERVICE-002"),
        ),
        pytest.param(
            "dispatch-error-loop",
            id="dispatch-error-loop",
            marks=pytest.mark.contract("C-SERVICE-003"),
        ),
        pytest.param(
            "stable-id-boundary",
            id="stable-id-boundary",
            marks=pytest.mark.contract("C-SERVICE-004"),
        ),
        pytest.param(
            "release-cleanup",
            id="release-cleanup",
            marks=pytest.mark.contract("C-SERVICE-005"),
        ),
        pytest.param(
            "delete-cleanup",
            id="delete-cleanup",
            marks=pytest.mark.contract("C-SERVICE-006"),
        ),
        pytest.param(
            "list-preserves-graph",
            id="list-preserves-graph",
            marks=pytest.mark.contract("C-SERVICE-007"),
        ),
        pytest.param(
            "shutdown-cleanup",
            id="shutdown-cleanup",
            marks=pytest.mark.contract("C-SERVICE-008"),
        ),
    ],
)
def test_worker_service_dispatches_scripted_component_scenario(
    scenario: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scripted dispatch returns envelopes, survives errors, and cleans state."""
    connection, request_ids, registry, store = _scenario(scenario)
    assert not hasattr(connection, "registry")
    assert not hasattr(connection, "store")
    try:
        _run_worker_main_with_dependencies(
            connection,
            registry,
            store,
            monkeypatch,
        )
    except PrototypeNotImplementedError as error:
        _assert_sentinel(error)
        raise

    assert connection.closed
    responses = [json.loads(data.decode("utf-8")) for data in connection.sent]
    assert len(responses) == len(request_ids)
    expected_responses: dict[str, list[tuple[str, str | None]]] = {
        "ping-and-shutdown": [("result", None), ("result", None)],
        "dispatch-error-loop": [
            ("error", "invalid_payload"),
            ("result", None),
            ("result", None),
        ],
        "stable-id-boundary": [("result", None), ("result", None)],
        "release-cleanup": [("result", None), ("result", None)],
        "delete-cleanup": [("result", None), ("result", None)],
        "list-preserves-graph": [("result", None), ("result", None)],
        "shutdown-cleanup": [("result", None)],
    }
    for response, request_id, expected in zip(
        responses, request_ids, expected_responses[scenario], strict=True
    ):
        _assert_response_envelope(
            response,
            request_id=request_id,
            expected_type=expected[0],
            expected_code=expected[1],
        )

    if scenario == "dispatch-error-loop":
        assert responses[0]["type"] == "error"
        assert responses[0]["code"] == "invalid_payload"
        assert responses[1]["type"] == "result"
        assert responses[1]["payload"]
    if scenario == "ping-and-shutdown":
        assert responses[0]["payload"]
    if scenario == "stable-id-boundary":
        assert responses[0]["payload"] == {
            "object_id": "obj-derived",
            "graph_id": "graph-1",
            "operation_id": "op-1",
        }
        assert registry.objects == registry.before
        assert registry.calls == [
            (
                "execute",
                {
                    "graph_id": "graph-1",
                    "operation_id": "op-1",
                    "input_object_ids": ["obj-source"],
                    "output_object_id": "obj-derived",
                },
            )
        ]
        assert set(responses[0]["payload"]) == {
            "object_id",
            "graph_id",
            "operation_id",
        }
    if scenario == "release-cleanup":
        assert store.calls == [
            ("release_shm", "shm-1"),
            ("cleanup", None),
        ]
    if scenario == "delete-cleanup":
        assert registry.calls == [("delete_object", "obj-derived")]
        assert "obj-derived" not in registry.objects
    if scenario == "list-preserves-graph":
        assert registry.calls == [("list_objects", None)]
        assert registry.objects == registry.before
        assert responses[0]["payload"]
    if scenario == "shutdown-cleanup":
        assert store.pending == set()


@pytest.mark.contract("C-SERVICE-015")
def test_worker_ping_returns_a_validated_effective_capability_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI receives worker facts only through the post-bootstrap ping response."""
    from gwexpy_studio.ops.io_capabilities import EffectiveCapabilitySnapshot

    connection, _ids, registry, store = _scenario("ping-and-shutdown")
    _run_worker_main_with_dependencies(connection, registry, store, monkeypatch)
    ping = json.loads(connection.sent[0].decode("utf-8"))

    snapshot = EffectiveCapabilitySnapshot.from_document(
        ping["payload"]["io_capabilities"]
    )
    assert snapshot.mode == "developer"
    assert snapshot.entries == ()


@pytest.mark.contract("C-SERVICE-016")
def test_worker_bootstrap_converts_unexpected_probe_failure_to_safe_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken optional probe does not expose its exception or bypass frozen I/O."""
    from gwexpy_studio.ops.io_capabilities import (
        CapabilityManifest,
        EffectiveCapabilitySnapshot,
        IOCapability,
    )

    manifest = CapabilityManifest(
        mode="frozen",
        digest="a" * 64,
        entries=(
            IOCapability(
                datatype="TimeSeries", format="csv", direction="read", tier="A"
            ),
        ),
    )
    monkeypatch.setattr(service_module, "load_capability_manifest", lambda: manifest)
    monkeypatch.setattr(
        service_module,
        "probe_effective_capabilities",
        lambda _manifest: (_ for _ in ()).throw(RuntimeError("/private/backend")),
    )
    connection, _ids, registry, store = _scenario("ping-and-shutdown")
    _run_worker_main_with_dependencies(connection, registry, store, monkeypatch)
    ping = json.loads(connection.sent[0].decode("utf-8"))
    snapshot = EffectiveCapabilitySnapshot.from_document(
        ping["payload"]["io_capabilities"]
    )

    entry = snapshot.capability("TimeSeries", "csv", "read")
    assert entry is not None
    assert entry.reason == "probe_failed"
    assert "/private/backend" not in json.dumps(ping)
