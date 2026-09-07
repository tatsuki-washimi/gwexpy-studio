"""Scientific worker process entry-point implementation."""

from __future__ import annotations

import logging
import os
import platform
from importlib.metadata import version
from typing import Any

from ..domain.project import Operation
from ..domain.project_v2 import record_dict
from ..errors import (
    ProtocolValidationError,
)
from ..ops.io_capabilities import (
    activate_effective_capability_snapshot,
    load_capability_manifest,
    probe_effective_capabilities,
    unprobed_effective_capabilities,
)
from ..ops.source import inspect_source
from ..runtime.executor import execute_operation_detailed
from ..runtime.logging import (
    STUDIO_LOGGER_NAME,
    configure_worker_logging,
    owned_studio_log_handlers,
    shutdown_studio_logging,
)
from ..runtime.store import ObjectStore
from .parent_lifetime import guard_parent_lifetime
from .protocol import (
    ERROR_CODES,
    UUID4,
    ErrorEnvelope,
    ResultEnvelope,
    decode_message,
    encode_message,
)
from .shm import create_block, release_block
from .signal_service import SIGNAL_REQUESTS, SignalService


class DefaultRegistry:
    """Worker-local registry holding materialized objects and graph execution."""

    def __init__(self) -> None:
        """Initialize the object store."""
        self.store = ObjectStore()

    def list_objects(self) -> list[str]:
        """List all materialized object IDs."""
        return [ref.object_id for ref in self.store.list_objects()]

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute an operation or echo execution descriptor."""
        op_data = payload.get("operation")
        if isinstance(op_data, dict) and "operation_id" in op_data:
            op = Operation(
                op_id=op_data.get("op_id", "op-0"),
                operation_id=op_data["operation_id"],
                operation_schema=op_data.get("operation_schema", 1),
                inputs=dict(op_data.get("inputs", {})),
                params=dict(op_data.get("params", {})),
                outputs=tuple(op_data.get("outputs", ())),
            )
            outcome = execute_operation_detailed(
                op, store=self.store, recorded_details=payload.get("recorded_details")
            )
            ref = outcome.object_ref
            return {
                "object_id": ref.object_id,
                "graph_id": payload.get("graph_id"),
                "operation_id": op.op_id,
                "object_ref": record_dict(ref),
                "details": record_dict(outcome.details),
                "warnings": list(outcome.warnings),
                "environment": {
                    "python": platform.python_version(),
                    **{
                        name: version(name)
                        for name in ("gwexpy", "gwpy", "numpy", "scipy", "astropy")
                    },
                },
            }

        output_obj_id = payload.get("output_object_id") or payload.get("output")
        graph_id = payload.get("graph_id")
        op_id = payload.get("operation_id") or payload.get("op_id")
        return {
            "object_id": output_obj_id,
            "graph_id": graph_id,
            "operation_id": op_id,
        }

    def delete_object(self, object_id: str) -> None:
        """Delete an object from the store."""
        if object_id in self.store:
            self.store.delete(object_id)


class DefaultStore:
    """Worker-local shared memory lifecycle tracking store."""

    def __init__(self) -> None:
        """Initialize tracking for pending blocks."""
        self.pending: set[str] = set()

    def release_shm(self, name: str) -> None:
        """Release a created shared-memory block."""
        self.pending.discard(name)
        try:
            release_block(name, acknowledged=True)
        except Exception:
            pass

    def cleanup(self) -> None:
        """Clean up all pending shared memory blocks."""
        for name in list(self.pending):
            self.release_shm(name)
        self.pending.clear()


def create_registry() -> Any:
    """Create the worker registry implementation."""
    return DefaultRegistry()


def create_store() -> Any:
    """Create the worker shared-memory store implementation."""
    return DefaultStore()


_WORKER_LOGGER_NAME = f"{STUDIO_LOGGER_NAME}.worker"


def _safe_worker_error_code(error: Exception, *, default: str) -> str:
    """Map arbitrary exceptions to a stable log-safe worker error code."""
    code = getattr(error, "code", default)
    if isinstance(code, str) and code in ERROR_CODES:
        return code
    return "operation_failed"


def _record_worker_failure(
    logger: logging.Logger | None,
    *,
    phase: str,
    code: str,
) -> None:
    """Record a worker failure without serializing its untrusted details."""
    if logger is None:
        return
    try:
        logger.error("Worker %s failed: %s", phase, code)
    except Exception:
        pass


def worker_main(connection: Any) -> None:
    """Run the worker loop handling protocol requests over connection."""
    store: Any | None = None
    worker_logger: logging.Logger | None = None
    worker_owned_handlers: frozenset[logging.Handler] = frozenset()
    capability_snapshot: Any = None

    try:
        guard_parent_lifetime()
        try:
            existing_handlers = owned_studio_log_handlers(
                logger_name=_WORKER_LOGGER_NAME
            )
            worker_logger = configure_worker_logging()
            worker_owned_handlers = (
                owned_studio_log_handlers(logger_name=_WORKER_LOGGER_NAME)
                - existing_handlers
            )
        except Exception:
            # A corrupt or inaccessible log destination must not prevent
            # scientific work or expose a data path through stderr.
            worker_logger = None
            worker_owned_handlers = frozenset()

        try:
            import gwexpy

            gwexpy.register_all(include_io=False)

            # The main/UI process never probes optional I/O backends.  This
            # worker-only bootstrap creates the effective reviewed-policy ∩
            # runtime-registry snapshot before accepting an I/O command.
            capability_manifest = load_capability_manifest()
            try:
                capability_snapshot = probe_effective_capabilities(capability_manifest)
            except Exception:
                # Keep the worker alive for recovery/project commands, but do
                # not let a defective optional backend turn reviewed A/B I/O
                # into a usable static declaration.
                _record_worker_failure(
                    worker_logger, phase="io_probe", code="probe_failed"
                )
                capability_snapshot = unprobed_effective_capabilities(
                    capability_manifest
                )
            activate_effective_capability_snapshot(capability_snapshot)

            registry = create_registry()
            store = create_store()
        except Exception as error:
            _record_worker_failure(
                worker_logger,
                phase="startup",
                code=_safe_worker_error_code(error, default="operation_failed"),
            )
            raise

        while True:
            try:
                data = connection.recv_bytes()
            except (EOFError, BrokenPipeError, ConnectionResetError):
                break

            req: Any = None
            pending_before = set(getattr(store, "pending", set()))
            try:
                req = decode_message(data)
                req_id = req["request_id"]
                req_type = req["type"]
                payload = req["payload"]

                if req_type == "ping":
                    import sys

                    resp_val: ResultEnvelope = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": {
                            "ready": True,
                            "request_id": req_id,
                            "pid": os.getpid(),
                            "python": sys.version,
                            "gwexpy_version": getattr(gwexpy, "__version__", "0.1.0"),
                            "gwexpy_path": getattr(gwexpy, "__file__", ""),
                            "io_capabilities": capability_snapshot.document(),
                        },
                    }
                    connection.send_bytes(encode_message(resp_val))

                elif req_type == "shutdown":
                    resp_val = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": {},
                    }
                    connection.send_bytes(encode_message(resp_val))
                    break

                elif req_type in SIGNAL_REQUESTS:
                    result = SignalService(registry.store, store).dispatch(
                        req_type, payload
                    )
                    resp_val = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": result,
                    }
                    connection.send_bytes(encode_message(resp_val))

                elif req_type == "execute":
                    if "unknown" in payload:
                        raise ProtocolValidationError(
                            "Unknown execute parameter", code="invalid_payload"
                        )
                    res = registry.execute(payload)
                    resp_val = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": res,
                    }
                    connection.send_bytes(encode_message(resp_val))

                elif req_type == "inspect_source":
                    inspection = inspect_source(str(payload["uri"]))
                    resp_val = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": {
                            "exists": inspection.exists,
                            "size_bytes": inspection.size_bytes,
                            "mtime": inspection.mtime,
                            "format_guess": inspection.format_guess,
                            "resolved_uri": inspection.resolved_uri,
                            "device": inspection.device,
                            "inode": inspection.inode,
                            "mtime_ns": inspection.mtime_ns,
                        },
                    }
                    connection.send_bytes(encode_message(resp_val))

                elif req_type in ("release_shm", "release-shm"):
                    store.release_shm(payload["name"])
                    resp_val = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": {},
                    }
                    connection.send_bytes(encode_message(resp_val))

                elif req_type in ("delete_object", "delete-object"):
                    registry.delete_object(payload["object_id"])
                    resp_val = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": {},
                    }
                    connection.send_bytes(encode_message(resp_val))

                elif req_type in ("list_objects", "list-objects"):
                    res = registry.list_objects()
                    resp_val = {
                        "protocol": 2,
                        "request_id": req_id,
                        "type": "result",
                        "payload": {"objects": res, "object_ids": res},
                    }
                    connection.send_bytes(encode_message(resp_val))

                elif req_type in ("get_array", "get-array"):
                    obj_id = payload["object_id"]
                    if hasattr(registry, "store"):
                        val = registry.store.get(obj_id)
                        raw = getattr(val, "value", val)
                        # ``raw`` is the unit-less numeric buffer, so the unit
                        # lives on ``val`` and must be read from there. Both
                        # wire fields are filled from this one value: the
                        # descriptor is the single source of truth for transfer
                        # metadata (shape/dtype/nbytes/order/unit), and the
                        # top-level ``unit`` is kept only for compatibility
                        # with existing readers. Previously the descriptor's
                        # unit came from ``create_block(raw)`` -- always empty,
                        # because a plain array carries no ``.unit`` -- so the
                        # two disagreed on every response.
                        unit_str = str(getattr(val, "unit", ""))
                        block = create_block(raw)
                        store.pending.add(block.descriptor.name)
                        block.shm.close()
                        resp_val = {
                            "protocol": 2,
                            "request_id": req_id,
                            "type": "result",
                            "payload": {
                                "descriptor": {
                                    "name": block.descriptor.name,
                                    "dtype": block.descriptor.dtype,
                                    "shape": list(block.descriptor.shape),
                                    "nbytes": block.descriptor.nbytes,
                                    "order": block.descriptor.order,
                                    "unit": unit_str,
                                },
                                "unit": unit_str,
                            },
                        }
                    else:
                        resp_val = {
                            "protocol": 2,
                            "request_id": req_id,
                            "type": "result",
                            "payload": {},
                        }
                    connection.send_bytes(encode_message(resp_val))

            except Exception as exc:
                for name in set(getattr(store, "pending", set())) - pending_before:
                    store.release_shm(name)
                code = _safe_worker_error_code(exc, default="invalid_payload")
                _record_worker_failure(worker_logger, phase="request", code=code)
                req_getter = getattr(req, "get", None) if "req" in locals() else None
                req_id_str = (
                    req_getter("request_id") if req_getter is not None else None
                )
                if not req_id_str:
                    req_id_str = "00000000-0000-4000-8000-000000000000"
                err_resp: ErrorEnvelope = {
                    "protocol": 2,
                    "request_id": UUID4(str(req_id_str)),
                    "type": "error",
                    "code": str(code),  # type: ignore[typeddict-item]
                    "message": str(exc) or "Worker operation error",
                }
                connection.send_bytes(encode_message(err_resp))

    finally:
        try:
            if store is not None:
                store.cleanup()
        finally:
            try:
                activate_effective_capability_snapshot(None)
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass
            if worker_owned_handlers:
                try:
                    shutdown_studio_logging(
                        logger_name=_WORKER_LOGGER_NAME,
                        owned_handlers=worker_owned_handlers,
                    )
                except Exception:
                    pass
