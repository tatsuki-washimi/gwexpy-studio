"""Worker-owned native objects and bounded member metadata."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import replace
from itertools import islice
from typing import Any, cast, get_args

import numpy as np

from ..domain.model import DataObjectRef, MemberRef, ObjectKind


def _member_selectors(value: Any) -> Iterator[tuple[dict[str, str | int], str]]:
    """Yield stable selectors without constructing native slices."""
    kind = type(value).__name__
    if kind.endswith("Dict"):
        for key in value:
            if type(key) not in (str, int):
                raise ValueError("Native dictionary keys must be strings or integers")
            yield {"key": key}, str(key)
    elif kind.endswith("List"):
        for index in range(len(value)):
            yield {"index": index}, str(index)
    elif kind.endswith("Matrix"):
        sample_dims = 2 if kind.startswith("Spectrogram") else 1
        leading = tuple(value.shape[:-sample_dims])
        if len(leading) == 1:
            for batch in range(leading[0]):
                yield {"batch": batch}, str(batch)
        elif len(leading) == 2:
            rows, cols = tuple(value.row_keys()), tuple(value.col_keys())
            for row in range(leading[0]):
                for col in range(leading[1]):
                    yield (
                        {"row": row, "col": col},
                        f"{rows[row]}, {cols[col]}",
                    )
        else:
            raise ValueError(f"Unsupported native matrix member dimensions: {leading}")


def _axes(value: Any) -> dict[str, dict[str, Any]]:
    """Describe native axes without serializing full coordinate arrays."""
    axes: dict[str, dict[str, Any]] = {}
    for name, prop, origin, step, unit in (
        ("time", "times", "t0", "dt", "s"),
        ("frequency", "frequencies", "f0", "df", "Hz"),
    ):
        coords = getattr(value, prop, None)
        if coords is None:
            continue
        values = np.asarray(getattr(coords, "value", coords))
        cadence = getattr(value, step, None)
        if cadence is not None and _is_regular(
            values, cadence, str(getattr(coords, "unit", unit))
        ):
            start = getattr(value, origin, None)
            if start is not None:
                axes[origin] = {"value": _axis_scalar(start, unit), "unit": unit}
            axes[step] = {"value": _axis_scalar(cadence, unit), "unit": unit}
        else:
            axes[name] = {
                "kind": "explicit",
                "length": len(values),
                "dtype": str(values.dtype),
                "unit": str(getattr(coords, "unit", unit)),
            }
    return axes


def _is_regular(values: np.ndarray, cadence: Any, unit: str) -> bool:
    """Only advertise a regular grid if it reproduces native coordinates."""
    if len(values) < 2:
        return True
    step = (
        cadence.to_value(unit)
        if str(getattr(cadence, "unit", ""))
        else np.asarray(cadence)
    )
    if np.issubdtype(values.dtype, np.integer):
        if not np.isfinite(step) or step != int(step):
            return False
        # Python integers avoid both overflow and implicit float64 comparison
        # rounding when native coordinates exceed 2**53.
        return bool(np.all(np.diff(values.astype(object)) == int(step)))
    expected = values[0] + np.arange(len(values), dtype=values.dtype) * step
    return bool(np.array_equal(values, expected))


def _axis_scalar(value: Any, unit: str) -> float:
    """Read a quantity or native numeric coordinate in its documented unit."""
    if str(getattr(value, "unit", "")):
        return float(value.to_value(unit))
    return float(value)


def _summary(value: Any) -> dict[str, Any]:
    """Snapshot class, axes, and scientific metadata without coercing data."""
    kind = type(value).__name__
    if kind not in get_args(ObjectKind):
        raise ValueError(f"Unsupported native object kind: {kind}")
    container = kind.endswith(("Dict", "List", "Matrix"))
    metadata: dict[str, Any] = {"native_class": f"{type(value).__module__}.{kind}"}
    shape = tuple(value.shape) if hasattr(value, "shape") else (len(value),)
    dtype: str | None
    unit: str | None
    units: set[str | None]
    if kind.endswith("Matrix"):
        units = {
            str(unit) if unit is not None else None
            for unit in np.asarray(value.units).flat
        }
        dtype = str(value.value.dtype)
        unit = next(iter(units)) if len(units) == 1 else None
        sample_dims = 2 if kind.startswith("Spectrogram") else 1
        metadata["member_count"] = int(np.prod(shape[:-sample_dims]))
        metadata["row_labels"] = [str(key) for key in value.row_keys()]
        metadata["column_labels"] = [str(key) for key in value.col_keys()]
    elif container:
        dtypes: set[str | None] = set()
        units = set()
        count = 0
        for member in value.values() if kind.endswith("Dict") else value:
            dtypes.add(str(member.value.dtype))
            units.add(
                str(member.unit) if getattr(member, "unit", None) is not None else None
            )
            count += 1
        dtype = next(iter(dtypes)) if len(dtypes) == 1 else None
        unit = next(iter(units)) if len(units) == 1 else None
        metadata["member_count"] = count
    else:
        dtype = str(value.value.dtype)
        unit = str(value.unit) if getattr(value, "unit", None) is not None else None
    channel = getattr(value, "channel", None)
    return {
        "kind": cast(ObjectKind, kind),
        "shape": shape,
        "dtype": dtype,
        "unit": unit,
        "name": getattr(value, "name", None),
        "channel": str(channel) if channel is not None else None,
        "axes": _axes(value) if not kind.endswith(("Dict", "List")) else {},
        "metadata": metadata,
    }


class ObjectStore:
    """Keep scientific parents native; materialize copied members on request."""

    def __init__(self) -> None:
        """Initialize an empty ObjectStore."""
        self._objects: dict[str, Any] = {}
        self._refs: dict[str, DataObjectRef] = {}
        self._next_id = 1

    def put(
        self,
        value: Any,
        *,
        produced_by: str | None = None,
        object_id: str | None = None,
    ) -> DataObjectRef:
        """Store one native scientific object and return its metadata reference."""
        summary = _summary(value)
        if object_id is None:
            while f"obj-{self._next_id}" in self._objects:
                self._next_id += 1
            object_id = f"obj-{self._next_id}"
            self._next_id += 1
        ref = DataObjectRef(object_id=object_id, produced_by=produced_by, **summary)
        self._objects[object_id] = value
        self._refs[object_id] = ref
        return ref

    def get(self, object_id: str) -> Any:
        """Retrieve a worker-owned native object by its ID."""
        if object_id not in self._objects:
            raise KeyError(f"Object not found in store: {object_id!r}")
        return self._objects[object_id]

    def describe(self, object_id: str) -> DataObjectRef:
        """Return the stored metadata snapshot without creating children."""
        self.get(object_id)
        return self._refs[object_id]

    def list_members(
        self, object_id: str, *, offset: int = 0, limit: int = 100
    ) -> tuple[MemberRef, ...]:
        """Describe a bounded page of members without storing or copying them."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 0:
            raise ValueError("Member offset and limit must be nonnegative integers")
        items = islice(_member_selectors(self.get(object_id)), offset, offset + limit)
        return tuple(
            MemberRef(
                selector=selector,
                label=label,
                **_summary(self._select_member(object_id, selector)),
            )
            for selector, label in items
        )

    def _select_member(self, object_id: str, selector: Mapping[str, Any]) -> Any:
        value = self.get(object_id)
        kind = type(value).__name__
        if not isinstance(selector, Mapping):
            raise ValueError("Member selector must be a mapping")
        if kind.endswith("Dict") and set(selector) == {"key"}:
            key = selector["key"]
            if type(key) not in (str, int):
                raise ValueError("Dictionary member key must be a string or integer")
            return value[key]
        expected = {"index"} if kind.endswith("List") else {"row", "col"}
        if kind.endswith("Matrix"):
            sample_dims = 2 if kind.startswith("Spectrogram") else 1
            expected = {"batch"} if len(value.shape) - sample_dims == 1 else expected
        if not kind.endswith(("List", "Matrix")) or set(selector) != expected:
            raise ValueError(f"Invalid selector for {kind}: {selector!r}")
        if any(type(v) is not int or v < 0 for v in selector.values()):
            raise ValueError("Member indices must be nonnegative integers")
        index = (
            (selector["row"], selector["col"])
            if expected == {"row", "col"}
            else next(iter(selector.values()))
        )
        if kind == "SpectrogramMatrix":
            from gwexpy.spectrogram import Spectrogram

            if value.times is None or value.frequencies is None:
                raise ValueError(
                    "SpectrogramMatrix member selection requires explicit native "
                    "time and frequency coordinates"
                )
            # The native matrix selector rejects independent epoch metadata.
            # Construct only the requested view using public scientific axes.
            meta = value.meta[index if isinstance(index, tuple) else (index, 0)]
            selected = Spectrogram(
                value.value[index],
                times=value.times,
                frequencies=value.frequencies,
                unit=meta.unit,
                name=meta.name,
                channel=meta.channel,
                copy=False,
            )
            if hasattr(value, "attrs"):
                selected.attrs = value.attrs
        else:
            selected = value[index]
        if kind.endswith("Matrix"):
            # Native scalar selection reconstructs from rounded coordinates.
            # Restore the detached scalar quantities without resetting axes.
            for attribute in ("t0", "dt", "f0", "df"):
                original = getattr(value, attribute, None)
                copied = getattr(selected, attribute, None)
                if original is not None and copied is not None:
                    copied[...] = (
                        original
                        if str(getattr(original, "unit", ""))
                        else getattr(original, "value", original) * copied.unit
                    )
        return selected

    def get_member(self, object_id: str, selector: Mapping[str, Any]) -> Any:
        """Return a detached native member, leaving the parent and store unchanged."""
        # GWexpy leaf.copy() reconstructs cadence from rounded coordinates.
        # The native ndarray copy retains exact cached cadence and detaches axes.
        member = self._select_member(object_id, selector)
        result = np.ndarray.copy(member)
        if hasattr(member, "attrs"):
            result.attrs = deepcopy(member.attrs)
        return result

    def extract_member(
        self,
        object_id: str,
        selector: Mapping[str, Any],
        *,
        produced_by: str | None = None,
        object_id_out: str | None = None,
    ) -> DataObjectRef:
        """Store a detached native member with its parent and selector recipe."""
        ref = self.put(
            self.get_member(object_id, selector),
            produced_by=produced_by,
            object_id=object_id_out,
        )
        ref = replace(
            ref,
            metadata={
                **ref.metadata,
                "parent_object_id": object_id,
                "member_selector": dict(selector),
            },
        )
        self._refs[ref.object_id] = ref
        return ref

    def get_axis_values(
        self, object_id: str, axis: str, selector: Mapping[str, Any] | None = None
    ) -> np.ndarray:
        """Return copied native coordinates without changing dtype or precision."""
        if axis not in ("time", "frequency"):
            raise ValueError(f"Unknown axis: {axis!r}")
        value = (
            self.get(object_id)
            if selector is None
            else self._select_member(object_id, selector)
        )
        coordinates = getattr(value, "times" if axis == "time" else "frequencies", None)
        if coordinates is None:
            raise ValueError(f"{type(value).__name__} has no {axis} axis")
        return np.array(getattr(coordinates, "value", coordinates), copy=True)

    def delete(self, object_id: str) -> None:
        """Delete a stored scientific object by its ID."""
        self.get(object_id)
        del self._objects[object_id]
        del self._refs[object_id]

    def list_objects(self) -> tuple[DataObjectRef, ...]:
        """List materialized object references in deterministic order."""
        return tuple(self._refs.values())

    def clear(self) -> None:
        """Clear all stored objects."""
        self._objects.clear()
        self._refs.clear()

    def __contains__(self, object_id: str) -> bool:
        """Check whether an object ID is present in the store."""
        return object_id in self._objects

    def __len__(self) -> int:
        """Return the number of stored objects."""
        return len(self._objects)
