"""Helpers for preserving prototype sentinel identity across test boundaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gwexpy_studio.errors import PrototypeNotImplementedError

FOUNDATION_CODE = "STUDIO-FOUNDATION-NOT-IMPLEMENTED"


def invoke_preserving_sentinel(callback: Callable[[], Any], *, owner: str) -> Any:
    """Validate and re-raise the current foundation sentinel unchanged."""
    try:
        return callback()
    except PrototypeNotImplementedError as error:
        assert type(error) is PrototypeNotImplementedError
        assert error.code == FOUNDATION_CODE
        assert error.owner == owner
        raise
