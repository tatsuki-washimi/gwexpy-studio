"""Scientific parameter definitions and safe Qt form value conversion."""

from __future__ import annotations

import json
import math
from typing import Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QWidget

# name, caption, input type, default, choices/unit
Field = tuple[str, str, str, Any, Any]
FFT_FIELDS: tuple[Field, ...] = (
    ("fftlength", "FFT length [s]", "quantity", "", "s"),
    ("overlap", "Overlap [s]", "quantity", "", "s"),
    ("window", "Window", "text", "", None),
)
FILTER_FIELDS: tuple[Field, ...] = (
    ("filtfilt", "Forward/backward (filtfilt)", "bool", True, None),
)
OP_FIELDS: dict[str, tuple[Field, ...]] = {
    "arithmetic": (
        ("operator", "Operator", "choice", "+", ("+", "-", "*", "/", "**")),
        ("operand_mode", "Right operand", "choice", "data", ("data", "scalar")),
        ("scalar", "Finite constant", "number", "1", None),
        ("unit", "Constant unit", "text", "", None),
        ("reverse", "Constant on the left", "bool", False, None),
    ),
    "timeseries.lowpass": (("frequency", "Cutoff [Hz]", "quantity", "", "Hz"),)
    + FILTER_FIELDS,
    "timeseries.highpass": (("frequency", "Cutoff [Hz]", "quantity", "", "Hz"),)
    + FILTER_FIELDS,
    "timeseries.notch": (("frequency", "Notch [Hz]", "quantity", "", "Hz"),)
    + FILTER_FIELDS,
    "timeseries.bandpass": (
        ("flow", "Lower cutoff [Hz]", "quantity", "", "Hz"),
        ("fhigh", "Upper cutoff [Hz]", "quantity", "", "Hz"),
    )
    + FILTER_FIELDS,
    "timeseries.zpk": (
        ("zeros", "Zeros (JSON array)", "json", "[]", None),
        ("poles", "Poles (JSON array)", "json", "[]", None),
        ("gain", "Real gain", "number", "1", None),
        ("analog", "Analog filter", "bool", False, None),
        ("unit", "Analog root unit", "choice", "rad/s", ("rad/s", "Hz")),
        ("normalize_gain", "Normalize gain", "bool", False, None),
    )
    + FILTER_FIELDS,
    "timeseries.psd": FFT_FIELDS
    + (
        (
            "method",
            "Averaging method",
            "choice",
            "median",
            ("median", "welch", "bartlett", "median-mean"),
        ),
    ),
    "timeseries.csd": FFT_FIELDS,
    "timeseries.coherence": FFT_FIELDS,
    "timeseries.transfer_function": FFT_FIELDS
    + (
        ("average", "Averaging", "choice", "mean", ("mean", "median")),
        ("mode", "Response mode", "choice", "steady", ("steady",)),
    ),
    "timeseries.resample": (
        ("rate", "New sample rate [Hz]", "quantity", "", "Hz"),
        ("window", "Window", "text", "", None),
        ("ftype", "Filter type", "choice", "fir", ("fir", "iir")),
        ("n", "Order (optional)", "integer", "", None),
    ),
}
FILTER_OPERATIONS = frozenset(
    {
        "timeseries.lowpass",
        "timeseries.highpass",
        "timeseries.bandpass",
        "timeseries.notch",
        "timeseries.zpk",
    }
)
BINARY_OPERATIONS = frozenset(
    {
        "arithmetic",
        "timeseries.csd",
        "timeseries.coherence",
        "timeseries.transfer_function",
    }
)
ARITHMETIC_IDS = {
    "+": "data.add",
    "-": "data.subtract",
    "*": "data.multiply",
    "/": "data.divide",
    "**": "data.power",
}


def finite_json(text: str) -> Any:
    """Read JSON, rejecting nonstandard nonfinite numeric constants."""

    def reject(value: str) -> None:
        raise ValueError(f"JSON numbers must be finite: {value}")

    def parse_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            reject(value)
        return number

    return json.loads(text, parse_constant=reject, parse_float=parse_float)


def make_field(field: Field, parent: QWidget) -> QWidget:
    """Construct a typed widget from one small declarative field definition."""
    name, _caption, kind, default, options = field
    widget: QWidget
    if kind == "bool":
        widget = QCheckBox(parent)
        widget.setChecked(default)
    elif kind == "choice":
        widget = QComboBox(parent)
        widget.addItems(options)
        widget.setCurrentText(default)
    else:
        widget = QLineEdit(str(default), parent)
        if kind == "json":
            widget.setPlaceholderText('["-1+2j", "-1-2j"]')
        elif kind == "quantity":
            example = "2 kHz" if options == "Hz" else "500 ms"
            widget.setPlaceholderText(f"Number in {options}, or e.g. {example}")
            widget.setToolTip(
                f"Plain numbers use {options}. Include a space and unit to convert."
            )
    widget.setObjectName(f"parameter_{name}")
    return widget


def field_value(widget: QWidget, field: Field) -> Any:
    """Convert a field to detached JSON-compatible data without evaluation."""
    name, _caption, kind, _default, unit = field
    if isinstance(widget, QCheckBox):
        return widget.isChecked()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    assert isinstance(widget, QLineEdit)
    text = widget.text().strip()
    if not text:
        return None
    if kind == "json":
        value = finite_json(text)
        if not isinstance(value, list):
            raise ValueError(f"{name} must be a JSON array")
        return value
    if kind in {"number", "quantity", "integer"}:
        parts = text.split(maxsplit=1) if kind == "quantity" else [text]
        value = float(parts[0])
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if kind == "integer":
            if not value.is_integer():
                raise ValueError(f"{name} must be an integer")
            return int(value)
        return (
            {"value": value, "unit": parts[1] if len(parts) > 1 else unit}
            if kind == "quantity"
            else value
        )
    return text
