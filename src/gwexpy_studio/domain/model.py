"""Dataclass records for data references, operations, execution, and plots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

SourceFormat = str
ObjectKind = Literal[
    "TimeSeries",
    "TimeSeriesDict",
    "TimeSeriesList",
    "TimeSeriesMatrix",
    "FrequencySeries",
    "FrequencySeriesDict",
    "FrequencySeriesList",
    "FrequencySeriesMatrix",
    "Spectrogram",
    "SpectrogramDict",
    "SpectrogramList",
    "SpectrogramMatrix",
]
PlotKind = Literal["line", "spectrogram", "bode"]


@dataclass(frozen=True, kw_only=True, slots=True)
class MemberRef:
    """Metadata for a selectable native member, without materializing an object."""

    selector: Mapping[str, str | int]
    label: str
    kind: ObjectKind
    shape: tuple[int, ...]
    dtype: str | None
    unit: str | None
    name: str | None = None
    channel: str | None = None
    axes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True, slots=True)
class DataSourceRef:
    """Reference to an external data source."""

    source_id: str
    uri: str
    format: SourceFormat
    size_bytes: int | None = None
    mtime: float | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class DataObjectRef:
    """Metadata snapshot for an object held by a scientific worker."""

    object_id: str
    kind: ObjectKind
    shape: tuple[int, ...]
    dtype: str | None
    unit: str | None
    name: str | None = None
    channel: str | None = None
    axes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    produced_by: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    members: tuple[MemberRef, ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class Operation:
    """Versioned semantic operation in the append-only graph."""

    op_id: str
    operation_id: str
    operation_schema: int
    inputs: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    outputs: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True, slots=True)
class ExecutionRecord:
    """Append-only record of one operation execution attempt."""

    execution_id: str
    op_id: str
    started_at: str
    duration_s: float
    status: str
    warnings: tuple[str, ...] = ()
    error: Mapping[str, Any] | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True, slots=True)
class ActivityRecord:
    """File-write or other external effect, separate from scientific operations."""

    activity_id: str
    action: str
    started_at: str
    status: str
    object_id: str | None = None
    target: str | None = None
    duration_s: float = 0.0
    details: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class PlotSpec:
    """Renderer-independent visual declaration."""

    plot_id: str
    kind: PlotKind
    object_ids: tuple[str, ...]
    xscale: str = "linear"
    yscale: str = "linear"
    xlim: tuple[float, float] | None = None
    ylim: tuple[float, float] | None = None
    phase_ylim: tuple[float, float] | None = None
    title: str | None = None
    xlabel: str | None = None
    ylabel: str | None = None
    legend: bool = False
    styles: Mapping[str, Any] = field(default_factory=dict)
    component: Literal["real", "imag", "abs", "phase"] = "real"
    magnitude_scale: Literal["db", "linear"] = "db"
    db_reference: float = 1.0
    display_unit: str | None = None
    phase_unwrap: bool = True
    selectors: Mapping[str, Mapping[str, str | int]] = field(default_factory=dict)
