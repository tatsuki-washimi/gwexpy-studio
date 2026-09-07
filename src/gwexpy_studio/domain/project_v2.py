"""Project-v2 readers for native metadata, visual settings, and activities."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from typing import Any, get_args

from ..errors import ProjectFormatError
from .model import ActivityRecord, DataObjectRef, MemberRef, ObjectKind
from .project_fields import (
    _require_bool,
    _require_dict,
    _require_dict_of_dict,
    _require_float,
    _require_list,
    _require_optional_dict,
    _require_optional_str,
    _require_str,
    _require_str_tuple,
)


def record_dict(value: Any) -> Any:
    """Copy a domain record into JSON primitive collections."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            entry.name: record_dict(getattr(value, entry.name))
            for entry in fields(value)
        }
    if isinstance(value, Mapping):
        return {key: record_dict(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [record_dict(item) for item in value]
    return value


def _validate_json(value: Any, path: str, depth: int = 0) -> None:
    """Reject non-data values before worker responses cross into the domain."""
    if depth > 64:
        raise ProjectFormatError(f"{path}: metadata nesting exceeds 64 levels")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProjectFormatError(f"{path}: JSON object keys must be strings")
            _validate_json(item, f"{path}.{key}", depth + 1)
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]", depth + 1)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectFormatError(f"{path}: expected finite JSON number")
    elif value is not None and not isinstance(value, (str, bool, int)):
        raise ProjectFormatError(
            f"{path}: expected JSON data, got {type(value).__name__}"
        )


def _shape(value: Mapping[str, Any], path: str) -> tuple[int, ...]:
    raw = _require_list(value, "shape", path)
    if any(type(dim) is not int or dim < 0 for dim in raw):
        raise ProjectFormatError(
            f"{path}.shape: dimensions must be nonnegative integers"
        )
    return tuple(raw)


def _metadata(value: Mapping[str, Any], path: str) -> dict[str, Any]:
    metadata = (
        dict(_require_dict(value, "metadata", path)) if "metadata" in value else {}
    )
    if "member_count" in metadata:
        count = metadata["member_count"]
        if type(count) is not int or count < 0:
            raise ProjectFormatError(f"{path}.metadata.member_count: invalid count")
    return metadata


def _axes(value: Mapping[str, Any], path: str) -> dict[str, dict[str, Any]]:
    axes = _require_dict_of_dict(value, "axes", path)
    result = {}
    for name, descriptor in axes.items():
        axis_path = f"{path}.axes.{name}"
        if name in ("time", "frequency"):
            if descriptor.get("kind") != "explicit":
                raise ProjectFormatError(f"{axis_path}.kind: expected explicit")
            if type(descriptor.get("length")) is not int or descriptor["length"] < 0:
                raise ProjectFormatError(
                    f"{axis_path}.length: expected nonnegative integer"
                )
            _require_str(descriptor, "dtype", axis_path)
            _require_str(descriptor, "unit", axis_path)
            if "values" in descriptor:
                raise ProjectFormatError(
                    f"{axis_path}: full coordinates belong in native storage"
                )
        elif name in ("t0", "dt", "f0", "df"):
            _require_float(descriptor, "value", axis_path)
            _require_str(descriptor, "unit", axis_path)
        else:
            raise ProjectFormatError(f"{axis_path}: unknown axis descriptor")
        result[name] = dict(descriptor)
    return result


def _scientific_fields(value: Mapping[str, Any], path: str) -> dict[str, Any]:
    kind = _require_str(value, "kind", path)
    if kind not in get_args(ObjectKind):
        raise ProjectFormatError(f"{path}.kind: unsupported native kind: {kind!r}")
    dtype = _require_optional_str(value, "dtype", path)
    unit = _require_optional_str(value, "unit", path)
    if not kind.endswith(("Dict", "List", "Matrix")) and (
        dtype is None or unit is None
    ):
        raise ProjectFormatError(
            f"{path}: single scientific objects require dtype and unit"
        )
    shape = _shape(value, path)
    dimensions = (
        (3, 4)
        if kind == "SpectrogramMatrix"
        else (3,)
        if kind.endswith("Matrix")
        else (2,)
        if kind == "Spectrogram"
        else (1,)
    )
    if len(shape) not in dimensions:
        raise ProjectFormatError(f"{path}.shape: invalid dimensions for {kind}")
    axes = _axes(value, path)
    for name, position in (
        ("time", -2 if kind.startswith("Spectrogram") else -1),
        ("frequency", -1),
    ):
        if name in axes and (
            len(shape) < abs(position) or axes[name]["length"] != shape[position]
        ):
            raise ProjectFormatError(
                f"{path}.axes.{name}: length differs from object shape"
            )
    return {
        "kind": kind,
        "shape": shape,
        "dtype": dtype,
        "unit": unit,
        "name": _require_optional_str(value, "name", path),
        "channel": _require_optional_str(value, "channel", path),
        "axes": axes,
        "metadata": _metadata(value, path),
    }


def _selector(value: Mapping[str, Any], path: str) -> dict[str, str | int]:
    selector = dict(_require_dict(value, "selector", path))
    if set(selector) == {"key"}:
        if type(selector["key"]) not in (str, int):
            raise ProjectFormatError(f"{path}.selector.key: expected string or integer")
    elif set(selector) in ({"index"}, {"batch"}, {"row", "col"}):
        if any(type(index) is not int or index < 0 for index in selector.values()):
            raise ProjectFormatError(
                f"{path}.selector: indices must be nonnegative integers"
            )
    else:
        raise ProjectFormatError(f"{path}.selector: invalid member selector")
    return selector


def object_from_dict(
    value: Mapping[str, Any], path: str = "object_ref"
) -> DataObjectRef:
    """Parse one native parent and optional bounded member snapshots."""
    if not isinstance(value, Mapping):
        raise ProjectFormatError(f"{path}: expected object")
    _validate_json(value, path)
    fields = _scientific_fields(value, path)
    members = []
    raw_members = _require_list(value, "members", path) if "members" in value else ()
    for index, member in enumerate(raw_members):
        member_path = f"{path}.members[{index}]"
        if not isinstance(member, Mapping):
            raise ProjectFormatError(f"{member_path}: expected object")
        members.append(
            MemberRef(
                selector=_selector(member, member_path),
                label=_require_str(member, "label", member_path),
                **_scientific_fields(member, member_path),
            )
        )
        expected = (
            {"key"}
            if fields["kind"].endswith("Dict")
            else {"index"}
            if fields["kind"].endswith("List")
            else {"batch"}
            if fields["kind"] == "SpectrogramMatrix" and len(fields["shape"]) == 3
            else {"row", "col"}
            if fields["kind"].endswith("Matrix")
            else set()
        )
        if set(members[-1].selector) != expected:
            raise ProjectFormatError(
                f"{member_path}.selector: incompatible with parent kind"
            )
    return DataObjectRef(
        object_id=_require_str(value, "object_id", path),
        produced_by=_require_optional_str(value, "produced_by", path),
        members=tuple(members),
        **fields,
    )


def plot_options_from_dict(value: Mapping[str, Any], path: str) -> dict[str, Any]:
    """Read v2 visual fields, retaining defaults for migrated declarations."""
    options = {
        "component": "real",
        "magnitude_scale": "db",
        "db_reference": 1.0,
        "display_unit": None,
        "phase_unwrap": True,
        "selectors": {},
    }
    options.update({key: value[key] for key in options if key in value})
    component = _require_str(options, "component", path)
    magnitude = _require_str(options, "magnitude_scale", path)
    if component not in ("real", "imag", "abs", "phase"):
        raise ProjectFormatError(f"{path}.component: invalid complex component")
    if magnitude not in ("db", "linear"):
        raise ProjectFormatError(f"{path}.magnitude_scale: expected db or linear")
    reference = _require_float(options, "db_reference", path)
    if reference <= 0:
        raise ProjectFormatError(f"{path}.db_reference: expected positive number")
    _require_optional_str(options, "display_unit", path)
    _require_bool(options, "phase_unwrap", path)
    selectors = _require_dict(options, "selectors", path)
    if set(selectors) - set(value["object_ids"]):
        raise ProjectFormatError(f"{path}.selectors: keys must belong to object_ids")
    options["selectors"] = {
        object_id: _selector({"selector": selector}, f"{path}.selectors[{object_id!r}]")
        for object_id, selector in selectors.items()
    }
    return options


def activity_from_dict(value: Mapping[str, Any], path: str) -> ActivityRecord:
    """Read external effects without introducing scientific graph nodes."""
    if not isinstance(value, Mapping):
        raise ProjectFormatError(f"{path}: expected activity object")
    required = {
        key: _require_str(value, key, path)
        for key in ("activity_id", "action", "started_at", "status")
    }
    defaults: dict[str, Any] = {
        "object_id": None,
        "target": None,
        "duration_s": 0.0,
        "details": {},
        "warnings": (),
        "error": None,
    }
    optional = {**defaults, **value}
    duration = _require_float(optional, "duration_s", path)
    if duration < 0:
        raise ProjectFormatError(f"{path}.duration_s: expected nonnegative number")
    return ActivityRecord(
        **required,
        object_id=_require_optional_str(optional, "object_id", path),
        target=_require_optional_str(optional, "target", path),
        duration_s=duration,
        details=dict(_require_dict(optional, "details", path)),
        warnings=_require_str_tuple(optional, "warnings", path),
        error=_require_optional_dict(optional, "error", path),
    )
