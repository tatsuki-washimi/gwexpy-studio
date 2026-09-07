"""Deterministic numeric sources used by headless contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

DURATION_SECONDS = 60.0
SAMPLE_RATE_HZ = 256.0
SAMPLE_COUNT = int(DURATION_SECONDS * SAMPLE_RATE_HZ)
T0_GPS = 1_000_000_000.0
UNIT_NAME = "m"
TEST_NAME = "X1:STUDIO-TEST"
CHANNEL_NAME = "X1:STUDIO-CHANNEL"


def random_values() -> np.ndarray:
    """Return fresh PCG64(42) standard-normal samples."""
    generator = np.random.Generator(np.random.PCG64(42))
    return generator.standard_normal(SAMPLE_COUNT).astype(np.float64, copy=False)


def linear_trend_values() -> np.ndarray:
    """Return a deterministic unit-amplitude linear trend."""
    return np.linspace(0.0, 1.0, SAMPLE_COUNT, dtype=np.float64)


def sine_32hz_values() -> np.ndarray:
    """Return a zero-phase 32 Hz sine wave sampled at 256 Hz."""
    times = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE_HZ
    return np.sin(2.0 * np.pi * 32.0 * times)


def make_timeseries(values: np.ndarray) -> Any:
    """Build a named gwexpy TimeSeries with imports local to this factory."""
    from astropy import units as u
    from gwexpy.timeseries import TimeSeries

    return TimeSeries(
        values,
        sample_rate=SAMPLE_RATE_HZ * u.Hz,
        unit=UNIT_NAME,
        t0=T0_GPS,
        name=TEST_NAME,
        channel=CHANNEL_NAME,
    )


def write_named_hdf5(series: Any, path: Path) -> Path:
    """Write one named HDF5 source using the explicit approved format."""
    series.write(path, format="hdf5")
    return path
