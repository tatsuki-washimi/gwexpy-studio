"""Pure-Python public boundary for the GWexpy Studio headless prototype."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._version import __version__
from .domain import (
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
    OperationGraph,
    PlotSpec,
    Project,
)
from .errors import (
    CodedStudioError,
    OperationError,
    ProjectFormatError,
    ProtocolValidationError,
    PrototypeNotImplementedError,
    SharedMemoryError,
    StudioError,
    WorkerBusyError,
    WorkerCrashedError,
    WorkerLifecycleError,
    WorkerTimeoutError,
)

if TYPE_CHECKING:
    from .session import StudioSession
    from .worker import WorkerClient, WorkerLifecycle

__all__ = [
    "OperationError",
    "CodedStudioError",
    "Operation",
    "OperationGraph",
    "DataObjectRef",
    "DataSourceRef",
    "ExecutionRecord",
    "PlotSpec",
    "Project",
    "ProjectFormatError",
    "ProtocolValidationError",
    "PrototypeNotImplementedError",
    "SharedMemoryError",
    "StudioError",
    "WorkerBusyError",
    "WorkerCrashedError",
    "WorkerLifecycleError",
    "WorkerTimeoutError",
    "WorkerClient",
    "WorkerLifecycle",
    "StudioSession",
    "__version__",
]


def __getattr__(name: str) -> Any:
    if name == "StudioSession":
        from .session import StudioSession

        return StudioSession
    if name == "WorkerClient":
        from .worker.client import WorkerClient

        return WorkerClient
    if name == "WorkerLifecycle":
        from .worker.client import WorkerLifecycle

        return WorkerLifecycle
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
