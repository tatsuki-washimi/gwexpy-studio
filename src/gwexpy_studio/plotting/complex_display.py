"""Display-only complex transformations shared by desktop and exported plots."""

from __future__ import annotations

import re

import numpy as np
from matplotlib.axes import Axes

from ..domain.model import PlotSpec


def display_values(values: np.ndarray, unit: str, spec: PlotSpec) -> np.ndarray:
    """Copy and optionally convert an array to its declared display unit."""
    if not np.isfinite(spec.db_reference) or spec.db_reference <= 0:
        raise ValueError("dB reference must be finite and positive")
    if spec.component not in {"real", "imag", "abs", "phase"}:
        raise ValueError(f"Unknown complex component: {spec.component}")
    if spec.magnitude_scale not in {"db", "linear"}:
        raise ValueError(f"Unknown magnitude scale: {spec.magnitude_scale}")
    result = np.array(values, copy=True)
    if spec.display_unit and spec.display_unit != unit:
        from astropy.units import Unit

        result = result * Unit(unit).to(Unit(spec.display_unit))
    return result


def segmented_phase(values: np.ndarray, *, unwrap: bool) -> np.ndarray:
    """Compute degrees, unwrapping independently across finite nonzero runs."""
    valid = np.isfinite(values) & (np.abs(values) > 0)
    phase = np.full(values.shape, np.nan, dtype=float)
    if values.ndim != 1:
        for index in np.ndindex(values.shape[:-1]):
            phase[index] = segmented_phase(values[index], unwrap=unwrap)
        return phase
    starts = np.flatnonzero(valid & ~np.r_[False, valid[:-1]])
    ends = np.flatnonzero(valid & ~np.r_[valid[1:], False]) + 1
    for start, end in zip(starts, ends, strict=True):
        segment = np.angle(values[start:end])
        phase[start:end] = np.rad2deg(np.unwrap(segment) if unwrap else segment)
    return phase


def component_values(values: np.ndarray, spec: PlotSpec) -> np.ndarray:
    """Select a real display projection without changing scientific values."""
    if spec.component == "phase":
        return segmented_phase(values, unwrap=spec.phase_unwrap)
    if spec.component == "imag":
        return np.imag(values)
    if spec.component == "abs":
        return np.abs(values)
    return np.real(values)


def display_ylabel(spec: PlotSpec) -> str | None:
    """Describe the projected quantity and unit used by a line plot."""
    if spec.component == "phase":
        return "Phase [deg]"
    if spec.display_unit:
        label = spec.ylabel or "Amplitude"
        if re.search(r"\[[^\]]*\]", label):
            return re.sub(r"\[[^\]]*\]", f"[{spec.display_unit}]", label)
        return f"{label} [{spec.display_unit}]"
    return spec.ylabel


def draw_bode(
    magnitude_ax: Axes,
    phase_ax: Axes,
    frequencies: np.ndarray,
    values: np.ndarray,
    spec: PlotSpec,
    *,
    unit: str,
    label: str | None = None,
    preview_stride: int | None = None,
) -> None:
    """Draw frequency, magnitude, and phase arrays with gaps kept explicit."""
    frequencies = np.asarray(frequencies, dtype=float)
    values = display_values(values, unit, spec)
    valid = np.isfinite(frequencies) & (frequencies > 0) & np.isfinite(values)
    masked = np.where(valid & (np.abs(values) > 0), values, complex(np.nan, np.nan))
    magnitude = np.abs(masked)
    if spec.magnitude_scale == "db":
        magnitude = 20 * np.log10(magnitude / spec.db_reference)
    phase = segmented_phase(masked, unwrap=spec.phase_unwrap)
    styles = dict(spec.styles)
    if label is not None:
        styles.setdefault("label", label)
    x = np.where(valid, frequencies, np.nan)
    if preview_stride is not None and preview_stride > 1:
        # Unwrap before decimation and preserve both sides of every gap.
        visible = np.isfinite(masked)
        transitions = np.flatnonzero(visible[1:] != visible[:-1]) + 1
        indices = np.unique(
            np.concatenate(
                (np.arange(0, len(x), preview_stride), transitions, transitions - 1)
            )
        )
        x, magnitude, phase = x[indices], magnitude[indices], phase[indices]
    magnitude_ax.plot(x, magnitude, **styles)
    phase_ax.plot(x, phase, **styles)
    display_unit = spec.display_unit or unit or "dimensionless"
    ylabel = (
        f"Magnitude [dB]\nre {spec.db_reference:g} {display_unit}"
        if spec.magnitude_scale == "db"
        else f"Magnitude [{display_unit}]"
    )
    magnitude_ax.set_ylabel(ylabel)
    phase_ax.set_ylabel("Phase [deg]")
    phase_ax.set_xlabel(spec.xlabel or "Frequency [Hz]")
    if spec.title:
        magnitude_ax.set_title(spec.title.replace("; ", ";\n").replace(": ", ":\n"))
    for ax in (magnitude_ax, phase_ax):
        ax.set_xscale("log")
        ax.grid(True, which="both", linestyle="--", alpha=0.5)
        if spec.xlim is not None:
            ax.set_xlim(*spec.xlim)
    if spec.ylim is not None:
        magnitude_ax.set_ylim(*spec.ylim)
    if spec.phase_ylim is not None:
        phase_ax.set_ylim(*spec.phase_ylim)
    if spec.legend:
        magnitude_ax.legend()
