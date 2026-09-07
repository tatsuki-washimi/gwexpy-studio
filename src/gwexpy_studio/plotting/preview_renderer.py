"""Matplotlib preview renderer for complete PreviewData and PlotSpec pairs."""

from __future__ import annotations

from typing import Any

import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from ..application.preview import PreviewData
from ..domain.model import PlotSpec
from .complex_display import component_values, display_values, display_ylabel, draw_bode

_LINE_PREVIEW_KINDS = frozenset({"TimeSeries", "FrequencySeries"})
_SUPPORTED_PREVIEW_KINDS = _LINE_PREVIEW_KINDS | {"Spectrogram"}


def validate_preview_spec(preview: object, spec: object) -> None:
    """Reject invalid preview/spec structure before any rendering mutation."""
    if not isinstance(preview, PreviewData):
        raise TypeError(f"Expected PreviewData, got {type(preview)}")
    if not isinstance(spec, PlotSpec):
        raise TypeError(f"Expected PlotSpec, got {type(spec)}")

    preview_kind = preview.ref.kind
    if preview_kind not in _SUPPORTED_PREVIEW_KINDS:
        raise ValueError(f"Unsupported preview kind for renderer: {preview_kind!r}")

    if not spec.object_ids:
        raise ValueError("Preview PlotSpec.object_ids must contain one object ID")
    if len(spec.object_ids) != 1:
        raise ValueError(
            "Preview PlotSpec.object_ids must contain exactly one object ID"
        )
    if spec.object_ids[0] != preview.ref.object_id:
        raise ValueError(
            "Preview PlotSpec object ID does not match PreviewData target: "
            f"{spec.object_ids[0]!r} != {preview.ref.object_id!r}"
        )

    allowed = {"line", "bode"} if preview_kind == "FrequencySeries" else {"line"}
    if preview_kind in _LINE_PREVIEW_KINDS and spec.kind not in allowed:
        raise ValueError(f"Preview kind {preview_kind!r} requires PlotSpec kind 'line'")
    if preview_kind == "Spectrogram" and spec.kind != "spectrogram":
        raise ValueError(
            "Preview kind 'Spectrogram' requires PlotSpec kind 'spectrogram'"
        )


def _line_coordinates(preview: PreviewData) -> np.ndarray:
    if preview.x_coordinates is not None:
        return preview.x_coordinates
    axes = preview.ref.axes
    if preview.ref.kind == "TimeSeries":
        start = float(axes["t0"]["value"])
        step = float(axes["dt"]["value"])
    else:
        start = float(axes["f0"]["value"])
        step = float(axes["df"]["value"])
    return start + np.arange(len(preview.values), dtype=np.float64) * step


def _apply_spec(ax: Axes, spec: PlotSpec) -> None:
    """Apply all axes-level PlotSpec fields after the plot artist exists."""
    ax.set_xscale(spec.xscale)
    ax.set_yscale(spec.yscale)
    if spec.xlim is not None:
        ax.set_xlim(*spec.xlim)
    if spec.ylim is not None:
        ax.set_ylim(*spec.ylim)
    if spec.title is not None:
        ax.set_title(spec.title)
    if spec.xlabel is not None:
        ax.set_xlabel(spec.xlabel)
    if spec.ylabel is not None:
        ax.set_ylabel(spec.ylabel)
    if spec.legend:
        ax.legend()


def render_preview(
    preview: PreviewData,
    spec: PlotSpec,
    *,
    fig: Figure | None = None,
) -> Figure:
    """Render one structurally coherent preview/spec pair onto a Figure."""
    validate_preview_spec(preview, spec)

    # Validate conversion before clearing an existing canvas.
    values = display_values(preview.values, preview.unit, spec)
    if fig is None:
        fig = Figure(figsize=(8, 5))
        FigureCanvasAgg(fig)
    else:
        # Disconnect logarithmic shared-axis limits before Matplotlib clears them.
        for previous_ax in fig.axes:
            previous_ax.set_xscale("linear")
        fig.clear()

    if spec.kind == "bode":
        magnitude, phase = fig.subplots(2, 1, sharex=True)
        draw_bode(
            magnitude,
            phase,
            _line_coordinates(preview),
            preview.values,
            spec,
            unit=preview.unit,
        )
        fig.tight_layout()
        return fig

    ax = fig.subplots()
    values = component_values(values, spec)

    if spec.kind == "line":
        plot_styles: dict[str, Any] = dict(spec.styles)
        coordinates = _line_coordinates(preview)
        ax.plot(coordinates, values, **plot_styles)
        ax.grid(True, which="both", linestyle="--", alpha=0.5)
    else:
        axes = preview.ref.axes
        nt, nf = preview.values.shape
        times = preview.x_coordinates
        frequencies = preview.y_coordinates
        if times is None:
            times = float(axes["t0"]["value"]) + np.arange(nt) * float(
                axes["dt"]["value"]
            )
        if frequencies is None:
            frequencies = float(axes["f0"]["value"]) + np.arange(nf) * float(
                axes["df"]["value"]
            )
        mesh_styles: dict[str, Any] = {"shading": "auto", **dict(spec.styles)}
        mesh = ax.pcolormesh(
            times,
            frequencies,
            values.T,
            **mesh_styles,
        )
        colorbar = fig.colorbar(mesh, ax=ax)
        if preview.unit or spec.display_unit:
            colorbar.set_label(
                "deg"
                if spec.component == "phase"
                else (spec.display_unit or preview.unit)
            )

    _apply_spec(ax, spec)
    ylabel = display_ylabel(spec)
    if spec.kind == "line" and ylabel is not None:
        ax.set_ylabel(ylabel)
    fig.tight_layout()
    return fig
