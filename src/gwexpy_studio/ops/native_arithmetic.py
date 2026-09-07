"""Elementwise arithmetic delegated to native GWexpy leaf operators."""

from __future__ import annotations

import operator
from collections.abc import Mapping
from typing import Any

import numpy as np
from astropy import units as u

from .native_containers import science_map, science_pairs, science_rebuild
from .values import decode_value

SCIENCE_OPERATORS = {
    "add": operator.add,
    "subtract": operator.sub,
    "multiply": operator.mul,
    "divide": operator.truediv,
    "power": operator.pow,
}


def science_scalar(value: Any) -> Any:
    """Decode a unitful scalar without accepting an array or boolean."""
    if isinstance(value, Mapping) and "__type__" in value:
        value = decode_value(value)
    if isinstance(value, Mapping):
        if set(value) == {"real", "imag"}:
            value = complex(value["real"], value["imag"])
        elif "value" in value and "unit" in value:
            value = science_scalar(value["value"]) * u.Unit(value["unit"])
        else:
            raise ValueError("Scalar mappings require value and unit")
    if (
        isinstance(value, (bool, np.bool_))
        or not np.isscalar(value)
        and not isinstance(value, u.Quantity)
    ):
        raise TypeError("Scalar operand must be a number or scalar quantity")
    if isinstance(value, u.Quantity):
        if not value.isscalar:
            raise ValueError("Scalar operand cannot broadcast an array")
    elif not isinstance(value, (int, float, complex, np.number)):
        raise TypeError("Scalar operand must be numeric")
    return value


def science_arithmetic(
    operation: str, inputs: Mapping[str, Any], params: Mapping[str, Any]
) -> Any:
    """Apply a native operator to strict pairs or a scalar in the chosen order."""
    native_operator = SCIENCE_OPERATORS[operation]
    reverse = params.get("reverse", False)
    if type(reverse) is not bool:
        raise TypeError("reverse requires a bool")

    def apply_pair(first: Any, second: Any) -> Any:
        return (
            native_operator(second, first)
            if reverse
            else native_operator(first, second)
        )

    mode = params.get("operand_mode", "data")
    if mode == "scalar":
        if "other" in inputs:
            raise ValueError("Scalar mode cannot also receive another data operand")
        if "scalar" not in params:
            raise ValueError("Scalar mode requires scalar")
        scalar = science_scalar(params["scalar"])
        return science_map(inputs["self"], lambda leaf: apply_pair(leaf, scalar))
    if mode != "data" or "other" not in inputs:
        raise ValueError("Data mode requires an other operand")
    if params.get("scalar") is not None:
        raise ValueError("Data mode cannot also receive a scalar")
    pairs = science_pairs(inputs["self"], inputs["other"])
    results = [(key, apply_pair(left, right)) for key, left, right in pairs]
    return science_rebuild(inputs["self"], results)
