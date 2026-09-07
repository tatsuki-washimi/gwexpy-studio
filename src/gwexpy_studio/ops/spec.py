"""Shared operation parameter and rendering contracts."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from astropy import units as u

from .values import decode_value, encode_value

ParamKind = Literal[
    "quantity",
    "float",
    "int",
    "str",
    "enum",
    "gps",
    "source",
    "bool",
    "json",
    "literal",
]
EmitContext = Mapping[str, Any]
ApplyFunction = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]
EmitFunction = Callable[[EmitContext], str]


@dataclass(frozen=True, kw_only=True, slots=True)
class ParamSpec:
    """Serializable description of one operation parameter."""

    name: str
    kind: ParamKind
    canonical_unit: str | None = None
    required: bool = False
    default: Any = None
    choices: tuple[str, ...] | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class OperationSpec:
    """Shared apply/emit contract for a stable operation ID."""

    operation_id: str
    schema_version: int
    input_roles: tuple[str, ...]
    result_kind: str
    params: tuple[ParamSpec, ...]
    apply: ApplyFunction
    emit: EmitFunction
    optional_input_roles: tuple[str, ...] = ()
    accepted_input_kinds: Mapping[str, tuple[str, ...]] | None = None


def quantity_to_canonical_float(value: Any, *, canonical_unit: str) -> float:
    """Normalize a quantity, mapping, or scalar to one canonical float."""
    target_unit = u.Unit(canonical_unit)
    if isinstance(value, Mapping):
        v = value.get("value")
        u_str = value.get("unit")
        if v is None or u_str is None or type(v) is bool:
            raise ValueError(f"Invalid quantity mapping: {value!r}")
        quantity = float(v) * u.Unit(u_str)
    elif isinstance(value, u.Quantity):
        quantity = value
    elif isinstance(value, (int, float)) and type(value) is not bool:
        quantity = float(value) * target_unit
    else:
        raise ValueError(f"Cannot convert {value!r} to quantity")

    try:
        converted = quantity.to(target_unit)
    except u.UnitConversionError as exc:
        raise ValueError(f"Incompatible unit: {exc}") from exc

    val = float(converted.value)
    if not math.isfinite(val):
        raise ValueError(f"Non-finite float quantity: {val}")
    return val


def render_param(name: str, value: Any, *, spec: ParamSpec) -> str:
    """Render a parameter literal for standalone code export."""
    if spec.kind == "str":
        return f"{name}={repr(str(value))}"
    if spec.kind in ("quantity", "float", "gps"):
        f_val = float(value)
        return f"{name}={repr(f_val)}"
    if spec.kind == "int":
        return f"{name}={int(value)}"
    return f"{name}={repr(value)}"


def normalize_params(
    params: Mapping[str, Any], *, spec: OperationSpec
) -> Mapping[str, Any]:
    """Normalize and validate operation parameters according to their spec."""
    if spec.schema_version != 1:
        raise ValueError(
            f"Unsupported operation schema version: {spec.schema_version} (expected 1)"
        )

    spec_by_name = {p.name: p for p in spec.params}

    # 1. Check for unknown params
    unknown_keys = set(params.keys()) - set(spec_by_name.keys())
    if unknown_keys:
        msg = f"Unknown parameters for {spec.operation_id}: {sorted(unknown_keys)}"
        raise ValueError(msg)

    normalized: dict[str, Any] = {}

    # 2. Check required and normalize each param
    for name, param_spec in spec_by_name.items():
        if name not in params:
            if param_spec.required:
                raise ValueError(
                    f"Missing required parameter '{name}' for {spec.operation_id}"
                )
            if param_spec.default is not None:
                normalized[name] = param_spec.default
            continue

        raw_val = params[name]
        if raw_val is None:
            if param_spec.required:
                raise ValueError(f"Required parameter '{name}' cannot be None")
            normalized[name] = None
            continue

        if type(raw_val) is bool and param_spec.kind in (
            "quantity",
            "float",
            "int",
            "gps",
        ):
            raise TypeError(
                f"Boolean value {raw_val!r} not accepted for numeric param '{name}'"
            )

        if param_spec.kind in ("quantity", "gps"):
            unit = param_spec.canonical_unit or "s"
            normalized[name] = quantity_to_canonical_float(raw_val, canonical_unit=unit)
        elif param_spec.kind == "float":
            f_val = float(raw_val)
            if not math.isfinite(f_val):
                raise ValueError(f"Non-finite float for param '{name}': {f_val}")
            normalized[name] = f_val
        elif param_spec.kind == "int":
            normalized[name] = int(raw_val)
        elif param_spec.kind == "enum":
            str_val = str(raw_val)
            if param_spec.choices is not None and str_val not in param_spec.choices:
                raise ValueError(
                    f"Invalid enum value '{str_val}' for param '{name}'. "
                    f"Choices: {param_spec.choices}"
                )
            normalized[name] = str_val
        elif param_spec.kind == "str":
            normalized[name] = str(raw_val)
        elif param_spec.kind == "bool":
            if type(raw_val) is not bool:
                raise TypeError(f"Parameter '{name}' requires a bool")
            normalized[name] = raw_val
        elif param_spec.kind in ("json", "literal"):
            encoded = encode_value(raw_val)
            decode_value(encoded)
            normalized[name] = encoded
        else:
            normalized[name] = raw_val

    # Operation-specific constraints (e.g. crop start < end)
    if spec.operation_id == "timeseries.crop":
        start_val = normalized.get("start")
        end_val = normalized.get("end")
        if start_val is not None and end_val is not None and start_val >= end_val:
            raise ValueError(
                f"Crop start ({start_val}) must be strictly less than end ({end_val})"
            )

    return normalized
