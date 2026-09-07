"""Worker logging integration at the spawn-compatible process boundary."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest


class _Connection:
    """A deterministic pipe replacement for a one-request worker lifecycle."""

    def __init__(self, requests: list[bytes]) -> None:
        self._requests = list(requests)
        self.closed = False
        self.sent: list[bytes] = []

    def recv_bytes(self) -> bytes:
        if not self._requests:
            raise EOFError
        return self._requests.pop(0)

    def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


class _Store:
    """Minimal transfer store proving normal worker cleanup still runs."""

    pending: set[str]

    def __init__(self) -> None:
        self.pending = set()
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


class _Registry:
    """Unused registry seam sufficient for a shutdown-only protocol exchange."""

    def list_objects(self) -> list[str]:
        return []

    def delete_object(self, _object_id: str) -> None:
        return None

    def execute(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {}


class _RecordingLogger:
    """A file-free logger seam that exposes exactly what the worker records."""

    def __init__(self) -> None:
        self.records: list[str] = []

    def error(self, message: str, *args: object, **_kwargs: object) -> None:
        self.records.append(message % args)


def _request(
    request_type: str,
    payload: dict[str, Any],
    *,
    request_id: str,
) -> bytes:
    from gwexpy_studio.worker.protocol import encode_message

    return encode_message(
        {
            "protocol": 2,
            "request_id": request_id,
            "type": request_type,
            "payload": payload,
        }
    )


def _shutdown_request() -> bytes:
    return _request(
        "shutdown",
        {},
        request_id="00000000-0000-4000-8000-000000000701",
    )


@pytest.mark.contract("TRT-LOG-008")
def test_worker_configures_and_shuts_down_its_log_for_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn entry owns a separate worker handler through normal cleanup."""
    from gwexpy_studio.worker import service

    events: list[object] = []
    store = _Store()
    connection = _Connection([_shutdown_request()])
    logger = logging.getLogger("gwexpy_studio.worker")
    created_handler = object()
    handler_snapshots = iter((frozenset(), frozenset({created_handler})))

    monkeypatch.setattr(
        service,
        "configure_worker_logging",
        lambda: events.append("configure-worker-log") or logger,
    )
    monkeypatch.setattr(
        service,
        "shutdown_studio_logging",
        lambda *, logger_name, owned_handlers: events.append(
            ("shutdown-worker-log", logger_name, owned_handlers)
        ),
    )
    monkeypatch.setattr(
        service,
        "owned_studio_log_handlers",
        lambda *, logger_name: next(handler_snapshots),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "guard_parent_lifetime",
        lambda: events.append("guard-parent") or False,
    )
    monkeypatch.setattr(
        service,
        "create_registry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(service, "create_store", lambda: store)
    monkeypatch.setitem(
        sys.modules,
        "gwexpy",
        SimpleNamespace(register_all=lambda *, include_io: None, __version__="test"),
    )

    service.worker_main(connection)

    assert events == [
        "guard-parent",
        "configure-worker-log",
        (
            "shutdown-worker-log",
            "gwexpy_studio.worker",
            frozenset({created_handler}),
        ),
    ]
    assert store.cleaned
    assert connection.closed


@pytest.mark.contract("TRT-LOG-009")
def test_worker_shuts_down_its_log_when_startup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker startup exception cannot leak the worker log handler."""
    from gwexpy_studio.worker import service

    events: list[object] = []
    connection = _Connection([])
    logger = logging.getLogger("gwexpy_studio.worker")

    monkeypatch.setattr(
        service,
        "configure_worker_logging",
        lambda: events.append("configure-worker-log") or logger,
    )
    monkeypatch.setattr(
        service,
        "shutdown_studio_logging",
        lambda *, logger_name, owned_handlers=None: events.append(
            ("shutdown-worker-log", logger_name, owned_handlers)
        ),
    )

    def fail_guard() -> bool:
        events.append("guard-parent")
        raise RuntimeError("worker startup failure")

    monkeypatch.setattr(service, "guard_parent_lifetime", fail_guard)

    with pytest.raises(RuntimeError, match="worker startup failure"):
        service.worker_main(connection)

    assert events == ["guard-parent"]
    assert connection.closed


@pytest.mark.contract("TRT-LOG-010")
def test_worker_leaves_a_preexisting_worker_handler_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-process worker seam cannot close a handler it inherited."""
    from gwexpy_studio.worker import service

    events: list[object] = []
    connection = _Connection([_shutdown_request()])
    store = _Store()
    logger = logging.getLogger("gwexpy_studio.worker")
    existing_handler = object()

    monkeypatch.setattr(
        service,
        "guard_parent_lifetime",
        lambda: events.append("guard-parent") or False,
    )
    monkeypatch.setattr(
        service,
        "configure_worker_logging",
        lambda: events.append("configure-worker-log") or logger,
    )
    monkeypatch.setattr(
        service,
        "owned_studio_log_handlers",
        lambda *, logger_name: frozenset({existing_handler}),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "shutdown_studio_logging",
        lambda **_kwargs: events.append("shutdown-worker-log"),
    )
    monkeypatch.setattr(service, "create_registry", lambda: _Registry())
    monkeypatch.setattr(service, "create_store", lambda: store)
    monkeypatch.setitem(
        sys.modules,
        "gwexpy",
        SimpleNamespace(register_all=lambda *, include_io: None, __version__="test"),
    )

    service.worker_main(connection)

    assert events == ["guard-parent", "configure-worker-log"]
    assert store.cleaned
    assert connection.closed


@pytest.mark.contract("TRT-LOG-011")
def test_worker_logs_only_a_safe_code_for_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup diagnostics never write a source path or exception text to logs."""
    from gwexpy_studio.worker import service

    events: list[object] = []
    logger = _RecordingLogger()
    connection = _Connection([])
    created_handler = object()
    handler_snapshots = iter((frozenset(), frozenset({created_handler})))

    monkeypatch.setattr(service, "guard_parent_lifetime", lambda: False)
    monkeypatch.setattr(service, "configure_worker_logging", lambda: logger)
    monkeypatch.setattr(
        service,
        "owned_studio_log_handlers",
        lambda *, logger_name: next(handler_snapshots),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "shutdown_studio_logging",
        lambda *, logger_name, owned_handlers: events.append(
            ("shutdown-worker-log", logger_name, owned_handlers)
        ),
    )

    def fail_register_all(*, include_io: bool) -> None:
        del include_io
        raise RuntimeError("/private/experiment/startup-source.hdf5")

    monkeypatch.setitem(
        sys.modules,
        "gwexpy",
        SimpleNamespace(register_all=fail_register_all, __version__="test"),
    )

    with pytest.raises(RuntimeError, match="startup-source"):
        service.worker_main(connection)

    assert logger.records == ["Worker startup failed: operation_failed"]
    assert "/private/experiment" not in "\n".join(logger.records)
    assert events == [
        (
            "shutdown-worker-log",
            "gwexpy_studio.worker",
            frozenset({created_handler}),
        )
    ]
    assert connection.closed


@pytest.mark.contract("TRT-LOG-012")
def test_worker_logs_only_a_safe_code_for_a_request_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scientific operation failure writes a stable code without its details."""
    from gwexpy_studio.worker import service

    logger = _RecordingLogger()
    connection = _Connection(
        [
            _request(
                "execute",
                {},
                request_id="00000000-0000-4000-8000-000000000702",
            ),
            _shutdown_request(),
        ]
    )
    store = _Store()
    created_handler = object()
    handler_snapshots = iter((frozenset(), frozenset({created_handler})))

    class _FailingRegistry(_Registry):
        def execute(self, _payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("/private/experiment/request-source.hdf5")

    monkeypatch.setattr(service, "guard_parent_lifetime", lambda: False)
    monkeypatch.setattr(service, "configure_worker_logging", lambda: logger)
    monkeypatch.setattr(
        service,
        "owned_studio_log_handlers",
        lambda *, logger_name: next(handler_snapshots),
        raising=False,
    )
    monkeypatch.setattr(
        service,
        "shutdown_studio_logging",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(service, "create_registry", lambda: _FailingRegistry())
    monkeypatch.setattr(service, "create_store", lambda: store)
    monkeypatch.setitem(
        sys.modules,
        "gwexpy",
        SimpleNamespace(register_all=lambda *, include_io: None, __version__="test"),
    )

    service.worker_main(connection)

    assert logger.records == ["Worker request failed: invalid_payload"]
    assert "/private/experiment" not in "\n".join(logger.records)
    assert store.cleaned
    assert connection.closed
