"""Closed data-only value encoding used by native I/O and Python recipes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def encode_value(value: Any) -> Any:
    """Encode supported scientific literals without serializing executable code."""
    import numpy as np
    from astropy.units import Quantity

    if isinstance(value, Quantity):
        return {
            "__type__": "quantity",
            "value": encode_value(value.value),
            "unit": str(value.unit),
        }
    if isinstance(value, np.ndarray):
        return encode_value(value.tolist())
    if isinstance(value, np.generic):
        return encode_value(value.item())
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        label = "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
        return {"__type__": "float", "value": label}
    if isinstance(value, complex):
        return {
            "__type__": "complex",
            "real": encode_value(value.real),
            "imag": encode_value(value.imag),
        }
    if isinstance(value, tuple):
        return {"__type__": "tuple", "items": [encode_value(item) for item in value]}
    if isinstance(value, list):
        return [encode_value(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Option mapping keys must be strings")
        return {key: encode_value(item) for key, item in value.items()}
    raise ValueError(f"Unsupported option value type: {type(value).__name__}")


def decode_value(value: Any, *, _depth: int = 0) -> Any:
    """Decode a closed set of typed literals, rejecting executable/unknown tags."""
    from astropy import units as u

    if _depth > 64:
        raise ValueError("Option nesting exceeds 64 levels")

    def nested(item: Any) -> Any:
        return decode_value(item, _depth=_depth + 1)

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Non-finite options require an explicit float tag")
        return value
    if isinstance(value, list):
        return [nested(item) for item in value]
    if not isinstance(value, Mapping):
        raise ValueError(f"Unsupported encoded option: {type(value).__name__}")
    if not all(isinstance(key, str) for key in value):
        raise ValueError("Option mapping keys must be strings")
    tag = value.get("__type__")
    if tag is None:
        return {key: nested(item) for key, item in value.items()}
    expected = {
        "quantity": {"__type__", "value", "unit"},
        "complex": {"__type__", "real", "imag"},
        "tuple": {"__type__", "items"},
        "float": {"__type__", "value"},
    }
    if tag not in expected:
        raise ValueError(f"Unknown option type tag: {tag!r}")
    if set(value) != expected[tag]:
        raise ValueError(f"Invalid fields for option type {tag}")
    if tag == "quantity":
        if not isinstance(value["unit"], str):
            raise ValueError("Quantity unit must be a string")
        return u.Quantity(nested(value["value"]), unit=u.Unit(value["unit"]))
    if tag == "complex":
        real, imag = nested(value["real"]), nested(value["imag"])
        if type(real) not in (int, float) or type(imag) not in (int, float):
            raise ValueError("Complex components must be real numbers")
        return complex(real, imag)
    if tag == "tuple":
        if not isinstance(value["items"], list):
            raise ValueError("Tuple items must be a list")
        return tuple(nested(item) for item in value["items"])
    if value["value"] not in ("nan", "inf", "-inf"):
        raise ValueError("Invalid non-finite float literal")
    return float(value["value"])
