"""Scientific runtime boundary interfaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .executor import execute_operation
    from .store import ObjectStore

__all__ = ["ObjectStore", "execute_operation"]


def __getattr__(name: str) -> Any:
    """Import scientific runtime objects only when a caller requests them."""
    if name == "ObjectStore":
        from .store import ObjectStore

        return ObjectStore
    if name == "execute_operation":
        from .executor import execute_operation

        return execute_operation
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
