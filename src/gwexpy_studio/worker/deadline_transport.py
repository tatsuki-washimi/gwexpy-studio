"""Context-local deadlines and bounded worker connection I/O."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ..errors import WorkerTimeoutError


@dataclass(frozen=True, slots=True)
class DeadlineToken:
    """Immutable absolute monotonic deadline shared by one operation chain."""

    deadline_monotonic: float

    def remaining(self) -> float:
        """Return the non-negative wall-clock budget still available."""
        return max(self.deadline_monotonic - time.monotonic(), 0.0)


class DeadlineContext:
    """Hold one nested absolute deadline without mutating client timeouts."""

    def __init__(self) -> None:
        """Create an independently scoped token slot for one client."""
        self._current: ContextVar[DeadlineToken | None] = ContextVar(
            f"gwexpy_worker_deadline_{id(self)}", default=None
        )

    @property
    def active(self) -> DeadlineToken | None:
        """Return the effective absolute monotonic deadline, when scoped."""
        return self._current.get()

    @contextmanager
    def scope(self, deadline_monotonic: float) -> Iterator[None]:
        """Install the earlier of a new deadline and any enclosing deadline."""
        if isinstance(deadline_monotonic, bool) or not isinstance(
            deadline_monotonic, (int, float)
        ):
            raise TypeError("deadline_monotonic must be a finite positive number")
        deadline = float(deadline_monotonic)
        if not math.isfinite(deadline) or deadline <= 0.0:
            raise ValueError("deadline_monotonic must be a finite positive number")
        current = self.active
        effective = (
            deadline if current is None else min(current.deadline_monotonic, deadline)
        )
        token = self._current.set(DeadlineToken(effective))
        try:
            yield
        finally:
            self._current.reset(token)


@dataclass(slots=True)
class _PendingCall:
    """Result slot for one connection call running on a watchdog thread."""

    operation: Callable[[], Any]
    error: BaseException | None = None
    finished: threading.Event = field(default_factory=threading.Event)
    result: Any = None

    def run(self) -> None:
        try:
            self.result = self.operation()
        except BaseException as exc:
            self.error = exc
        finally:
            self.finished.set()


class BoundedConnectionTransport:
    """Run blocking connection sends and receives behind finite watchdogs."""

    def __init__(self) -> None:
        """Create an empty set of in-flight receive watchdogs."""
        self._read_lock = threading.Lock()
        self._orphan_reads: list[tuple[Any, _PendingCall]] = []

    @staticmethod
    def _start(pending: _PendingCall, *, operation: str) -> None:
        token = uuid.uuid4()
        threading.Thread(
            target=pending.run,
            name=f"gwexpy-studio-worker-{operation}-{token}",
            daemon=True,
        ).start()

    @staticmethod
    def _wait(pending: _PendingCall, timeout_s: float, *, operation: str) -> Any:
        timeout = max(float(timeout_s), 0.0)
        if timeout == 0.0 or not pending.finished.wait(timeout):
            raise WorkerTimeoutError(
                f"Worker {operation} timed out after {timeout_s}s",
                code="worker_timeout",
            )
        if pending.error is not None:
            raise pending.error
        return pending.result

    @staticmethod
    def _require_budget(timeout_s: float, *, operation: str) -> None:
        if float(timeout_s) <= 0.0:
            raise WorkerTimeoutError(
                f"Worker {operation} timed out after {timeout_s}s",
                code="worker_timeout",
            )

    def send_bytes(self, conn: Any, data: bytes, timeout_s: float) -> None:
        """Send one frame without allowing ``send_bytes`` to block the caller."""
        self._require_budget(timeout_s, operation="send")
        pending = _PendingCall(lambda: conn.send_bytes(data))
        self._start(pending, operation="send")
        self._wait(pending, timeout_s, operation="send")

    def _has_live_orphan_read(self, conn: Any) -> bool:
        with self._read_lock:
            self._orphan_reads = [
                item for item in self._orphan_reads if not item[1].finished.is_set()
            ]
            return any(orphan_conn is conn for orphan_conn, _item in self._orphan_reads)

    def recv_bytes(self, conn: Any, timeout_s: float) -> bytes:
        """Receive one frame while preventing overlap with an abandoned read."""
        self._require_budget(timeout_s, operation="response frame")
        if self._has_live_orphan_read(conn):
            raise WorkerTimeoutError(
                "A previously timed-out read is still draining this connection",
                code="worker_timeout",
            )
        pending = _PendingCall(conn.recv_bytes)
        self._start(pending, operation="recv")
        try:
            result = self._wait(pending, timeout_s, operation="response frame")
        except WorkerTimeoutError:
            with self._read_lock:
                self._orphan_reads.append((conn, pending))
            raise
        if result is None:
            raise EOFError("Worker produced no response frame")
        if not isinstance(result, bytes):
            raise TypeError("Worker response frame must be bytes")
        return result
