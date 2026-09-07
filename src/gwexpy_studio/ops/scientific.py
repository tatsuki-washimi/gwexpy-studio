"""Operation schemas and standalone emitters for strict scientific tools."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from types import MappingProxyType
from typing import Any

from .native_arithmetic import SCIENCE_OPERATORS, science_arithmetic
from .native_containers import SCIENCE_KINDS
from .native_filters import SCIENCE_FILTER_NAMES, science_filter
from .native_spectral import science_resample, science_spectral
from .spec import OperationSpec, ParamSpec


def _science_emit(helper: str, operation: str, context: Mapping[str, Any]) -> str:
    """Emit the shared deterministic public-native helper invocation."""
    inputs = (
        "{"
        + ", ".join(
            f"{role!r}: {variable}" for role, variable in context["inputs"].items()
        )
        + "}"
    )
    if helper == "science_filter" and context.get("details", {}).get("filter_recipes"):
        return (
            f"{context['variable']} = science_filter_from_recipes("
            f"{inputs}, {context['details']!r})"
        )
    unwrap = ".value" if helper == "science_filter" else ""
    return (
        f"{context['variable']} = {helper}("
        f"{operation!r}, {inputs}, {context['params']!r}){unwrap}"
    )


_ARITHMETIC_PARAMS = (
    ParamSpec(
        name="operand_mode", kind="enum", choices=("data", "scalar"), default="data"
    ),
    ParamSpec(name="scalar", kind="literal"),
    ParamSpec(name="reverse", kind="bool", default=False),
)

_SCIENCE_SPECS = {
    "data." + name: OperationSpec(
        operation_id="data." + name,
        schema_version=1,
        input_roles=("self",),
        optional_input_roles=("other",),
        accepted_input_kinds={"self": SCIENCE_KINDS, "other": SCIENCE_KINDS},
        result_kind="input",
        params=_ARITHMETIC_PARAMS,
        apply=partial(science_arithmetic, name),
        emit=partial(_science_emit, "science_arithmetic", name),
    )
    for name in SCIENCE_OPERATORS
}

_TS_KINDS = tuple(kind for kind in SCIENCE_KINDS if kind.startswith("TimeSeries"))
_FFT_PARAMS = (
    ParamSpec(name="fftlength", kind="quantity", canonical_unit="s"),
    ParamSpec(name="overlap", kind="quantity", canonical_unit="s"),
    ParamSpec(name="window", kind="str"),
)
_FILTER_PARAMS = (ParamSpec(name="filtfilt", kind="bool", default=True),)
_params: tuple[ParamSpec, ...]

for _name in SCIENCE_FILTER_NAMES:
    if _name == "zpk":
        _params = (
            ParamSpec(name="zeros", kind="json", required=True),
            ParamSpec(name="poles", kind="json", required=True),
            ParamSpec(name="gain", kind="float", required=True),
            ParamSpec(name="analog", kind="bool", default=False),
            ParamSpec(
                name="unit", kind="enum", choices=("rad/s", "Hz"), default="rad/s"
            ),
            ParamSpec(name="normalize_gain", kind="bool", default=False),
        )
    else:
        _frequencies = ("flow", "fhigh") if _name == "bandpass" else ("frequency",)
        _params = tuple(
            ParamSpec(name=key, kind="quantity", canonical_unit="Hz", required=True)
            for key in _frequencies
        )
    _SCIENCE_SPECS["timeseries." + _name] = OperationSpec(
        operation_id="timeseries." + _name,
        schema_version=1,
        input_roles=("self",),
        result_kind="input",
        accepted_input_kinds={"self": _TS_KINDS},
        params=_params + _FILTER_PARAMS,
        apply=partial(science_filter, _name),
        emit=partial(_science_emit, "science_filter", _name),
    )

for _name in ("psd", "csd", "coherence", "transfer_function"):
    _params = _FFT_PARAMS
    _roles = ("self",) if _name == "psd" else ("self", "other")
    if _name == "psd":
        _params += (
            ParamSpec(
                name="method",
                kind="enum",
                choices=("welch", "bartlett", "median", "median-mean"),
            ),
        )
    elif _name == "transfer_function":
        _params += (
            ParamSpec(name="mode", kind="enum", choices=("steady",), default="steady"),
            ParamSpec(
                name="average", kind="enum", choices=("mean", "median"), default="mean"
            ),
        )
    _SCIENCE_SPECS["timeseries." + _name] = OperationSpec(
        operation_id="timeseries." + _name,
        schema_version=1,
        input_roles=_roles,
        result_kind="frequency",
        accepted_input_kinds={role: _TS_KINDS for role in _roles},
        params=_params,
        apply=partial(science_spectral, _name),
        emit=partial(_science_emit, "science_spectral", _name),
    )

_SCIENCE_SPECS["timeseries.resample"] = OperationSpec(
    operation_id="timeseries.resample",
    schema_version=1,
    input_roles=("self",),
    result_kind="input",
    accepted_input_kinds={"self": _TS_KINDS},
    params=(
        ParamSpec(name="rate", kind="quantity", canonical_unit="Hz", required=True),
        ParamSpec(name="window", kind="str"),
        ParamSpec(name="ftype", kind="enum", choices=("fir", "iir"), default="fir"),
        ParamSpec(name="n", kind="int"),
    ),
    apply=partial(science_resample, "resample"),
    emit=partial(_science_emit, "science_resample", "resample"),
)
SCIENTIFIC_REGISTRY: Mapping[str, OperationSpec] = MappingProxyType(_SCIENCE_SPECS)
