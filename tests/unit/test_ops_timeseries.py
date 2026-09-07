"""Contract tests for curated TimeSeries operations and public-api oracles."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy import units as u

from gwexpy_studio.ops.timeseries import REGISTRY
from tests.support.fixtures import CHANNEL_NAME, SAMPLE_RATE_HZ, T0_GPS, TEST_NAME
from tests.support.g0_consistency import assert_crop_value_error_worker_and_export


def _axis_value(value: Any, unit: str) -> float:
    """Return one finite axis scalar in the requested unit."""
    quantity = value if hasattr(value, "unit") else value * u.Unit(unit)
    return float(quantity.to_value(unit))


def _assert_reference_values(actual: Any, reference: Any) -> None:
    """Compare a Studio result with a separately computed public-api result."""
    actual_values = np.asarray(actual.value)
    reference_values = np.asarray(reference.value)

    # Finiteness is checked before any unit conversion or tolerance arithmetic.
    assert np.isfinite(actual_values).all()
    assert np.isfinite(reference_values).all()
    assert actual_values.shape == reference_values.shape

    actual_unit = u.Unit(actual.unit)
    reference_unit = u.Unit(reference.unit)
    assert actual_unit == reference_unit
    actual_in_reference_unit = (actual_values * actual_unit).to_value(reference_unit)
    tolerance = 1e-12 * max(1.0, float(np.max(np.abs(reference_values))))
    np.testing.assert_allclose(
        actual_in_reference_unit,
        reference_values,
        rtol=1e-12,
        atol=tolerance,
    )


def _assert_time_axes(actual: Any, reference: Any) -> None:
    """Check time origin, spacing, and monotonic sample coordinates."""
    assert _axis_value(actual.t0, "s") == pytest.approx(
        _axis_value(reference.t0, "s"), rel=1e-12, abs=1e-12
    )
    assert _axis_value(actual.dt, "s") == pytest.approx(
        _axis_value(reference.dt, "s"), rel=1e-12, abs=1e-12
    )
    actual_times = np.asarray(actual.times.to_value("s"))
    reference_times = np.asarray(reference.times.to_value("s"))
    assert np.isfinite(actual_times).all()
    assert np.isfinite(reference_times).all()
    assert np.all(np.diff(actual_times) > 0.0)
    assert np.all(np.diff(reference_times) > 0.0)
    np.testing.assert_allclose(actual_times, reference_times, rtol=1e-12, atol=1e-12)


def _assert_frequency_axes(actual: Any, reference: Any, *, nyquist_hz: float) -> None:
    """Check frequency origin, spacing, Nyquist, and monotonic coordinates."""
    assert _axis_value(actual.f0, "Hz") == pytest.approx(
        _axis_value(reference.f0, "Hz"), rel=1e-12, abs=1e-12
    )
    assert _axis_value(actual.df, "Hz") == pytest.approx(
        _axis_value(reference.df, "Hz"), rel=1e-12, abs=1e-12
    )
    actual_frequencies = np.asarray(actual.frequencies.to_value("Hz"))
    reference_frequencies = np.asarray(reference.frequencies.to_value("Hz"))
    assert np.isfinite(actual_frequencies).all()
    assert np.isfinite(reference_frequencies).all()
    assert np.all(np.diff(actual_frequencies) > 0.0)
    assert np.all(np.diff(reference_frequencies) > 0.0)
    np.testing.assert_allclose(
        actual_frequencies, reference_frequencies, rtol=1e-12, atol=1e-12
    )
    assert actual_frequencies[-1] == pytest.approx(nyquist_hz, abs=1e-12)
    assert reference_frequencies[-1] == pytest.approx(nyquist_hz, abs=1e-12)


def _public_reference(operation_id: str, input_series: Any, hdf5_source: Path) -> Any:
    """Compute an oracle result with only the public gwexpy API."""
    import gwexpy

    gwexpy.register_all(include_io=True)
    from gwexpy.timeseries import TimeSeries

    if operation_id == "timeseries.read":
        return TimeSeries.read(hdf5_source, format="hdf5", name=TEST_NAME)
    if operation_id == "timeseries.crop":
        return input_series.crop(T0_GPS + 1.0, T0_GPS + 10.0)
    if operation_id == "timeseries.detrend":
        return input_series.detrend("linear")
    if operation_id == "timeseries.asd":
        return input_series.asd(fftlength=4.0)
    if operation_id == "timeseries.spectrogram":
        return input_series.spectrogram(stride=4.0, fftlength=2.0)
    raise AssertionError(f"unhandled curated operation: {operation_id}")


def _assert_scientific_contract(
    operation_id: str, actual: Any, reference: Any, *, expected_unit: str
) -> None:
    """Assert values, units, axes, and approved spectral invariants."""
    _assert_reference_values(actual, reference)
    assert u.Unit(actual.unit) == u.Unit(expected_unit)

    if operation_id in {"timeseries.read", "timeseries.crop", "timeseries.detrend"}:
        _assert_time_axes(actual, reference)
    elif operation_id == "timeseries.asd":
        _assert_frequency_axes(actual, reference, nyquist_hz=SAMPLE_RATE_HZ / 2.0)
        actual_values = np.asarray(actual.value)
        reference_values = np.asarray(reference.value)
        assert (actual_values >= 0.0).all()
        assert (reference_values >= 0.0).all()
        actual_frequencies = np.asarray(actual.frequencies.to_value("Hz"))
        reference_frequencies = np.asarray(reference.frequencies.to_value("Hz"))
        assert actual_frequencies[np.argmax(actual_values)] == pytest.approx(
            32.0, abs=1e-12
        )
        assert reference_frequencies[np.argmax(reference_values)] == pytest.approx(
            32.0, abs=1e-12
        )
    else:
        assert operation_id == "timeseries.spectrogram"
        assert np.asarray(actual.value).ndim == 2
        _assert_time_axes(actual, reference)
        _assert_frequency_axes(actual, reference, nyquist_hz=SAMPLE_RATE_HZ / 2.0)
        actual_values = np.asarray(actual.value)
        reference_values = np.asarray(reference.value)
        assert (actual_values >= 0.0).all()
        assert (reference_values >= 0.0).all()
        actual_frequencies = np.asarray(actual.frequencies.to_value("Hz"))
        reference_frequencies = np.asarray(reference.frequencies.to_value("Hz"))
        actual_peak = np.unravel_index(np.argmax(actual_values), actual_values.shape)
        reference_peak = np.unravel_index(
            np.argmax(reference_values), reference_values.shape
        )
        assert actual_frequencies[actual_peak[1]] == pytest.approx(32.0, abs=1e-12)
        assert reference_frequencies[reference_peak[1]] == pytest.approx(
            32.0, abs=1e-12
        )


def _inspect_emitted_call(
    source: str,
    *,
    target: str,
    receiver: str,
    method: str,
) -> ast.Call:
    """Require one executable assignment to one public gwexpy call."""
    tree = ast.parse(source)
    assert len(tree.body) == 1
    statement = tree.body[0]
    assert isinstance(statement, ast.Assign)
    assert len(statement.targets) == 1
    assigned = statement.targets[0]
    assert isinstance(assigned, ast.Name)
    assert assigned.id == target
    assert isinstance(statement.value, ast.Call)
    call = statement.value
    assert isinstance(call.func, ast.Attribute)
    assert isinstance(call.func.value, ast.Name)
    assert call.func.value.id == receiver
    assert call.func.attr == method
    compile(tree, "<emitted-operation>", "exec")
    return call


def _literal_call_parts(call: ast.Call) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Evaluate positional and keyword literals from one generated call."""
    positional = tuple(ast.literal_eval(argument) for argument in call.args)
    keywords: dict[str, Any] = {}
    for keyword in call.keywords:
        assert keyword.arg is not None
        assert keyword.arg not in keywords
        keywords[keyword.arg] = ast.literal_eval(keyword.value)
    return positional, keywords


@pytest.mark.parametrize(
    ("operation_id", "call_kwargs", "expected_kind", "expected_unit"),
    [
        pytest.param(
            "timeseries.read",
            {"format": "hdf5", "name": TEST_NAME},
            "TimeSeries",
            "m",
            id="read",
            marks=pytest.mark.contract("B-014"),
        ),
        pytest.param(
            "timeseries.crop",
            {"start": T0_GPS + 1.0, "end": T0_GPS + 10.0},
            "TimeSeries",
            "m",
            id="crop",
            marks=pytest.mark.contract("B-015"),
        ),
        pytest.param(
            "timeseries.detrend",
            {"detrend": "linear"},
            "TimeSeries",
            "m",
            id="detrend",
            marks=pytest.mark.contract("B-016"),
        ),
        pytest.param(
            "timeseries.asd",
            {"fftlength": 4.0},
            "FrequencySeries",
            "m / Hz(1/2)",
            id="asd",
            marks=pytest.mark.contract("B-017"),
        ),
        pytest.param(
            "timeseries.spectrogram",
            {"stride": 4.0, "fftlength": 2.0},
            "Spectrogram",
            "m2 / Hz",
            id="spectrogram",
            marks=pytest.mark.contract("B-018"),
        ),
    ],
)
def test_curated_apply_returns_finite_metadata_preserving_results(
    operation_id: str,
    call_kwargs: dict[str, object],
    expected_kind: str,
    expected_unit: str,
    timeseries: Any,
    linear_trend: Any,
    sine_32hz: Any,
    hdf5_source: Path,
) -> None:
    """Each apply path matches a separately computed public gwexpy result."""
    spec = REGISTRY[operation_id]
    if operation_id == "timeseries.detrend":
        input_series = linear_trend
    elif operation_id in {"timeseries.asd", "timeseries.spectrogram"}:
        input_series = sine_32hz
    else:
        input_series = timeseries
    original = np.array(input_series.value, copy=True)
    source_bytes = hdf5_source.read_bytes()
    reference = _public_reference(operation_id, input_series, hdf5_source)
    inputs = {} if operation_id == "timeseries.read" else {"self": input_series}
    if operation_id == "timeseries.read":
        call_kwargs = {"source": str(hdf5_source), **call_kwargs}

    result = spec.apply(inputs, call_kwargs)

    assert spec.result_kind == expected_kind
    assert np.asarray(result.value).dtype == np.float64
    _assert_scientific_contract(
        operation_id, result, reference, expected_unit=expected_unit
    )
    assert np.array_equal(input_series.value, original)
    assert hdf5_source.read_bytes() == source_bytes
    if operation_id == "timeseries.read":
        assert result.name == TEST_NAME
        assert str(result.channel) == CHANNEL_NAME
    elif operation_id == "timeseries.crop":
        assert _axis_value(result.t0, "s") == pytest.approx(T0_GPS + 1.0, abs=1e-12)
        assert _axis_value(result.duration, "s") == pytest.approx(
            9.0, rel=1e-12, abs=1e-12
        )
    elif operation_id == "timeseries.detrend":
        assert np.max(np.abs(np.asarray(result.value))) <= 1e-12


@pytest.mark.parametrize(
    (
        "operation_id",
        "context",
        "target",
        "receiver",
        "method",
        "expected_args",
        "expected_kwargs",
        "omitted_kwargs",
    ),
    [
        pytest.param(
            "timeseries.read",
            {
                "variable": "raw",
                "inputs": {},
                "params": {"source": "input.h5", "format": "hdf5"},
            },
            "raw",
            "TimeSeries",
            "read",
            ("input.h5",),
            {"format": "hdf5"},
            {"name"},
            id="read",
            marks=pytest.mark.contract("B-019"),
        ),
        pytest.param(
            "timeseries.crop",
            {
                "variable": "cropped",
                "inputs": {"self": "raw"},
                "params": {"start": 1.0, "end": 2.0},
            },
            "cropped",
            "raw",
            "crop",
            (1.0, 2.0),
            {},
            set(),
            id="crop",
            marks=pytest.mark.contract("B-020"),
        ),
        pytest.param(
            "timeseries.detrend",
            {
                "variable": "detrended",
                "inputs": {"self": "raw"},
                "params": {"detrend": "linear"},
            },
            "detrended",
            "raw",
            "detrend",
            ("linear",),
            {},
            set(),
            id="detrend",
            marks=pytest.mark.contract("B-021"),
        ),
        pytest.param(
            "timeseries.asd",
            {
                "variable": "asd",
                "inputs": {"self": "cropped"},
                "params": {"fftlength": {"value": 4000.0, "unit": "ms"}},
            },
            "asd",
            "cropped",
            "asd",
            (),
            {"fftlength": 4.0},
            {"overlap", "window", "method"},
            id="asd",
            marks=pytest.mark.contract("B-022"),
        ),
        pytest.param(
            "timeseries.spectrogram",
            {
                "variable": "specgram",
                "inputs": {"self": "raw"},
                "params": {
                    "stride": {"value": 4000.0, "unit": "ms"},
                    "fftlength": {"value": 2000.0, "unit": "ms"},
                },
            },
            "specgram",
            "raw",
            "spectrogram",
            (4.0,),
            {"fftlength": 2.0},
            {"overlap", "window"},
            id="spectrogram",
            marks=pytest.mark.contract("B-023"),
        ),
    ],
)
def test_curated_emit_generates_parseable_studio_free_code(
    operation_id: str,
    context: dict[str, object],
    target: str,
    receiver: str,
    method: str,
    expected_args: tuple[object, ...],
    expected_kwargs: dict[str, object],
    omitted_kwargs: set[str],
) -> None:
    """Every operation emits its exact public, executable call semantics."""
    with pytest.raises(AssertionError):
        _inspect_emitted_call("pass", target=target, receiver=receiver, method=method)

    source = REGISTRY[operation_id].emit(context)

    call = _inspect_emitted_call(
        source, target=target, receiver=receiver, method=method
    )
    actual_args, actual_kwargs = _literal_call_parts(call)
    assert actual_args == expected_args
    assert actual_kwargs == expected_kwargs
    assert omitted_kwargs.isdisjoint(actual_kwargs)
    assert "gwexpy_studio" not in source


@pytest.mark.integration
@pytest.mark.contract("B-024")
def test_public_gwexpy_read_oracle_round_trips_named_hdf5(
    timeseries: Any, hdf5_source: Path
) -> None:
    """The scientific oracle calls the public TimeSeries API directly."""
    from gwexpy.timeseries import TimeSeries

    loaded = TimeSeries.read(hdf5_source, format="hdf5")

    assert np.array_equal(loaded.value, timeseries.value)
    assert u.Unit(loaded.unit) == u.Unit("m")
    assert float(loaded.t0.to_value("s")) == T0_GPS
    assert float(loaded.sample_rate.to_value("Hz")) == SAMPLE_RATE_HZ
    assert float(loaded.dt.to_value("s")) == pytest.approx(
        1.0 / SAMPLE_RATE_HZ, rel=1e-12, abs=1e-12
    )
    assert loaded.name == TEST_NAME
    assert str(loaded.channel) == CHANNEL_NAME


@pytest.mark.integration
@pytest.mark.contract("B-025")
def test_public_gwexpy_crop_and_detrend_oracle_is_non_destructive(
    linear_trend: Any,
) -> None:
    """Crop and detrend use public APIs and preserve the source values."""
    original = np.array(linear_trend.value, copy=True)

    cropped = linear_trend.crop(T0_GPS + 1.0, T0_GPS + 10.0)
    detrended = linear_trend.detrend("linear")

    assert len(cropped) == 2304
    assert float(cropped.t0.to_value("s")) == T0_GPS + 1.0
    assert float(cropped.duration.to_value("s")) == pytest.approx(
        9.0, rel=1e-12, abs=1e-12
    )
    assert np.isfinite(cropped.value).all()
    assert np.isfinite(detrended.value).all()
    assert np.max(np.abs(detrended.value)) <= 1e-12
    assert np.array_equal(linear_trend.value, original)


@pytest.mark.integration
@pytest.mark.contract("B-026")
def test_public_gwexpy_asd_oracle_checks_peak_frequency_and_units(
    sine_32hz: Any,
) -> None:
    """The ASD oracle checks finite nonnegative values, df, Nyquist, and peak."""
    asd = sine_32hz.asd(fftlength=4.0)
    values = np.asarray(asd.value)
    frequencies = np.asarray(asd.frequencies.to_value("Hz"))

    assert np.isfinite(values).all()
    assert (values >= 0.0).all()
    assert np.all(np.diff(frequencies) > 0.0)
    assert float(asd.df.to_value("Hz")) == pytest.approx(0.25, abs=1e-12)
    assert frequencies[-1] == pytest.approx(128.0, abs=1e-12)
    assert frequencies[np.argmax(values)] == pytest.approx(32.0, abs=1e-12)
    assert u.Unit(asd.unit) == u.m / u.Hz**0.5


@pytest.mark.integration
@pytest.mark.contract("B-027")
def test_public_gwexpy_spectrogram_oracle_checks_axes_and_nonnegative_values(
    sine_32hz: Any,
) -> None:
    """The spectrogram oracle checks 2-D orientation metadata and nonnegativity."""
    import gwexpy

    gwexpy.register_all(include_io=False)
    spectrogram = sine_32hz.spectrogram(stride=4.0, fftlength=2.0)
    values = np.asarray(spectrogram.value)
    times = np.asarray(spectrogram.times.to_value("s"))
    frequencies = np.asarray(spectrogram.frequencies.to_value("Hz"))

    assert values.shape == (15, 257)
    assert np.isfinite(values).all()
    assert (values >= 0.0).all()
    assert np.all(np.diff(times) > 0.0)
    assert np.all(np.diff(frequencies) > 0.0)
    assert float(spectrogram.dt.to_value("s")) == pytest.approx(4.0, abs=1e-12)
    assert float(spectrogram.df.to_value("Hz")) == pytest.approx(0.5, abs=1e-12)
    assert frequencies[-1] == pytest.approx(128.0, abs=1e-12)
    peak = np.unravel_index(np.argmax(values), values.shape)
    assert frequencies[peak[1]] == pytest.approx(32.0, abs=1e-12)
    assert u.Unit(spectrogram.unit) == u.m**2 / u.Hz


@pytest.mark.unit
@pytest.mark.contract("B-080")
def test_independent_sine_parseval_asd_oracle(sine_32hz: Any) -> None:
    """Pure sine wave ASD integrates to exact Parseval power invariant P = 0.5."""
    operation = REGISTRY["timeseries.asd"]
    asd_result = operation.apply(
        {"self": sine_32hz}, {"fftlength": 4.0, "method": "welch"}
    )
    asd_values = np.asarray(asd_result.value)
    df = float(asd_result.df.to_value("Hz"))
    # Discrete integral of PSD = ASD^2 over positive frequencies:
    # int PSD(f) df = sum(ASD^2) * df
    # For A=1 pure sine wave, total power must equal 0.5 within Welch window
    # spectral leakage tolerance (rtol=5e-2)
    integrated_power = float(np.sum(asd_values**2) * df)
    assert np.isclose(integrated_power, 0.5, rtol=5e-2), (
        f"Parseval power {integrated_power} deviates from expected 0.5 (rtol=5e-2)"
    )


@pytest.mark.unit
@pytest.mark.contract("B-081")
def test_crop_rejects_out_of_span_bounds(
    timeseries: Any, hdf5_source: Path, tmp_path: Path
) -> None:
    """Crop operation rejects intervals outside the series span [t0, t0+duration]."""
    operation = REGISTRY["timeseries.crop"]
    t0 = float(timeseries.t0.to_value("s"))
    t1 = float(timeseries.span[1])

    # Out of span cases: left underflow, right overflow, completely outside
    out_of_bounds_params = [
        {"start": t0 - 5.0, "end": t0 + 10.0},
        {"start": t0 + 10.0, "end": t1 + 5.0},
        {"start": t0 - 20.0, "end": t0 - 5.0},
        {"start": t1 + 5.0, "end": t1 + 20.0},
    ]
    for params in out_of_bounds_params:
        with pytest.raises(ValueError):
            operation.apply({"self": timeseries}, params)
    assert_crop_value_error_worker_and_export(hdf5_source, timeseries, tmp_path)
