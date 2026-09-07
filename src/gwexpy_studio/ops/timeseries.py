"""Curated TimeSeries operation implementations and registry."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .spec import OperationSpec, ParamSpec, normalize_params

SUPPORTED_FORMATS = ("hdf5", "csv_enhanced")
_PUBLIC_FORMATS = {"csv_enhanced": "csv"}


def _read_apply(
    resolved_inputs: Mapping[str, Any], call_kwargs: Mapping[str, Any]
) -> Any:
    """Read a TimeSeries from a supported data source."""
    del resolved_inputs
    import gwexpy

    gwexpy.register_all(include_io=True)
    from gwexpy.timeseries import TimeSeries

    source = call_kwargs["source"]
    fmt = _PUBLIC_FORMATS.get(call_kwargs["format"], call_kwargs["format"])
    name = call_kwargs.get("name")
    if name is not None:
        return TimeSeries.read(source, format=fmt, name=name)
    return TimeSeries.read(source, format=fmt)


def _read_emit(context: Mapping[str, Any]) -> str:
    """Emit a standalone TimeSeries.read call."""
    target = context["variable"]
    params = context["params"]
    source = params["source"]
    fmt = _PUBLIC_FORMATS.get(params["format"], params["format"])
    name = params.get("name")
    if name is not None:
        return (
            f"{target} = TimeSeries.read("
            f"{repr(source)}, format={repr(fmt)}, name={repr(name)})"
        )
    return f"{target} = TimeSeries.read({repr(source)}, format={repr(fmt)})"


def _crop_apply(
    resolved_inputs: Mapping[str, Any], call_kwargs: Mapping[str, Any]
) -> Any:
    """Crop a TimeSeries with strict span boundary validation."""
    series = resolved_inputs["self"]
    start = call_kwargs.get("start")
    end = call_kwargs.get("end")

    # Validate bounds against the TimeSeries interval [t0, t0 + duration].
    t0 = float(series.t0.to_value("s"))
    duration = float(series.duration.to_value("s"))
    t_end = t0 + duration

    if start is not None and (start < t0 or start > t_end):
        raise ValueError(f"Crop start {start} is outside series span [{t0}, {t_end}]")
    if end is not None and (end < t0 or end > t_end):
        raise ValueError(f"Crop end {end} is outside series span [{t0}, {t_end}]")
    if start is not None and end is not None and start >= end:
        raise ValueError(f"Crop start ({start}) must be strictly less than end ({end})")

    return series.crop(start, end)


def _crop_emit(context: Mapping[str, Any]) -> str:
    """Emit a TimeSeries.crop call."""
    target = context["variable"]
    receiver = context["inputs"]["self"]
    spec = REGISTRY["timeseries.crop"]
    params = normalize_params(context["params"], spec=spec)
    start = params.get("start")
    end = params.get("end")
    if start is not None and end is not None:
        return f"{target} = {receiver}.crop({repr(float(start))}, {repr(float(end))})"
    if start is not None:
        return f"{target} = {receiver}.crop(start={repr(float(start))})"
    if end is not None:
        return f"{target} = {receiver}.crop(end={repr(float(end))})"
    return f"{target} = {receiver}.crop()"


def _detrend_apply(
    resolved_inputs: Mapping[str, Any], call_kwargs: Mapping[str, Any]
) -> Any:
    """Detrend a TimeSeries."""
    series = resolved_inputs["self"]
    detrend_type = call_kwargs.get("detrend", "constant")
    return series.detrend(detrend_type)


def _detrend_emit(context: Mapping[str, Any]) -> str:
    """Emit a TimeSeries.detrend call."""
    target = context["variable"]
    receiver = context["inputs"]["self"]
    params = context["params"]
    detrend_type = params.get("detrend", "constant")
    return f"{target} = {receiver}.detrend({repr(detrend_type)})"


def _asd_apply(
    resolved_inputs: Mapping[str, Any], call_kwargs: Mapping[str, Any]
) -> Any:
    """Compute Amplitude Spectral Density (ASD) of a TimeSeries."""
    series = resolved_inputs["self"]
    kwargs: dict[str, Any] = {"fftlength": call_kwargs["fftlength"]}
    for opt in ("overlap", "window", "method"):
        if call_kwargs.get(opt) is not None:
            kwargs[opt] = call_kwargs[opt]
    return series.asd(**kwargs)


def _asd_emit(context: Mapping[str, Any]) -> str:
    """Emit a TimeSeries.asd call."""
    target = context["variable"]
    receiver = context["inputs"]["self"]
    spec = REGISTRY["timeseries.asd"]
    params = normalize_params(context["params"], spec=spec)
    kwargs_parts = [f"fftlength={repr(float(params['fftlength']))}"]
    for opt in ("overlap", "window", "method"):
        if params.get(opt) is not None:
            kwargs_parts.append(f"{opt}={repr(params[opt])}")
    return f"{target} = {receiver}.asd({', '.join(kwargs_parts)})"


def _spectrogram_apply(
    resolved_inputs: Mapping[str, Any], call_kwargs: Mapping[str, Any]
) -> Any:
    """Compute Spectrogram of a TimeSeries."""
    series = resolved_inputs["self"]
    stride = call_kwargs["stride"]
    kwargs: dict[str, Any] = {}
    for opt in ("fftlength", "overlap", "window", "method"):
        if call_kwargs.get(opt) is not None:
            kwargs[opt] = call_kwargs[opt]
    return series.spectrogram(stride, **kwargs)


def _spectrogram_emit(context: Mapping[str, Any]) -> str:
    """Emit a TimeSeries.spectrogram call."""
    target = context["variable"]
    receiver = context["inputs"]["self"]
    spec = REGISTRY["timeseries.spectrogram"]
    params = normalize_params(context["params"], spec=spec)
    args_parts = [repr(float(params["stride"]))]
    for opt in ("fftlength", "overlap", "window", "method"):
        if params.get(opt) is not None:
            args_parts.append(f"{opt}={repr(params[opt])}")
    return f"{target} = {receiver}.spectrogram({', '.join(args_parts)})"


REGISTRY: Mapping[str, OperationSpec] = MappingProxyType(
    {
        "timeseries.read": OperationSpec(
            operation_id="timeseries.read",
            schema_version=1,
            input_roles=(),
            result_kind="TimeSeries",
            params=(
                ParamSpec(name="source", kind="source", required=True),
                ParamSpec(
                    name="format",
                    kind="enum",
                    required=True,
                    choices=SUPPORTED_FORMATS,
                ),
                ParamSpec(name="name", kind="str"),
            ),
            apply=_read_apply,
            emit=_read_emit,
        ),
        "timeseries.crop": OperationSpec(
            operation_id="timeseries.crop",
            schema_version=1,
            input_roles=("self",),
            result_kind="TimeSeries",
            params=(
                ParamSpec(name="start", kind="gps"),
                ParamSpec(name="end", kind="gps"),
            ),
            apply=_crop_apply,
            emit=_crop_emit,
        ),
        "timeseries.detrend": OperationSpec(
            operation_id="timeseries.detrend",
            schema_version=1,
            input_roles=("self",),
            result_kind="TimeSeries",
            params=(
                ParamSpec(
                    name="detrend",
                    kind="enum",
                    default="constant",
                    choices=("constant", "linear"),
                ),
            ),
            apply=_detrend_apply,
            emit=_detrend_emit,
        ),
        "timeseries.asd": OperationSpec(
            operation_id="timeseries.asd",
            schema_version=1,
            input_roles=("self",),
            result_kind="FrequencySeries",
            params=(
                ParamSpec(
                    name="fftlength",
                    kind="quantity",
                    canonical_unit="s",
                    required=True,
                ),
                ParamSpec(name="overlap", kind="quantity", canonical_unit="s"),
                ParamSpec(name="window", kind="str"),
                ParamSpec(
                    name="method",
                    kind="enum",
                    choices=("welch", "bartlett", "median", "median-mean"),
                ),
            ),
            apply=_asd_apply,
            emit=_asd_emit,
        ),
        "timeseries.spectrogram": OperationSpec(
            operation_id="timeseries.spectrogram",
            schema_version=1,
            input_roles=("self",),
            result_kind="Spectrogram",
            params=(
                ParamSpec(
                    name="stride",
                    kind="quantity",
                    canonical_unit="s",
                    required=True,
                ),
                ParamSpec(name="fftlength", kind="quantity", canonical_unit="s"),
                ParamSpec(name="overlap", kind="quantity", canonical_unit="s"),
                ParamSpec(name="window", kind="str"),
                ParamSpec(
                    name="method",
                    kind="enum",
                    choices=("welch", "bartlett", "median", "median-mean"),
                ),
            ),
            apply=_spectrogram_apply,
            emit=_spectrogram_emit,
        ),
    }
)
