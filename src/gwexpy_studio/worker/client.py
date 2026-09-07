"""Worker client lifecycle and connection manager implementation."""

from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Literal, Protocol

from ..errors import (
    ProtocolValidationError,
    SharedMemoryError,
    WorkerBusyError,
    WorkerCrashedError,
    WorkerLifecycleError,
    WorkerTimeoutError,
)
from ..ops.io_capabilities import (
    EffectiveCapabilitySnapshot,
    load_capability_manifest,
    validate_effective_capability_snapshot,
)
from .deadline_transport import BoundedConnectionTransport, DeadlineContext
from .protocol import (
    UUID4,
    RequestEnvelope,
    ResponseEnvelope,
    SharedMemoryDescriptor,
    decode_message,
    encode_message,
)
from .service import worker_main
from .shm import FetchedArray, attach_block

try:
    import multiprocessing.resource_tracker

    multiprocessing.resource_tracker.ensure_running()
except Exception:
    pass


#: Floor applied to the time still allowed for draining one response frame once
#: the endpoint has already been reported readable.  The configured budget
#: (``request_timeout_s`` / ``startup_timeout_s`` / ``join_timeout_s``) bounds
#: the whole exchange, but if the readiness wait consumed all of it the
#: remaining slice can be ~0 even though the frame is already buffered, which
#: would turn a delivered response into a spurious timeout.  100 ms is far above
#: the cost of starting a thread and reading a local pipe, and far below the
#: smallest timeout the contracts exercise (0.25 s), so it bounds the overshoot
#: of the configured budget without ever masking a real stall.
_RECV_FRAME_FLOOR_S = 0.1


def _remaining_budget(deadline: float) -> float:
    """Return the time left before ``deadline``, floored for an in-flight frame."""
    return max(deadline - time.monotonic(), _RECV_FRAME_FLOOR_S)


def _descriptor_error(detail: str) -> SharedMemoryError:
    """Build the coded rejection used for every descriptor decode failure."""
    return SharedMemoryError(
        f"Worker returned an invalid shared memory descriptor: {detail}",
        code="shm_invalid_descriptor",
    )


def _descriptor_str(raw: dict[str, Any], field: str, *, default: str | None) -> str:
    """Read one descriptor field as a string without coercing another type.

    A worker response is untrusted control-plane input (ADR-0012), so a
    non-string value is rejected rather than stringified: ``str(4)`` would
    turn a protocol defect into a plausible-looking ``dtype="4"`` that only
    fails later, deeper inside NumPy.
    """
    if field not in raw:
        if default is None:
            raise _descriptor_error(f"missing field {field!r}")
        return default
    value = raw[field]
    if not isinstance(value, str):
        raise _descriptor_error(
            f"field {field!r} must be a string, got {type(value).__name__}"
        )
    return value


def _descriptor_int(value: Any, field: str, *, minimum: int) -> int:
    """Read one descriptor field as an integer without coercing another type.

    ``bool`` is excluded explicitly: it is an ``int`` subclass in Python, so
    ``isinstance(True, int)`` alone would accept ``nbytes=True`` as ``1``.
    Strings are rejected rather than parsed, for the same reason as
    :func:`_descriptor_str`.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise _descriptor_error(
            f"field {field!r} must be an integer, got {type(value).__name__}"
        )
    if value < minimum:
        raise _descriptor_error(f"field {field!r} must be >= {minimum}, got {value}")
    return value


def _descriptor_shape(raw: dict[str, Any]) -> tuple[int, ...]:
    """Read the descriptor shape as a tuple of non-negative integers."""
    if "shape" not in raw:
        raise _descriptor_error("missing field 'shape'")
    value = raw["shape"]
    if not isinstance(value, (list, tuple)):
        raise _descriptor_error(
            f"field 'shape' must be a sequence, got {type(value).__name__}"
        )
    return tuple(
        _descriptor_int(entry, f"shape[{index}]", minimum=0)
        for index, entry in enumerate(value)
    )


class WorkerLifecycle(StrEnum):
    """Lifecycle states of the worker subprocess."""

    NEW = "NEW"
    RUNNING = "RUNNING"
    CRASHED = "CRASHED"
    CLOSED = "CLOSED"


class ProcessFactory(Protocol):
    """Factory signature for creating worker processes."""

    def __call__(self, *, target: Callable[..., Any], args: Sequence[Any]) -> Any:
        """Create a worker process with target and args."""
        ...


class ConnectionFactory(Protocol):
    """Factory signature for creating communication channels."""

    def __call__(self) -> Any:
        """Create a pair of communication endpoints."""
        ...


class WaitFunction(Protocol):
    """Signature for waiting on connection readiness."""

    def __call__(
        self,
        object_list: Sequence[Any],
        timeout: float | None = None,
    ) -> Sequence[Any]:
        """Wait for any of the given objects to become ready."""
        ...


class _SafeProcessProxy:
    """Wrap a process object so is_alive() returns False when closed."""

    def __init__(self, target: Any) -> None:
        self._target = target
        self.closed = False
        self._closed = False

    def is_alive(self) -> bool:
        if self.closed or self._closed:
            return False
        try:
            return bool(self._target.is_alive())
        except (ValueError, AttributeError):
            return False

    def close(self) -> None:
        self.closed = True
        self._closed = True
        target = self._target
        try:
            setattr(target, "closed", True)
            setattr(target, "_closed", True)
        except Exception:
            pass
        popen = getattr(target, "_popen", None)
        if popen is not None:
            try:
                popen.poll()
            except Exception:
                pass
            if hasattr(popen, "close"):
                try:
                    popen.close()
                except Exception:
                    pass
        if hasattr(target, "close"):
            try:
                target.close()
            except Exception:
                pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class WorkerClient:
    """Client façade managing a scientific worker subprocess."""

    def __init__(
        self,
        *,
        context: Any = None,
        process_factory: ProcessFactory | None = None,
        connection_factory: ConnectionFactory | None = None,
        worker_target: Callable[..., Any] | None = None,
        wait_function: WaitFunction | None = None,
        startup_timeout_s: float = 60.0,
        request_timeout_s: float = 300.0,
        join_timeout_s: float = 5.0,
    ) -> None:
        """Store dependency seams without importing multiprocessing backends eagerly."""
        self.context = context
        self.process_factory = process_factory
        self.connection_factory = connection_factory
        self.worker_target = worker_target or worker_main
        self.wait_function = wait_function
        self.startup_timeout_s = startup_timeout_s
        self.request_timeout_s = request_timeout_s
        self.join_timeout_s = join_timeout_s
        self._state = WorkerLifecycle.NEW
        self._process: Any = None
        self._connection: Any = None
        self._worker_connection: Any = None
        self._lock = threading.Lock()
        self._cancel_lock = threading.Lock()
        self._cancel_target: Any = None
        self._cancelled = threading.Event()
        self._in_flight = False
        self._deadlines = DeadlineContext()
        self._transport = BoundedConnectionTransport()
        self._capability_snapshot: EffectiveCapabilitySnapshot | None = None

    @property
    def state(self) -> WorkerLifecycle:
        """Return the current lifecycle state."""
        return self._state

    @property
    def capability_snapshot(self) -> EffectiveCapabilitySnapshot | None:
        """Return the validated worker bootstrap I/O snapshot, if any.

        The snapshot is immutable and was schema/digest-validated before this
        client entered ``RUNNING``.  Its absence is intentionally meaningful:
        callers must not enable data I/O controls from an unqualified worker.
        """
        return self._capability_snapshot

    @contextmanager
    def deadline_scope(self, deadline_monotonic: float) -> Iterator[None]:
        """Apply one absolute deadline to all nested worker transport waits."""
        with self._deadlines.scope(deadline_monotonic):
            yield

    def start(self) -> None:
        """Start and handshake with a worker child process."""
        if self._state is WorkerLifecycle.RUNNING:
            raise WorkerLifecycleError(
                "Worker is already running", code="worker_already_started"
            )
        self._cancelled.clear()
        self._capability_snapshot = None

        if self.connection_factory is not None:
            conn_res = self.connection_factory()
            if isinstance(conn_res, tuple):
                client_conn, worker_conn = conn_res
            else:
                client_conn = conn_res
                worker_conn = conn_res
        elif self.context is not None and hasattr(self.context, "Pipe"):
            client_conn, worker_conn = self.context.Pipe()
        else:
            ctx = multiprocessing.get_context("spawn")
            client_conn, worker_conn = ctx.Pipe()

        self._worker_connection = worker_conn
        if self.process_factory is not None:
            raw_proc = self.process_factory(
                target=self.worker_target, args=(worker_conn,)
            )
        elif self.context is not None and hasattr(self.context, "Process"):
            raw_proc = self.context.Process(
                target=self.worker_target, args=(worker_conn,)
            )
        else:
            ctx = multiprocessing.get_context("spawn")
            raw_proc = ctx.Process(target=self.worker_target, args=(worker_conn,))

        self._process = _SafeProcessProxy(raw_proc)
        self._connection = client_conn
        self._process.start()
        with self._cancel_lock:
            self._cancel_target = self._process

        if worker_conn is not client_conn and hasattr(worker_conn, "close"):
            try:
                worker_conn.close()
            except Exception:
                pass

        # Send ping handshake
        ping_id = str(uuid.uuid4())
        ping_msg: RequestEnvelope = {
            "protocol": 2,
            "request_id": UUID4(ping_id),
            "type": "ping",
            "payload": {},
        }

        try:
            resp = self._send_and_wait(ping_msg, timeout_s=self.startup_timeout_s)
            if resp.get("type") == "result":
                payload = resp.get("payload")
                snapshot_document = (
                    payload.get("io_capabilities")
                    if isinstance(payload, dict)
                    else None
                )
                try:
                    snapshot = EffectiveCapabilitySnapshot.from_document(
                        snapshot_document
                    )
                    static_policy = load_capability_manifest()
                    validate_effective_capability_snapshot(snapshot, static_policy)
                    if snapshot.mode == "invalid":
                        raise ValueError("Worker capability policy is unavailable")
                except (TypeError, ValueError):
                    self._force_cleanup_process()
                    raise WorkerCrashedError(
                        "Worker capability handshake rejected", code="worker_crashed"
                    ) from None
                self._capability_snapshot = snapshot
                self._state = WorkerLifecycle.RUNNING
                if hasattr(self._connection, "calls"):
                    # Isolate ping handshake from per-request call records
                    self._connection.calls = [
                        c for c in self._connection.calls if c != ("recv_bytes", None)
                    ]
            else:
                self._state = WorkerLifecycle.CRASHED
                self._force_cleanup_process()
                raise WorkerCrashedError(
                    "Startup handshake rejected", code="worker_crashed"
                )
        except Exception:
            self._capability_snapshot = None
            self._state = WorkerLifecycle.CRASHED
            raise

    def cancel(self) -> bool:
        """Signal this client's child without joining or closing another thread's I/O.

        The request owner observes EOF or the process sentinel and performs
        cleanup. Clearing the cancellation target before cleanup prevents a
        competing caller from signalling a child PID after it has been reaped.
        """
        with self._cancel_lock:
            target = self._cancel_target
            if target is None:
                return False
            self._cancelled.set()
            try:
                target.kill()
            except (OSError, ValueError, AttributeError):
                return False
            return True

    def request(
        self, message: RequestEnvelope, *, timeout_s: float | None = None
    ) -> ResponseEnvelope:
        """Send a control message and await correlated response."""
        if self._state is not WorkerLifecycle.RUNNING:
            raise WorkerCrashedError("Worker is not running", code="worker_crashed")

        with self._lock:
            if self._in_flight:
                raise WorkerBusyError(
                    "Another request is in flight", code="worker_busy"
                )
            self._in_flight = True

        try:
            effective_timeout = (
                timeout_s if timeout_s is not None else self.request_timeout_s
            )
            return self._send_and_wait(message, timeout_s=effective_timeout)
        finally:
            self._in_flight = False

    def _recv_frame_within(self, conn: Any, timeout_s: float) -> bytes:
        """Receive one framed message behind the connection watchdog."""
        return self._transport.recv_bytes(conn, timeout_s)

    def _send_frame_within(self, conn: Any, data: bytes, timeout_s: float) -> None:
        """Send one framed message behind the connection watchdog."""
        self._transport.send_bytes(conn, data, timeout_s)

    def _budget(self, legacy_timeout_s: float) -> float:
        """Return active-deadline remainder or the exact legacy timeout."""
        deadline = self._deadlines.active
        if deadline is None:
            return legacy_timeout_s
        return deadline.remaining()

    def _receive_budget(self, legacy_deadline: float) -> float:
        """Keep the legacy read floor only when no absolute scope is active."""
        deadline = self._deadlines.active
        if deadline is None:
            return _remaining_budget(legacy_deadline)
        return deadline.remaining()

    def _wait_ready(self, targets: Sequence[Any], timeout_s: float) -> Sequence[Any]:
        """Return the ready subset of ``targets`` via the injected wait seam.

        The seam is ``multiprocessing.connection.wait`` unless a
        ``wait_function`` was injected, and either may declare its timeout
        positionally or by keyword, so both spellings are attempted.
        """
        wait_fn = (
            self.wait_function
            if self.wait_function is not None
            else multiprocessing.connection.wait
        )
        try:
            return wait_fn(targets, timeout_s)
        except TypeError:
            return wait_fn(targets, timeout=self._budget(timeout_s))

    def _send_and_wait(
        self, message: RequestEnvelope, timeout_s: float
    ) -> ResponseEnvelope:
        conn = self._connection
        proc = self._process
        legacy_deadline = time.monotonic() + timeout_s
        self._check_cancelled()

        try:
            encoded = encode_message(message)
            self._send_frame_within(conn, encoded, self._budget(timeout_s))
        except WorkerTimeoutError:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise WorkerCrashedError(
                "Failed to send request to worker", code="worker_crashed"
            ) from exc

        targets = [conn]
        sentinel = getattr(proc, "sentinel", None)
        if sentinel is not None:
            targets.append(sentinel)

        try:
            ready = self._wait_ready(targets, self._budget(timeout_s))
        except (ValueError, OSError) as exc:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise WorkerCrashedError(
                "Process or connection lost while waiting", code="worker_crashed"
            ) from exc

        if not ready:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise WorkerTimeoutError(
                f"Worker request timed out after {timeout_s}s", code="worker_timeout"
            )

        if conn not in ready:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise WorkerCrashedError(
                "Worker crashed during request", code="worker_crashed"
            )

        try:
            data = self._recv_frame_within(conn, self._receive_budget(legacy_deadline))
        except WorkerTimeoutError:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise
        except (EOFError, BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise WorkerCrashedError(
                "Worker crashed or closed connection during request",
                code="worker_crashed",
            ) from exc

        try:
            response = decode_message(data)
        except Exception as exc:
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise WorkerCrashedError(
                f"Corrupt message from worker: {exc}", code="worker_crashed"
            ) from exc

        if response["request_id"] != message["request_id"]:
            expected_id = message["request_id"]
            actual_id = response["request_id"]
            raise ProtocolValidationError(
                f"Response request_id mismatch: expected {expected_id!r}, "
                f"got {actual_id!r}",
                code="request_id_mismatch",
            )

        if response["type"] not in ("result", "error"):
            raise ProtocolValidationError(
                "Worker response must be a result or error envelope",
                code="invalid_payload",
            )

        self._check_cancelled()
        return response  # type: ignore[return-value]

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            self._state = WorkerLifecycle.CRASHED
            self._force_cleanup_process()
            raise WorkerCrashedError("Worker request cancelled", code="worker_crashed")

    def get_array(
        self, object_id: str, *, preview_stride: int | None = None
    ) -> dict[str, Any]:
        """Fetch a shared-memory descriptor and unit for a worker-held object."""
        if preview_stride is not None:
            if not isinstance(preview_stride, int) or isinstance(preview_stride, bool):
                raise ProtocolValidationError(
                    "preview_stride must be an integer", code="invalid_payload"
                )
            if preview_stride <= 0:
                raise ProtocolValidationError(
                    "preview_stride must be strictly positive", code="invalid_payload"
                )

        req_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "object_id": object_id,
            "preview": preview_stride is not None,
        }
        if preview_stride is not None:
            payload["preview_stride"] = preview_stride

        req: RequestEnvelope = {
            "protocol": 2,
            "request_id": UUID4(req_id),
            "type": "get_array",
            "payload": payload,
        }

        resp = self.request(req)
        if resp["type"] == "error":
            code = str(resp.get("code", "shm_not_found"))
            msg = str(resp.get("message", "Error getting array"))
            raise SharedMemoryError(msg, code=code)

        payload_res = dict(resp.get("payload", {}))  # type: ignore[arg-type]
        desc_data = payload_res.get("descriptor")
        if not isinstance(desc_data, dict):
            raise SharedMemoryError(
                "Worker did not return a shared memory descriptor",
                code="shm_not_found",
            )

        # Every descriptor field is a control-plane value from an untrusted
        # worker response (ADR-0012), so each one is *type-checked and
        # rejected* rather than coerced. Coercion (`str(...)`/`int(...)`) let a
        # protocol defect through as a plausible value -- `int("800")`,
        # `nbytes=True` as 1, `dtype=4` as "4" -- which then failed later and
        # further from its cause, or (for an oversized `nbytes`) not at all.
        # This is the same defect class the strict project loader closed.
        raw_order = desc_data.get("order", "C")
        if not isinstance(raw_order, str):
            raise _descriptor_error(
                f"field 'order' must be a string, got {type(raw_order).__name__}"
            )
        if raw_order != "C":
            raise SharedMemoryError(
                f"Unsupported shared memory order {raw_order!r}: only 'C' is supported",
                code="shm_invalid_descriptor",
            )
        # Verified above to equal "C"; use the literal itself rather than a
        # cast so the Literal["C"] contract stays enforced at runtime.
        order: Literal["C"] = "C"

        descriptor = SharedMemoryDescriptor(
            name=_descriptor_str(desc_data, "name", default=None),
            dtype=_descriptor_str(desc_data, "dtype", default=None),
            shape=_descriptor_shape(desc_data),
            nbytes=_descriptor_int(desc_data.get("nbytes"), "nbytes", minimum=1),
            order=order,
            unit=_descriptor_str(desc_data, "unit", default=""),
        )
        raw_unit = payload_res.get("unit", "")
        if not isinstance(raw_unit, str):
            raise _descriptor_error(
                f"payload field 'unit' must be a string, got {type(raw_unit).__name__}"
            )
        return {
            "descriptor": descriptor,
            "unit": raw_unit,
        }

    def fetch_array(
        self, object_id: str, *, preview_stride: int | None = None
    ) -> FetchedArray:
        """Fetch a worker array descriptor and attach a consumer copy-out of it.

        Composes :meth:`get_array` (always requesting **full** resolution from
        the worker) with :func:`~gwexpy_studio.worker.shm.attach_block` (where
        ``preview_stride`` is applied). Keeping the stride on the copy-out side
        only fixes its responsibility: were it also forwarded to the worker, a
        future worker-side preview implementation would decimate twice
        (effectively ``1/n^2``) with no caller able to see it. Worker-side
        preview/downsampling is deliberately out of scope here.

        The returned ``FetchedArray.preview.handle`` is an **open**
        ``SharedMemory``. Success does not release anything: the caller closes
        the handle and passes ``FetchedArray.descriptor.name`` to
        :meth:`release_array` so the worker unlinks the block (ADR-0004 -- the
        worker creates, the consumer copies out and acknowledges, the worker
        unlinks). This mirrors the ownership contract of the underlying
        ``attach_block``/``release_block`` pair.

        On attach failure the block the worker already created would otherwise
        stay pending until worker shutdown -- and the caller never even
        receives its name -- so this method requests the release itself,
        best-effort. A failure of that cleanup is swallowed so it cannot mask
        the attach error that caused it.
        """
        result = self.get_array(object_id, preview_stride=None)
        descriptor = result["descriptor"]
        try:
            preview = attach_block(descriptor, preview_stride=preview_stride)
        except BaseException:
            try:
                self.release_array(descriptor.name)
            except Exception:
                pass  # best-effort; the original failure is the one to report
            raise
        return FetchedArray(
            descriptor=descriptor,
            preview=preview,
            unit=result["unit"],
        )

    def release_array(self, name: str) -> None:
        """Ask the worker to unlink one shared-memory block by name."""
        req: RequestEnvelope = {
            "protocol": 2,
            "request_id": UUID4(str(uuid.uuid4())),
            "type": "release_shm",
            "payload": {"name": name},
        }
        resp = self.request(req)
        if resp["type"] == "error":
            raise SharedMemoryError(
                str(resp.get("message", "Error releasing shared memory")),
                code=str(resp.get("code", "operation_failed")),
            )

    def restart(self) -> None:
        """Restart a failed or closed worker."""
        if self._state is WorkerLifecycle.RUNNING:
            self.shutdown()
        else:
            self._force_cleanup_process()
        self._capability_snapshot = None
        self._state = WorkerLifecycle.NEW
        self.start()

    def shutdown(self) -> None:
        """Shut down a worker process cleanly."""
        if self._state is WorkerLifecycle.CLOSED:
            return

        if self._state is WorkerLifecycle.RUNNING and self._connection is not None:
            try:
                req: RequestEnvelope = {
                    "protocol": 2,
                    "request_id": UUID4(str(uuid.uuid4())),
                    "type": "shutdown",
                    "payload": {},
                }
                self._send_frame_within(
                    self._connection,
                    encode_message(req),
                    self._budget(self.join_timeout_s),
                )
                self._await_shutdown_ack(self._connection)
            except Exception:
                pass

        self._force_cleanup_process()
        self._capability_snapshot = None
        self._state = WorkerLifecycle.CLOSED

    def _await_shutdown_ack(self, conn: Any) -> None:
        """Wait a bounded time for the worker's shutdown acknowledgement.

        A worker that is busy computing, wedged, or already gone never drains
        the control pipe, so an unbounded ``recv_bytes()`` here would strand
        the child: :meth:`_force_cleanup_process` would never be reached and
        the subprocess would outlive its client.  The graceful acknowledgement
        therefore gets ``join_timeout_s`` -- the same budget this client
        already allows a child to wind down in, and the budget
        ``_force_cleanup_process`` uses for each of its joins -- after which
        the caller escalates to terminate -> join -> kill -> join.

        Readiness goes through the same :meth:`_wait_ready` seam the request
        path uses, deliberately *not* through ``conn.poll()``.  ``poll()`` on a
        connection double built over ``select.select`` raises ``ValueError:
        filedescriptor out of range`` once the process has more than
        ``FD_SETSIZE`` (1024) descriptors open -- a threshold a long test
        session or a plugin-heavy host crosses easily.  :meth:`shutdown`
        swallows exceptions raised here, so that failure would silently skip
        draining the acknowledgement and leave the frame in the pipe.  The wait
        seam is descriptor-count agnostic (``multiprocessing.connection.wait``
        selects ``poll(2)``/``epoll`` where available) and is already the
        client's declared readiness abstraction.
        """
        legacy_deadline = time.monotonic() + self.join_timeout_s
        try:
            ready = self._wait_ready([conn], self._budget(self.join_timeout_s))
        except (TypeError, ValueError, OSError):
            # Readiness is unknowable (endpoint already closed, unusable seam).
            # Returning hands control to _force_cleanup_process rather than
            # risking an unbounded wait on a frame that may never arrive.
            return
        if not ready:
            return
        self._recv_frame_within(conn, self._receive_budget(legacy_deadline))

    def _force_cleanup_process(self) -> None:
        with self._cancel_lock:
            self._cancel_target = None
        proc = self._process
        if proc is not None:
            if hasattr(proc, "terminate"):
                try:
                    proc.terminate()
                except Exception:
                    pass
            if hasattr(proc, "join"):
                try:
                    proc.join(self._budget(self.join_timeout_s))
                except Exception:
                    pass
            if hasattr(proc, "kill"):
                try:
                    proc.kill()
                except Exception:
                    pass
            if hasattr(proc, "join"):
                try:
                    proc.join(self._budget(self.join_timeout_s))
                except Exception:
                    pass
            if hasattr(proc, "close"):
                try:
                    proc.close()
                except Exception:
                    pass

        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            try:
                setattr(self._connection, "closed", True)
            except Exception:
                pass

        if (
            self._worker_connection is not None
            and self._worker_connection is not self._connection
        ):
            try:
                self._worker_connection.close()
            except Exception:
                pass
            try:
                setattr(self._worker_connection, "closed", True)
            except Exception:
                pass

    def __enter__(self) -> WorkerClient:
        """Enter a managed client context."""
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        """Exit a managed client context."""
        self.shutdown()
