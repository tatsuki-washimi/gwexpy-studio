"""Assertions shared by scaffold contract items."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from gwexpy_studio.errors import PrototypeNotImplementedError


def assert_prototype_not_implemented(
    callback: Callable[[], Any], *, code: str, owner: str
) -> None:
    """Assert the exact sentinel type and metadata for a scaffold entry point."""
    with pytest.raises(PrototypeNotImplementedError) as raised:
        callback()

    error = raised.value
    assert type(error) is PrototypeNotImplementedError
    assert error.code == code
    assert error.owner == owner
