"""Shared pytest configuration and deterministic GWexpy fixtures."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tests.support.fixtures import (  # noqa: E402
    CHANNEL_NAME,
    linear_trend_values,
    make_timeseries,
    random_values,
    sine_32hz_values,
    write_named_hdf5,
)


@pytest.fixture(autouse=True)
def isolated_user_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every Studio-owned XDG write out of the real user profile."""
    homes = {
        "XDG_CONFIG_HOME": tmp_path / "user-config",
        "XDG_DATA_HOME": tmp_path / "user-data",
        "XDG_STATE_HOME": tmp_path / "user-state",
        "XDG_CACHE_HOME": tmp_path / "user-cache",
    }
    for name, directory in homes.items():
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv(
        "MPLCONFIGDIR", str(homes["XDG_CACHE_HOME"] / "gwexpy-studio" / "matplotlib")
    )


@pytest.fixture(scope="session", autouse=True)
def environment_guard() -> None:
    """Validate that runtime environment meets tested dependency bounds."""
    import astropy
    import gwexpy
    import gwpy
    import matplotlib
    import numpy
    import scipy

    guards = [
        (
            "gwexpy",
            Version(str(gwexpy.__version__)),
            Version("0.2.0"),
            Version("0.3.0"),
        ),
        (
            "gwpy",
            Version(str(gwpy.__version__)),
            Version("4.0.0"),
            Version("5.0.0"),
        ),
        (
            "numpy",
            Version(str(numpy.__version__)),
            Version("2.0.0"),
            Version("3.0.0"),
        ),
        (
            "scipy",
            Version(str(scipy.__version__)),
            Version("1.15.0"),
            Version("2.0.0"),
        ),
        (
            "astropy",
            Version(str(astropy.__version__)),
            Version("7.0.0"),
            Version("9.0.0"),
        ),
        (
            "matplotlib",
            Version(str(matplotlib.__version__)),
            Version("3.10.0"),
            Version("4.0.0"),
        ),
    ]
    for pkg_name, current_v, min_v, max_v in guards:
        if not (min_v <= current_v < max_v):
            msg = (
                f"ENV-GUARD: {pkg_name} version {current_v} is out of "
                f"verified range [{min_v}, {max_v})"
            )
            pytest.fail(msg, pytrace=False)

    try:
        import io

        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        _fig = Figure()
        FigureCanvasAgg(_fig)
        _ax = _fig.subplots()
        _ax.plot([1.0, 10.0], [1.0, 10.0], label="w", color="black")
        _ax.set_xscale("log")
        _ax.set_yscale("log")
        _ax.set_title("warmup with $\\sqrt{f}$")
        _ax.set_xlabel("time [s]")
        _ax.set_ylabel("amplitude [m / Hz(1/2)]")
        _ax.legend()
        _buf = io.BytesIO()
        _fig.savefig(_buf, format="png")
        _fig.clear()
        del _fig, _ax, _buf
    except Exception:
        pass


@pytest.fixture(scope="session")
def gwexpy_version_guard(environment_guard: None) -> Any:
    """Import gwexpy lazily and require the approved minimum version."""
    del environment_guard
    import gwexpy

    return gwexpy


@pytest.fixture(scope="session")
def gwexpy_module(gwexpy_version_guard: Any) -> Any:
    """Return the guarded gwexpy module for fixtures that need it."""
    return gwexpy_version_guard


@pytest.fixture
def make_test_timeseries(
    gwexpy_module: Any,
) -> Callable[[str], Any]:
    """Return a factory for deterministic named TimeSeries variants."""
    del gwexpy_module

    def factory(kind: str = "random") -> Any:
        values_by_kind = {
            "random": random_values,
            "linear": linear_trend_values,
            "sine_32hz": sine_32hz_values,
        }
        try:
            values = values_by_kind[kind]()
        except KeyError as exc:
            raise ValueError(f"unknown fixture kind: {kind}") from exc
        return make_timeseries(values)

    return factory


@pytest.fixture
def timeseries(make_test_timeseries: Callable[[str], Any]) -> Any:
    """Return the deterministic PCG64(42) TimeSeries fixture."""
    return make_test_timeseries("random")


@pytest.fixture
def random_timeseries(timeseries: Any) -> Any:
    """Alias for the deterministic random TimeSeries fixture."""
    return timeseries


@pytest.fixture
def linear_trend(make_test_timeseries: Callable[[str], Any]) -> Any:
    """Return the deterministic linear-trend TimeSeries fixture."""
    return make_test_timeseries("linear")


@pytest.fixture
def sine_32hz(make_test_timeseries: Callable[[str], Any]) -> Any:
    """Return the deterministic 32 Hz sine TimeSeries fixture."""
    return make_test_timeseries("sine_32hz")


@pytest.fixture
def hdf5_source(tmp_path: Path, timeseries: Any) -> Path:
    """Return a named HDF5 source written with the explicit v1 format."""
    path = tmp_path / f"{CHANNEL_NAME.replace(':', '-')}.h5"
    return write_named_hdf5(timeseries, path)


@pytest.fixture
def source_file(hdf5_source: Path) -> Path:
    """Compatibility alias for the named HDF5 source fixture."""
    return hdf5_source
