import numpy as np
import pytest
from gwexpy.frequencyseries import FrequencySeriesDict, FrequencySeriesMatrix
from gwexpy.timeseries import TimeSeries, TimeSeriesDict, TimeSeriesMatrix

from tests.unit.test_signal_science import assert_native_equal, science_apply


def signal_pair():
    rng = np.random.default_rng(321)
    first = TimeSeries(
        rng.normal(size=8192), sample_rate=256, t0=123, unit="m", name="A"
    )
    second = TimeSeries(
        2 * np.roll(first.value, 3), sample_rate=256, t0=123, unit="s", name="B"
    )
    return first, second


@pytest.mark.parametrize("operation", ["psd", "csd", "coherence", "transfer_function"])
def test_spectral_native_parity(operation):
    first, second = signal_pair()
    params = {"fftlength": 2.0, "overlap": 1.0, "window": "hann"}
    inputs = {"self": first}
    if operation == "psd":
        expected = first.psd(**params)
    else:
        inputs["other"] = second
        expected = getattr(first, operation)(second, **params)
    result = science_apply("timeseries." + operation, inputs, params)
    assert_native_equal(result, expected)


@pytest.mark.contract("SIG-0062")
def test_transfer_function_gain_and_delay_have_b_over_a_direction():
    first, second = signal_pair()
    result = science_apply(
        "timeseries.transfer_function",
        {"self": first, "other": second},
        {"fftlength": 4, "overlap": 2},
    )
    frequencies = result.frequencies.to_value("Hz")
    selection = (frequencies > 5) & (frequencies < 100)
    expected = 2 * np.exp(-2j * np.pi * frequencies[selection] * 3 / 256)
    np.testing.assert_allclose(result.value[selection], expected, atol=0.025)
    assert result.unit.to_string() == "s / m"


@pytest.mark.parametrize("operation", ["csd", "coherence", "transfer_function"])
def test_pairwise_spectral_container_uses_matching_keys(operation):
    first, second = signal_pair()
    left = TimeSeriesDict(a=first, b=second)
    right = TimeSeriesDict(b=second * 2, a=first * 3)
    result = science_apply(
        "timeseries." + operation, {"self": left, "other": right}, {"fftlength": 2}
    )
    assert isinstance(result, FrequencySeriesDict)
    assert list(result) == ["a", "b"]
    for key in left:
        assert_native_equal(
            result[key], getattr(left[key], operation)(right[key], fftlength=2)
        )


@pytest.mark.contract("SIG-0063")
def test_matrix_psd_preserves_per_cell_units_and_labels():
    first, second = signal_pair()
    matrix = TimeSeriesMatrix(
        np.array([[first.value], [second.value]]),
        sample_rate=256,
        units=[["m"], ["s"]],
        names=[["A"], ["B"]],
        rows=["one", "two"],
        cols=["channel"],
    )
    result = science_apply("timeseries.psd", {"self": matrix}, {"fftlength": 2})
    assert isinstance(result, FrequencySeriesMatrix)
    assert result.row_keys() == matrix.row_keys()
    for index in range(2):
        assert_native_equal(result[index, 0], matrix[index, 0].psd(fftlength=2))


@pytest.mark.parametrize("operation", ["csd", "coherence", "transfer_function"])
def test_spectral_refuses_shifted_native_input_before_backend_alignment(operation):
    first, second = signal_pair()
    second.t0 = second.t0.to_value("s") + 1
    with pytest.raises(ValueError, match="matching"):
        science_apply(
            "timeseries." + operation,
            {"self": first, "other": second},
            {"fftlength": 2},
        )


@pytest.mark.contract("SIG-0064")
def test_resample_is_explicit_and_matches_public_method():
    first, _ = signal_pair()
    result = science_apply(
        "timeseries.resample",
        {"self": first},
        {"rate": {"value": 0.128, "unit": "kHz"}},
    )
    assert_native_equal(result, first.resample(128))


@pytest.mark.parametrize(
    "params",
    [
        {"fftlength": 0},
        {"fftlength": 2, "overlap": 2},
        {"overlap": -1},
        {"fftlength": 100},
    ],
)
def test_spectral_validates_segment_parameters(params):
    first, _ = signal_pair()
    with pytest.raises(ValueError, match="fftlength|overlap"):
        science_apply("timeseries.psd", {"self": first}, params)
