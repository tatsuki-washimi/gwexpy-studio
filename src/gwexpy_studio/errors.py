"""Error types shared by the headless Studio boundary."""

from __future__ import annotations

from typing import ClassVar


class StudioError(Exception):
    """Base class for Studio-owned errors."""


class CodedStudioError(StudioError):
    """Studio error carrying a stable machine-readable error code."""

    default_code: ClassVar[str] = ""

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        """Create a coded error without implementing boundary translation."""
        resolved_code = self.default_code if code is None else code
        if not resolved_code:
            raise ValueError("code must be non-empty")
        self.code = resolved_code
        super().__init__(message or resolved_code)


class ProtocolValidationError(CodedStudioError):
    """Raised when a control-plane message fails protocol validation."""

    default_code = "invalid_payload"


class SharedMemoryError(CodedStudioError):
    """Raised when a shared-memory boundary operation is invalid."""

    default_code = "shm_invalid_descriptor"


class WorkerBusyError(CodedStudioError):
    """Raised when a worker cannot accept another in-flight request."""

    default_code = "worker_busy"


class WorkerTimeoutError(CodedStudioError):
    """Raised when a worker startup or request deadline expires."""

    default_code = "worker_timeout"


class WorkerLifecycleError(CodedStudioError):
    """Raised for an invalid worker lifecycle transition."""

    default_code = "worker_busy"


class OperationError(CodedStudioError):
    """Raised when an operation request cannot be executed."""

    default_code = "operation_failed"


class WorkerCrashedError(CodedStudioError):
    """Raised when a worker process exits unexpectedly or becomes unreachable."""

    default_code = "worker_crashed"


class ProjectFormatError(CodedStudioError):
    """Raised when a project manifest or archive violates the format."""

    default_code = "invalid_manifest"


FOUNDATION_CODE = "STUDIO-FOUNDATION-NOT-IMPLEMENTED"


class PrototypeNotImplementedError(CodedStudioError, NotImplementedError):
    """Temporary foundation placeholder sentinel."""

    default_code = FOUNDATION_CODE

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str = FOUNDATION_CODE,
        owner: str = "",
    ) -> None:
        """Keep the deferred code and owner."""
        super().__init__(
            message or f"{code}: implementation owned by {owner}", code=code
        )
        self.owner = owner
