"""Preview data model and boundary validation for GUI presentation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..domain.model import DataObjectRef, PlotSpec

_VALID_KINDS: frozenset[str] = frozenset(
    {"TimeSeries", "FrequencySeries", "Spectrogram"}
)


def _validate_axis(
    axes: dict[str, Any],
    axis_name: str,
    *,
    expected_unit: str,
    must_be_positive: bool = False,
) -> None:
    """Validate one axis descriptor against exact unit and numerical bounds."""
    if axis_name not in axes:
        raise ValueError(f"Missing required axis {axis_name!r}")
    axis_val = axes[axis_name]
    if not isinstance(axis_val, dict):
        raise ValueError(f"Axis {axis_name!r} descriptor must be a dict")
    if "value" not in axis_val or "unit" not in axis_val:
        raise ValueError(f"Axis {axis_name!r} must contain 'value' and 'unit'")

    val = axis_val["value"]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(
            f"Axis {axis_name!r} value must be a real finite non-bool number"
        )
    if not math.isfinite(val):
        raise ValueError(f"Axis {axis_name!r} value must be finite")
    if must_be_positive and val <= 0:
        raise ValueError(
            f"Axis {axis_name!r} step value must be strictly positive, got {val}"
        )

    unit_str = axis_val["unit"]
    if unit_str != expected_unit:
        raise ValueError(
            f"Axis {axis_name!r} unit must be {expected_unit!r}, got {unit_str!r}"
        )


@dataclass(frozen=True, slots=True)
class PreviewData:
    """Immutable preview data container holding a detached array and metadata."""

    ref: DataObjectRef
    values: np.ndarray
    unit: str
    x_coordinates: np.ndarray | None = None
    y_coordinates: np.ndarray | None = None

    def __post_init__(self) -> None:
        """Validate preview properties against domain reference and physical axes."""
        if not isinstance(self.ref, DataObjectRef):
            raise TypeError("ref must be an instance of DataObjectRef")
        if self.ref.kind not in _VALID_KINDS:
            raise ValueError(f"Unsupported preview kind: {self.ref.kind!r}")
        if not isinstance(self.values, np.ndarray):
            raise TypeError("values must be a numpy ndarray")

        if self.values.shape != self.ref.shape:
            raise ValueError(
                f"Array shape {self.values.shape} does not match "
                f"ref shape {self.ref.shape}"
            )
        if self.values.dtype != np.dtype(self.ref.dtype):
            raise ValueError(
                f"Array dtype {self.values.dtype} does not match "
                f"ref dtype {self.ref.dtype}"
            )
        if self.unit != self.ref.unit:
            raise ValueError(
                f"Preview unit {self.unit!r} does not match ref unit {self.ref.unit!r}"
            )

        axes = dict(self.ref.axes)
        for coordinates, length, label in (
            (self.x_coordinates, self.values.shape[0], "x"),
            (self.y_coordinates, self.values.shape[-1], "y"),
        ):
            if coordinates is not None:
                if not isinstance(coordinates, np.ndarray) or coordinates.shape != (
                    length,
                ):
                    raise ValueError(f"Invalid {label} coordinate shape")
                if coordinates.dtype.kind not in "iuf" or not np.all(
                    np.isfinite(coordinates)
                ):
                    raise ValueError(f"Invalid {label} coordinates")
        if self.x_coordinates is not None and (
            self.ref.kind != "Spectrogram" or self.y_coordinates is not None
        ):
            return
        if self.ref.kind == "TimeSeries":
            _validate_axis(axes, "t0", expected_unit="s")
            _validate_axis(axes, "dt", expected_unit="s", must_be_positive=True)
        elif self.ref.kind == "FrequencySeries":
            _validate_axis(axes, "f0", expected_unit="Hz")
            _validate_axis(axes, "df", expected_unit="Hz", must_be_positive=True)
        elif self.ref.kind == "Spectrogram":
            _validate_axis(axes, "t0", expected_unit="s")
            _validate_axis(axes, "dt", expected_unit="s", must_be_positive=True)
            _validate_axis(axes, "f0", expected_unit="Hz")
            _validate_axis(axes, "df", expected_unit="Hz", must_be_positive=True)


@dataclass(frozen=True, slots=True)
class PreviewPayload:
    """Immutable preview data and its complete ephemeral rendering spec."""

    preview: PreviewData
    spec: PlotSpec
