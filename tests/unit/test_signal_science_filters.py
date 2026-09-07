import json

import numpy as np
import pytest
from gwexpy.signal import filter_design
from gwexpy.timeseries import TimeSeries, TimeSeriesDict
from scipy import signal

from tests.unit.test_signal_science import assert_native_equal, science_apply

FILTERS = [
    ("lowpass", {"frequency": 30}),
    ("highpass", {"frequency": 20}),
    ("bandpass", {"flow": 15, "fhigh": 50}),
    ("notch", {"frequency": 30}),
    ("zpk", {"zeros": [0.0], "poles": [0.5], "gain": 0.5}),
]


@pytest.mark.parametrize("kind,params", FILTERS)
@pytest.mark.parametrize("filtfilt", [False, True])
def test_filter_and_response_match_native_and_scipy(kind, params, filtfilt):
    from gwexpy_studio.ops.native_filters import filter_response

    series = TimeSeries(
        np.random.default_rng(42).normal(size=2048),
        sample_rate=256,
        t0=1234,
        unit="m",
        name="input",
        channel="A",
    )
    original = series.copy()
    params = dict(params, filtfilt=filtfilt)
    output = science_apply("timeseries." + kind, {"self": series}, params)
    expected = getattr(series, kind)(**params)
    assert_native_equal(output.value, expected)
    assert str(output.value.channel) == str(series.channel)
    preview = filter_response("timeseries." + kind, {"self": series}, params)
    member = preview["members"][0]
    recipe = member["recipe"]
    sos = np.asarray(recipe["coefficients"])
    frequency, response = signal.freqz_sos(sos, worN=4096, fs=256)
    if filtfilt:
        response = abs(response) ** 2
    np.testing.assert_allclose(member["frequency"], frequency)
    np.testing.assert_allclose(member["response"], response, atol=1e-10)
    assert member["frequency_unit"] == "Hz"
    assert member["unit"] == ""
    assert recipe["filtfilt"] is filtfilt
    assert output.details["filter_recipes"][0]["coefficients"] == recipe["coefficients"]
    json.dumps(output.details, allow_nan=False)
    assert_native_equal(series, original)


@pytest.mark.parametrize("unit", ["rad/s", "Hz"])
def test_analog_zpk_response_uses_executed_digital_filter(unit):
    from gwexpy_studio.ops.native_filters import filter_response

    series = TimeSeries(np.random.default_rng(42).normal(size=2048), sample_rate=256)
    params = {
        "zeros": [],
        "poles": [-20.0] if unit == "rad/s" else [3.0],
        "gain": 20.0,
        "analog": True,
        "unit": unit,
        "normalize_gain": False,
        "filtfilt": True,
    }
    result = science_apply("timeseries.zpk", {"self": series}, params)
    assert_native_equal(result.value, series.zpk(**params))
    recipe = filter_response("timeseries.zpk", {"self": series}, params)["members"][0][
        "recipe"
    ]
    expected = filter_design.prepare_digital_filter(
        (params["zeros"], params["poles"], params["gain"]),
        analog=True,
        unit=unit,
        normalize_gain=False,
        sample_rate=256,
        output="sos",
    )
    np.testing.assert_allclose(recipe["coefficients"], expected)


@pytest.mark.parametrize(
    "kind,params",
    [
        ("lowpass", {"frequency": 0}),
        ("highpass", {"frequency": 128}),
        ("bandpass", {"flow": 30, "fhigh": 20}),
        ("notch", {"frequency": -1}),
        ("zpk", {"zeros": ["0.2+0.3j"], "poles": [0.5], "gain": 1}),
        ("zpk", {"zeros": [], "poles": [], "gain": float("inf")}),
    ],
)
def test_invalid_filter_is_rejected(kind, params):
    series = TimeSeries(np.ones(64), sample_rate=256)
    with pytest.raises(
        (ValueError, TypeError), match="frequency|Nyquist|flow|conjugate|finite"
    ):
        science_apply("timeseries." + kind, {"self": series}, params)


@pytest.mark.contract("SIG-0060")
def test_filter_batch_records_each_sample_rate_and_is_atomic():
    from gwexpy_studio.ops.native_filters import filter_response

    series = TimeSeriesDict(
        a=TimeSeries(np.ones(128), sample_rate=256),
        b=TimeSeries(np.ones(128), sample_rate=128),
    )
    preview = filter_response("timeseries.lowpass", {"self": series}, {"frequency": 20})
    assert [member["recipe"]["sample_rate"] for member in preview["members"]] == [
        256,
        128,
    ]
    snapshots = {key: value.copy() for key, value in series.items()}
    with pytest.raises(ValueError, match="Nyquist"):
        science_apply("timeseries.lowpass", {"self": series}, {"frequency": 80})
    for key in series:
        assert_native_equal(series[key], snapshots[key])


@pytest.mark.contract("SIG-0061")
def test_complex_conjugate_zpk_roots_accepted():
    series = TimeSeries(np.ones(128), sample_rate=128)
    params = {
        "zeros": ["0.2+0.3j", "0.2-0.3j"],
        "poles": ["0.5+0.2j", "0.5-0.2j"],
        "gain": 1.0,
    }
    result = science_apply("timeseries.zpk", {"self": series}, params)
    expected = series.zpk([0.2 + 0.3j, 0.2 - 0.3j], [0.5 + 0.2j, 0.5 - 0.2j], 1.0)
    assert_native_equal(result.value, expected)
