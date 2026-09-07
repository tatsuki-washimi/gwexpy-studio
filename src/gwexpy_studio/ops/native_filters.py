"""Public GWexpy filter design, execution recipes, and response inspection."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from .native_containers import science_kind, science_members, science_rebuild
from .native_results import ScientificResult
from .values import decode_value

SCIENCE_FILTER_NAMES = ("lowpass", "highpass", "bandpass", "notch", "zpk")


def science_real_roots(values: Any, name: str) -> np.ndarray:
    """Validate finite roots paired with their complex conjugates."""
    if not isinstance(values, (list, tuple, np.ndarray)):
        raise ValueError(f"{name} must be an array of finite conjugate roots")
    roots = np.asarray(
        [
            complex(decode_value(value) if isinstance(value, Mapping) else value)
            for value in values
        ],
        dtype=complex,
    )
    if roots.ndim != 1 or not np.all(np.isfinite(roots)):
        raise ValueError(f"{name} must contain finite conjugate roots")
    unmatched = list(roots)
    while unmatched:
        root = unmatched.pop()
        if root.imag == 0:
            continue
        match = next(
            (
                index
                for index, value in enumerate(unmatched)
                if np.isclose(value, root.conjugate(), rtol=1e-12, atol=1e-14)
            ),
            None,
        )
        if match is None:
            raise ValueError(
                f"{name} must contain conjugate pairs for real coefficients"
            )
        unmatched.pop(match)
    return roots


def science_filter_recipe(
    operation: str, series: Any, params: Mapping[str, Any]
) -> dict[str, Any]:
    """Design and record the actual real digital SOS passed to native filter."""
    from gwexpy.signal import filter_design

    name = operation.removeprefix("timeseries.")
    if name not in SCIENCE_FILTER_NAMES:
        raise ValueError(f"Unsupported filter: {operation}")
    if science_kind(series) != "TimeSeries":
        raise TypeError("Filters require a TimeSeries native kind")
    sample_rate = float(series.sample_rate.to_value("Hz"))
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("Sample rate must be finite and positive")
    filtfilt = params.get("filtfilt", True)
    if type(filtfilt) is not bool:
        raise TypeError("filtfilt requires a bool")
    if name == "zpk":
        zeros = science_real_roots(params["zeros"], "zeros")
        poles = science_real_roots(params["poles"], "poles")
        gain = float(params["gain"])
        if not math.isfinite(gain):
            raise ValueError("gain must be finite and real")
        analog, normalize_gain = (
            params.get("analog", False),
            params.get("normalize_gain", False),
        )
        if type(analog) is not bool or type(normalize_gain) is not bool:
            raise TypeError("analog and normalize_gain require bool values")
        unit = params.get("unit", "rad/s")
        if unit not in ("rad/s", "Hz"):
            raise ValueError("ZPK unit must be rad/s or Hz")
        sos = getattr(filter_design, "prepare_digital_filter")(
            (zeros, poles, gain),
            analog=analog,
            sample_rate=sample_rate,
            unit=unit,
            normalize_gain=normalize_gain,
            output="sos",
        )
    else:
        names = ("flow", "fhigh") if name == "bandpass" else ("frequency",)
        frequencies = [float(params[key]) for key in names]
        if any(
            not math.isfinite(value) or not 0 < value < sample_rate / 2
            for value in frequencies
        ):
            raise ValueError("Filter frequency must be positive and below Nyquist")
        if name == "bandpass" and frequencies[0] >= frequencies[1]:
            raise ValueError("bandpass flow must be less than fhigh")
        kwargs: dict[str, Any] = {"output": "zpk"}
        if name != "notch":
            kwargs["analog"] = False
        zpk = getattr(filter_design, name)(*frequencies, sample_rate, **kwargs)
        sos = getattr(filter_design, "prepare_digital_filter")(
            zpk, sample_rate=sample_rate, output="sos"
        )
    coefficients = np.asarray(sos)
    if not np.all(np.isfinite(coefficients)) or np.iscomplexobj(coefficients):
        raise ValueError("Digital filter coefficients must be finite and real")
    return {
        "format": "sos",
        "coefficients": coefficients.tolist(),
        "sample_rate": sample_rate,
        "filtfilt": filtfilt,
        "operation": "timeseries." + name,
    }


def science_filter(
    operation: str, inputs: Mapping[str, Any], params: Mapping[str, Any]
) -> ScientificResult:
    """Stage member designs and filtered copies, retaining the exact recipes."""
    members = science_members(inputs["self"])
    prepared = [
        (key, leaf, science_filter_recipe(operation, leaf, params))
        for key, leaf in members
    ]
    outputs, recipes = [], []
    for key, leaf, recipe in prepared:
        output = leaf.filter(
            np.asarray(recipe["coefficients"]), filtfilt=recipe["filtfilt"]
        )
        outputs.append((key, output))
        recipes.append(
            dict(
                recipe, label=str(key) if key is not None else str(leaf.name or "self")
            )
        )
    return ScientificResult(
        value=science_rebuild(inputs["self"], outputs),
        details={"filter_recipes": recipes},
    )


def filter_response(
    operation_id: str,
    inputs: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    recorded_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect the executed digital response on the public integer grid.

    Forward-backward real filtering has response |H|² and zero phase.
    This describes steady-state response; finite-record edge transients are
    excluded. GWpy 4.0.2's explicit frequency-array path is deliberately unused.
    """
    if recorded_details is not None:
        science_prepare_recorded_filters(inputs, recorded_details)
        return filter_response_from_recipes(recorded_details)
    recipes = [
        dict(
            science_filter_recipe(operation_id, leaf, params),
            label=str(key) if key is not None else str(leaf.name or "self"),
        )
        for key, leaf in science_members(inputs["self"])
    ]
    return filter_response_from_recipes({"filter_recipes": recipes})


def filter_response_from_recipes(details: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect recorded digital coefficients without designing or applying them."""
    from gwexpy.signal import filter_design

    members = []
    for recipe in details.get("filter_recipes", []):
        coefficients = science_validate_filter_recipe(recipe)
        frequency, response = getattr(filter_design, "frequency_response")(
            coefficients, 4096, sample_rate=recipe["sample_rate"]
        )
        if recipe["filtfilt"]:
            response = np.asarray(abs(response) ** 2, dtype=complex)
        members.append(
            {
                "label": recipe.get("label", "self"),
                "frequency": frequency,
                "response": response,
                "frequency_unit": "Hz",
                "unit": "",
                "recipe": recipe,
            }
        )
    if not members:
        raise ValueError("Filter response requires a nonempty container")
    return {
        "members": members,
        "description": (
            "Digital steady-state response; finite-record edge transients are excluded."
        ),
    }


def science_filter_from_recipes(
    inputs: Mapping[str, Any], details: Mapping[str, Any]
) -> Any:
    """Replay recorded SOS coefficients through the public filter API."""
    prepared = science_prepare_recorded_filters(inputs, details)
    return science_rebuild(
        inputs["self"],
        [
            (key, leaf.filter(coefficients, filtfilt=filtfilt))
            for key, leaf, coefficients, filtfilt in prepared
        ],
    )


def science_validate_filter_recipe(recipe: Mapping[str, Any]) -> np.ndarray:
    """Validate the real digital coefficient and sampling contract."""
    coefficients = np.asarray(recipe["coefficients"])
    sample_rate = recipe.get("sample_rate")
    if (
        recipe.get("format") != "sos"
        or coefficients.ndim != 2
        or coefficients.shape[1] != 6
        or np.iscomplexobj(coefficients)
        or not np.all(np.isfinite(coefficients))
        or type(recipe.get("filtfilt")) is not bool
        or not isinstance(sample_rate, (int, float))
        or type(sample_rate) is bool
        or not math.isfinite(sample_rate)
        or sample_rate <= 0
    ):
        raise ValueError("Invalid real digital SOS filter recipe")
    return coefficients


def science_prepare_recorded_filters(
    inputs: Mapping[str, Any], details: Mapping[str, Any]
) -> list[tuple[Any, Any, np.ndarray, bool]]:
    """Validate all input members against recorded recipes before replay."""
    members = science_members(inputs["self"])
    recipes = details.get("filter_recipes", [])
    if len(members) != len(recipes) or not members:
        raise ValueError("Filter recipes must match the input member count")
    prepared = []
    for (key, leaf), recipe in zip(members, recipes, strict=True):
        if science_kind(leaf) != "TimeSeries":
            raise TypeError("Filter recipes require TimeSeries inputs")
        if float(leaf.sample_rate.to_value("Hz")) != recipe["sample_rate"]:
            raise ValueError("Filter recipe sample rate does not match input")
        label = str(key) if key is not None else str(leaf.name or "self")
        if recipe.get("label", label) != label:
            raise ValueError("Filter recipe labels do not match input members")
        coefficients = science_validate_filter_recipe(recipe)
        prepared.append((key, leaf, coefficients, recipe["filtfilt"]))
    return prepared
