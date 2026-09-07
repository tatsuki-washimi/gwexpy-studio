"""Public native spectral and resampling operations on independent leaves."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .native_containers import (
    science_axis_match,
    science_kind,
    science_map,
    science_members,
    science_pairs,
    science_rebuild,
)


def science_fft_kwargs(series: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate seconds-based segment parameters without changing defaults."""
    if science_kind(series) != "TimeSeries":
        raise TypeError("Spectral operations require a TimeSeries native kind")
    fftlength, overlap = params.get("fftlength"), params.get("overlap")
    duration = float(series.duration.to_value("s"))
    effective_length = duration if fftlength is None else float(fftlength)
    if not math.isfinite(effective_length) or not 0 < effective_length <= duration:
        raise ValueError("fftlength must be positive and no longer than the input")
    if overlap is not None and (
        not math.isfinite(float(overlap)) or not 0 <= float(overlap) < effective_length
    ):
        raise ValueError("overlap must be nonnegative and less than fftlength")
    return {key: value for key, value in params.items() if value is not None}


def science_spectral(
    operation: str, inputs: Mapping[str, Any], params: Mapping[str, Any]
) -> Any:
    """Run PSD or paired CSD, coherence, and steady B/A transfer functions."""
    if operation not in ("psd", "csd", "coherence", "transfer_function"):
        raise ValueError(f"Unsupported spectral operation: {operation}")
    if operation == "psd":
        members = science_members(inputs["self"])
        prepared = [
            (key, leaf, science_fft_kwargs(leaf, params)) for key, leaf in members
        ]
        return science_rebuild(
            inputs["self"],
            [(key, leaf.psd(**kwargs)) for key, leaf, kwargs in prepared],
        )
    if operation == "transfer_function" and params.get("mode", "steady") != "steady":
        raise ValueError("Only steady transfer_function mode is supported")
    pairs = science_pairs(inputs["self"], inputs["other"])
    prepared_pairs = [
        (key, left, right, science_fft_kwargs(left, params))
        for key, left, right in pairs
    ]
    outputs = []
    for key, left, right, kwargs in prepared_pairs:
        # Check native axes immediately before calling APIs that may auto-align.
        science_axis_match(left, right)
        if operation == "transfer_function":
            kwargs["mode"] = "steady"
        outputs.append((key, getattr(left, operation)(right, **kwargs)))
    return science_rebuild(inputs["self"], outputs)


def science_resample(
    operation: str, inputs: Mapping[str, Any], params: Mapping[str, Any]
) -> Any:
    """Explicitly resample every copied leaf through TimeSeries.resample."""
    del operation
    rate = float(params["rate"])
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("Resample rate must be finite and positive")
    kwargs = {
        key: value
        for key, value in params.items()
        if key != "rate" and value is not None
    }

    def resample_leaf(leaf: Any) -> Any:
        if science_kind(leaf) != "TimeSeries":
            raise TypeError("Resampling requires a TimeSeries native kind")
        return leaf.resample(rate, **kwargs)

    return science_map(inputs["self"], resample_leaf)
