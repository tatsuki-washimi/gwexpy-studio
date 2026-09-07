"""Strict public-object traversal and topology-preserving reconstruction."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import numpy as np
from astropy import units as u

SCIENCE_FAMILIES = ("TimeSeries", "FrequencySeries", "Spectrogram")
SCIENCE_KINDS = tuple(
    family + suffix
    for family in SCIENCE_FAMILIES
    for suffix in ("", "Dict", "List", "Matrix")
)


def science_class(kind: str) -> type:
    """Resolve a supported public GWexpy class."""
    family = next((name for name in SCIENCE_FAMILIES if kind.startswith(name)), None)
    if kind not in SCIENCE_KINDS or family is None:
        raise TypeError(f"Unsupported scientific kind: {kind}")
    return getattr(importlib.import_module("gwexpy." + family.lower()), kind)


def science_kind(value: Any) -> str:
    """Return a supported native object's exact family and topology."""
    kind = type(value).__name__
    if kind not in SCIENCE_KINDS or not isinstance(value, science_class(kind)):
        raise TypeError(f"Unsupported scientific kind: {kind}")
    return kind


def science_copy(value: Any) -> Any:
    """Detach a native leaf without re-inferring cadence from its coordinates."""
    if science_kind(value) not in SCIENCE_FAMILIES:
        raise TypeError("Scientific copying requires a native leaf")
    # GWexpy 0.2 TimeSeries.copy reconstructs from times and can change dt by
    # one ULP. NumPy's public copy retains native metadata through finalize.
    result = np.ndarray.copy(value)
    if hasattr(value, "attrs"):
        result.attrs = deepcopy(value.attrs)
    return result


def science_preserve_axis_metadata(source: Any, target: Any) -> None:
    """Restore exact scalar metadata after copying full native coordinates."""
    for attribute in ("t0", "dt", "f0", "df"):
        original = getattr(source, attribute, None)
        copied = getattr(target, attribute, None)
        if original is not None and copied is not None:
            # Quantity item assignment updates the detached scalar metadata;
            # an axis setter would regenerate or discard explicit coordinates.
            copied[...] = original


def science_members(value: Any) -> list[tuple[Any, Any]]:
    """Extract independent leaves using public constructors and metadata."""
    kind = science_kind(value)
    if kind.endswith("Dict"):
        return [(key, science_copy(leaf)) for key, leaf in value.items()]
    if kind.endswith("List"):
        return [(index, science_copy(leaf)) for index, leaf in enumerate(value)]
    if kind.endswith("Matrix"):
        family = kind.removesuffix("Matrix")
        shape = value.shape[:-2] if family == "Spectrogram" else value.shape[:-1]
        members = []
        for index in np.ndindex(shape):
            cell_index = index if len(index) == 2 else (index[0], 0)
            meta = value.meta[cell_index]
            kwargs = {"unit": meta.unit, "name": meta.name, "channel": meta.channel}
            if family != "FrequencySeries":
                kwargs["times"] = value.times.copy()
            if family != "TimeSeries":
                kwargs["frequencies"] = value.frequencies.copy()
            if family == "FrequencySeries":
                kwargs["epoch"] = deepcopy(value.epoch)
            leaf = science_class(family)(value.value[index].copy(), **kwargs)
            science_preserve_axis_metadata(value, leaf)
            members.append((index, leaf))
        return members
    return [(None, science_copy(value))]


def science_rebuild(template: Any, members: list[tuple[Any, Any]]) -> Any:
    """Rebuild the same topology without unit conversion or axis intersection."""
    kind = science_kind(template)
    if not members:
        raise ValueError("Scientific operations require a nonempty container")
    family = science_kind(members[0][1])
    if kind.endswith("Dict"):
        return science_class(family + "Dict")(dict(members))
    if kind.endswith("List"):
        return science_class(family + "List")([leaf for _, leaf in members])
    if not kind.endswith("Matrix"):
        return members[0][1]
    topology = (
        template.shape[:-2] if kind.startswith("Spectrogram") else template.shape[:-1]
    )
    leaf = members[0][1]
    values = np.stack([item.value for _, item in members]).reshape(
        topology + leaf.shape
    )
    metadata_shape = topology if len(topology) == 2 else (topology[0], 1)
    kwargs = {
        "units": np.array([item.unit for _, item in members], dtype=object).reshape(
            metadata_shape
        ),
        "names": np.array([item.name for _, item in members], dtype=object).reshape(
            metadata_shape
        ),
        "channels": np.array(
            [item.channel for _, item in members], dtype=object
        ).reshape(metadata_shape),
        "rows": deepcopy(template.rows),
        "cols": deepcopy(template.cols),
        "name": deepcopy(template.name),
        "epoch": deepcopy(template.epoch),
        "attrs": deepcopy(getattr(template, "attrs", {})),
    }
    if family != "FrequencySeries":
        kwargs["times"] = leaf.times.copy()
    if family != "TimeSeries":
        kwargs["frequencies"] = leaf.frequencies.copy()
    if family == "Spectrogram":
        from gwexpy.types.metadata import MetaDataMatrix

        meta = np.empty(metadata_shape, dtype=object)
        for index, item in members:
            cell_index = index if len(index) == 2 else (index[0], 0)
            meta[cell_index] = {
                "unit": item.unit,
                "name": item.name,
                "channel": item.channel,
            }
        kwargs["meta"] = MetaDataMatrix(meta)
    result = science_class(family + "Matrix")(values, **kwargs)
    science_preserve_axis_metadata(leaf, result)
    result.attrs = deepcopy(getattr(template, "attrs", {}))
    return result


def science_axis_match(left: Any, right: Any) -> None:
    """Reject every kind, shape, origin, cadence, and coordinate mismatch."""
    if science_kind(left) != science_kind(right) or left.shape != right.shape:
        raise ValueError("Operands must have the same native kind and shape")
    for attribute, unit in (
        ("times", "s"),
        ("t0", "s"),
        ("dt", "s"),
        ("frequencies", "Hz"),
        ("f0", "Hz"),
        ("df", "Hz"),
    ):
        a, b = getattr(left, attribute, None), getattr(right, attribute, None)
        if a is None and b is None:
            continue
        if a is None or b is None:
            raise ValueError(f"Operands must have matching {attribute} axis metadata")
        av, bv = u.Quantity(a).to_value(unit), u.Quantity(b).to_value(unit)
        if not np.array_equal(av, bv):
            raise ValueError(f"Operands must have matching {attribute} coordinates")


def science_pairs(left: Any, right: Any) -> list[tuple[Any, Any, Any]]:
    """Preflight the entire batch before returning ordered independent pairs."""
    kind = science_kind(left)
    if kind != science_kind(right):
        raise ValueError("Operands must have the same native kind and topology")
    if kind.endswith("Dict"):
        if set(left) != set(right):
            raise ValueError("Dictionary operands must have the same key set")
        for key in left:
            science_axis_match(left[key], right[key])
    elif kind.endswith("List"):
        if len(left) != len(right):
            raise ValueError("Container operands must have the same length")
        for first, second in zip(left, right, strict=True):
            science_axis_match(first, second)
    elif kind.endswith("Matrix"):
        science_axis_match(left, right)
        if (
            left.shape != right.shape
            or left.row_keys() != right.row_keys()
            or left.col_keys() != right.col_keys()
        ):
            raise ValueError(
                "Matrix operands must have matching shape and row/column labels"
            )
    else:
        science_axis_match(left, right)
    a, b = science_members(left), science_members(right)
    if kind.endswith("Dict"):
        b_by_key = dict(b)
        b = [(key, b_by_key[key]) for key, _ in a]
    return [
        (key, first, second) for (key, first), (_, second) in zip(a, b, strict=True)
    ]


def science_map(value: Any, function: Callable[[Any], Any]) -> Any:
    """Stage all unary results before returning a reconstructed native object."""
    return science_rebuild(
        value, [(key, function(leaf)) for key, leaf in science_members(value)]
    )
