"""Central operation registry shared by execution and standalone export."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from types import MappingProxyType

from .io_operations import IO_REGISTRY
from .native_containers import SCIENCE_KINDS
from .native_selection import native_legacy
from .scientific import SCIENTIFIC_REGISTRY, _science_emit
from .spec import OperationSpec
from .timeseries import REGISTRY as LEGACY_REGISTRY

_CENTRAL_SPECS = {**LEGACY_REGISTRY, **SCIENTIFIC_REGISTRY, **IO_REGISTRY}
_TS_INPUT_KINDS = tuple(kind for kind in SCIENCE_KINDS if kind.startswith("TimeSeries"))
for _name, _result in (
    ("crop", "input"),
    ("detrend", "input"),
    ("asd", "frequency"),
    ("spectrogram", "spectrogram"),
):
    _CENTRAL_SPECS["timeseries." + _name] = replace(
        LEGACY_REGISTRY["timeseries." + _name],
        accepted_input_kinds={"self": _TS_INPUT_KINDS},
        result_kind=_result,
        apply=partial(native_legacy, _name),
        emit=partial(_science_emit, "native_legacy", _name),
    )

REGISTRY: Mapping[str, OperationSpec] = MappingProxyType(_CENTRAL_SPECS)
