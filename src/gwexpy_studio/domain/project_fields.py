"""Strict primitive-field readers shared by versioned project parsers."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from ..errors import ProjectFormatError

_VALID_OBJECT_KINDS: frozenset[str] = frozenset(
    {"TimeSeries", "FrequencySeries", "Spectrogram"}
)
_VALID_SOURCE_FORMATS: frozenset[str] = frozenset({"hdf5", "csv_enhanced"})
_VALID_PLOT_KINDS: frozenset[str] = frozenset({"line", "spectrogram"})

_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "project_id",
        "created",
        "modified",
        "compatibility",
        "sources",
        "objects",
        "operations",
        "executions",
        "plots",
        "ui_state",
    }
)

_ALLOWED_UNIT_TOKENS: frozenset[str] = frozenset(
    {
        "",
        "m",
        "s",
        "Hz",
        "kg",
        "g",
        "rad",
        "deg",
        "sr",
        "count",
        "ct",
        "counts",
        "V",
        "W",
        "A",
        "K",
        "J",
        "N",
        "Pa",
        "T",
        "G",
        "C",
        "F",
        "Ohm",
        "H",
        "lm",
        "lx",
        "Bq",
        "Gy",
        "Sv",
        "kat",
        "min",
        "h",
        "d",
        "yr",
        "au",
        "pc",
        "ly",
        "Jy",
        "mag",
        "dB",
        "dimensionless",
        "sqrt",
        "cm",
        "mm",
        "um",
        "nm",
        "pm",
        "fm",
        "km",
        "kHz",
        "MHz",
        "GHz",
        "THz",
        "mV",
        "uV",
        "nV",
        "pV",
        "kW",
        "MW",
        "GW",
        "ms",
        "us",
        "ns",
        "ps",
        "fs",
        "n",
        "u",
        "p",
        "f",
        "k",
        "M",
        "G",
        "T",
    }
)


def _is_valid_unit_string(unit_str: str) -> bool:
    """Validate unit expression syntax using pure token parsing."""
    if not isinstance(unit_str, str):
        return False
    if not unit_str or unit_str.strip() == "":
        return True
    if not re.fullmatch(r"^[A-Za-z0-9_\(\)\/\*\^\+\-\.\s]+$", unit_str):
        return False
    words = re.findall(r"[A-Za-z]+", unit_str)
    for word in words:
        if word not in _ALLOWED_UNIT_TOKENS:
            return False
    return True


def _check_finite_recursive(value: Any) -> None:
    """Ensure no float NaN or infinite values exist in data structures."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectFormatError(f"Non-finite float value encountered: {value}")
    elif isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ProjectFormatError(f"Dictionary key must be string: {k!r}")
            _check_finite_recursive(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite_recursive(item)


# --- Strict type-checking helpers for Project.from_dict --------------------
#
# These replace bare str()/int()/float()/bool()/dict()/tuple() coercions,
# which silently mask type mismatches (e.g. a JSON string "NaN" surviving
# float("NaN") as a non-finite value) and missing required keys (e.g. a
# dropped "object_id" silently becoming ""). Every field parsed through
# from_dict must go through one of these so that a type mismatch or a
# missing required key raises ProjectFormatError with a JSON-path-style
# location, never a bare Python exception and never a silently-coerced
# default. `bool` is a subclass of `int` in Python, so every int/float
# check below explicitly excludes `type(x) is bool` first.


def _field_path(path: str, key: str) -> str:
    """Build a dotted JSON-path-style label for a nested document field."""
    return f"{path}.{key}" if path else key


def _require_present(container: Mapping[str, Any], key: str, path: str) -> Any:
    """Return the raw value for a required key, or raise if it is absent."""
    if key not in container:
        raise ProjectFormatError(f"{_field_path(path, key)}: required field is missing")
    return container[key]


def _require_str(container: Mapping[str, Any], key: str, path: str) -> str:
    """Return a required field's value, rejecting anything but ``str``."""
    value = _require_present(container, key, path)
    if not isinstance(value, str):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected str, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value


def _require_optional_str(
    container: Mapping[str, Any], key: str, path: str
) -> str | None:
    """Return a required field whose value must be ``str`` or ``None``."""
    value = _require_present(container, key, path)
    if value is not None and not isinstance(value, str):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected str or null, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value


def _require_int(container: Mapping[str, Any], key: str, path: str) -> int:
    """Return a required field's value, rejecting anything but ``int``."""
    value = _require_present(container, key, path)
    if type(value) is bool or not isinstance(value, int):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected int, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value


def _require_optional_int(
    container: Mapping[str, Any], key: str, path: str
) -> int | None:
    """Return a required field whose value must be ``int`` or ``None``."""
    value = _require_present(container, key, path)
    if value is None:
        return None
    if type(value) is bool or not isinstance(value, int):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected int or null, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value


def _require_float(container: Mapping[str, Any], key: str, path: str) -> float:
    """Return a required numeric field as a finite ``float``.

    Type is checked before the finiteness check, so a non-numeric value
    (e.g. the string ``"NaN"``) is rejected as a type error rather than
    being parsed into a non-finite float.
    """
    value = _require_present(container, key, path)
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected number, got "
            f"{type(value).__name__}: {value!r}"
        )
    result = float(value)
    if not math.isfinite(result):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: non-finite value: {result}"
        )
    return result


def _require_optional_float(
    container: Mapping[str, Any], key: str, path: str
) -> float | None:
    """Return a required field whose value must be a finite number or ``None``."""
    value = _require_present(container, key, path)
    if value is None:
        return None
    if type(value) is bool or not isinstance(value, (int, float)):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected number or null, got "
            f"{type(value).__name__}: {value!r}"
        )
    result = float(value)
    if not math.isfinite(result):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: non-finite value: {result}"
        )
    return result


def _require_bool(container: Mapping[str, Any], key: str, path: str) -> bool:
    """Return a required field's value, rejecting anything but ``bool``."""
    value = _require_present(container, key, path)
    if not isinstance(value, bool):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected bool, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value


def _require_dict(
    container: Mapping[str, Any], key: str, path: str
) -> Mapping[str, Any]:
    """Return a required field's value, rejecting anything but a str-keyed mapping."""
    value = _require_present(container, key, path)
    if not isinstance(value, Mapping):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected object, got "
            f"{type(value).__name__}: {value!r}"
        )
    for k in value:
        if not isinstance(k, str):
            raise ProjectFormatError(
                f"{_field_path(path, key)}: object key must be str, got {k!r}"
            )
    return value


def _require_optional_dict(
    container: Mapping[str, Any], key: str, path: str
) -> Mapping[str, Any] | None:
    """Return a required field whose value must be a str-keyed mapping or ``None``."""
    value = _require_present(container, key, path)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected object or null, got "
            f"{type(value).__name__}: {value!r}"
        )
    for k in value:
        if not isinstance(k, str):
            raise ProjectFormatError(
                f"{_field_path(path, key)}: object key must be str, got {k!r}"
            )
    return value


def _require_dict_of_str(
    container: Mapping[str, Any], key: str, path: str
) -> Mapping[str, str]:
    """Return a required field's value as a mapping with every value a ``str``."""
    raw = _require_dict(container, key, path)
    for k, v in raw.items():
        if not isinstance(v, str):
            raise ProjectFormatError(
                f"{_field_path(path, key)}.{k}: expected str value, got "
                f"{type(v).__name__}: {v!r}"
            )
    return raw


def _require_dict_of_dict(
    container: Mapping[str, Any], key: str, path: str
) -> Mapping[str, Mapping[str, Any]]:
    """Return a required field's value as a mapping whose every value is a mapping."""
    raw = _require_dict(container, key, path)
    for k, v in raw.items():
        if not isinstance(v, Mapping):
            raise ProjectFormatError(
                f"{_field_path(path, key)}.{k}: expected object value, got "
                f"{type(v).__name__}: {v!r}"
            )
    return raw


def _require_list(
    container: Mapping[str, Any], key: str, path: str
) -> list[Any] | tuple[Any, ...]:
    """Return a required field's value, rejecting anything but a list/tuple."""
    value = _require_present(container, key, path)
    if not isinstance(value, (list, tuple)):
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected array, got "
            f"{type(value).__name__}: {value!r}"
        )
    return value


def _require_str_tuple(
    container: Mapping[str, Any], key: str, path: str
) -> tuple[str, ...]:
    """Return a required array field as a tuple of ``str`` elements.

    Rejects a bare string value outright (via the list/tuple type check in
    ``_require_list``) so a string can no longer be silently split into a
    tuple of one-character strings by ``tuple(str_value)``.
    """
    raw = _require_list(container, key, path)
    result: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise ProjectFormatError(
                f"{_field_path(path, key)}[{index}]: expected str, got "
                f"{type(item).__name__}: {item!r}"
            )
        result.append(item)
    return tuple(result)


def _require_optional_limit(
    container: Mapping[str, Any], key: str, path: str
) -> tuple[float, float] | None:
    """Return a required ``[min, max]`` numeric pair field, or ``None``."""
    value = _require_present(container, key, path)
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ProjectFormatError(
            f"{_field_path(path, key)}: expected a 2-element array or null, "
            f"got {value!r}"
        )
    result: list[float] = []
    for index, item in enumerate(value):
        if type(item) is bool or not isinstance(item, (int, float)):
            raise ProjectFormatError(
                f"{_field_path(path, key)}[{index}]: expected number, got "
                f"{type(item).__name__}: {item!r}"
            )
        num = float(item)
        if not math.isfinite(num):
            raise ProjectFormatError(
                f"{_field_path(path, key)}[{index}]: non-finite value: {num}"
            )
        result.append(num)
    return (result[0], result[1])
