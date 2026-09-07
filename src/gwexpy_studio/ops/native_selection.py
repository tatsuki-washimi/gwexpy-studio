"""Native member selection and legacy operation mapping for replay."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .native_containers import (
    science_copy,
    science_kind,
    science_map,
    science_members,
)


def native_extract(value: Any, selector: Mapping[str, Any]) -> Any:
    """Copy exactly one member identified by a strict native selector."""
    kind = science_kind(value)
    if not isinstance(selector, Mapping):
        raise ValueError("Member selector must be an object")
    if kind.endswith("Dict"):
        if set(selector) != {"key"} or type(selector["key"]) not in (str, int):
            raise ValueError("Dictionary selector requires one string or integer key")
        return science_copy(value[selector["key"]])
    if kind.endswith("List"):
        expected = {"index"}
        key: Any = selector.get("index")
    elif kind.endswith("Matrix"):
        batch = kind == "SpectrogramMatrix" and value.ndim == 3
        expected = {"batch"} if batch else {"row", "col"}
        key = (
            (selector.get("batch"),)
            if batch
            else (selector.get("row"), selector.get("col"))
        )
    else:
        raise ValueError("Member extraction requires a container")
    if set(selector) != expected or any(
        type(index) is not int or index < 0 for index in selector.values()
    ):
        raise ValueError(
            "Member indices must be nonnegative integers for this container"
        )
    members = dict(science_members(value))
    if key not in members:
        raise IndexError("Member index is outside the container")
    return members[key]


def native_legacy(
    operation: str, inputs: Mapping[str, Any], params: Mapping[str, Any]
) -> Any:
    """Map established TimeSeries operations through the public leaf APIs."""
    if operation not in ("crop", "detrend", "asd", "spectrogram"):
        raise ValueError(f"Unsupported legacy operation: {operation}")

    def apply_leaf(leaf: Any) -> Any:
        if science_kind(leaf) != "TimeSeries":
            raise TypeError("This operation requires TimeSeries inputs")
        kwargs = {key: value for key, value in params.items() if value is not None}
        if operation == "crop":
            start, end = kwargs.get("start"), kwargs.get("end")
            t0 = float(leaf.t0.to_value("s"))
            stop = t0 + float(leaf.duration.to_value("s"))
            if start is not None and not t0 <= start <= stop:
                raise ValueError(
                    f"Crop start {start} is outside series span [{t0}, {stop}]"
                )
            if end is not None and not t0 <= end <= stop:
                raise ValueError(
                    f"Crop end {end} is outside series span [{t0}, {stop}]"
                )
            if start is not None and end is not None and start >= end:
                raise ValueError(
                    f"Crop start ({start}) must be strictly less than end ({end})"
                )
        if operation == "detrend":
            return leaf.detrend(kwargs.get("detrend", "constant"))
        return getattr(leaf, operation)(**kwargs)

    return science_map(inputs["self"], apply_leaf)
