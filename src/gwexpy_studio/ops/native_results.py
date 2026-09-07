"""Execution values with JSON-safe scientific provenance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScientificResult:
    """Carry the native result and reproducible execution details together."""

    value: Any
    details: Mapping[str, Any]
