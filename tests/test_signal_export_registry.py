import numpy as np
import pytest
from gwexpy.timeseries import TimeSeries, TimeSeriesDict, TimeSeriesMatrix


@pytest.mark.contract("SIG-0007")
def test_registry_covers_native_intake_and_extraction(tmp_path):
    from gwexpy_studio.ops.registry import REGISTRY

    series = TimeSeries(np.arange(32.0), sample_rate=8, unit="m", name="A")
    path = tmp_path / "input.h5"
    series.write(path, format="hdf5")
    read = REGISTRY["data.read"]
    result = read.apply(
        {}, {"datatype": "TimeSeries", "source": str(path), "format": "hdf5"}
    )
    np.testing.assert_array_equal(result.value, series.value)
    assert result.unit == series.unit
    extracted = REGISTRY["data.extract"].apply(
        {"self": TimeSeriesDict(a=series)}, {"selector": {"key": "a"}}
    )
    np.testing.assert_array_equal(extracted.value, series.value)
    assert not np.shares_memory(extracted.value, series.value)


@pytest.mark.parametrize("operation", ["crop", "detrend", "asd", "spectrogram"])
def test_legacy_registry_maps_containers_to_native_leaves(operation):
    from gwexpy_studio.ops.registry import REGISTRY

    series = TimeSeries(
        np.random.default_rng(3).normal(size=256), sample_rate=32, unit="m", name="A"
    )
    container = TimeSeriesDict(a=series, b=series * 2)
    params = {
        "crop": {"start": 1.0, "end": 5.0},
        "detrend": {"detrend": "linear"},
        "asd": {"fftlength": 1.0},
        "spectrogram": {"stride": 1.0, "fftlength": 1.0},
    }[operation]
    result = REGISTRY["timeseries." + operation].apply({"self": container}, params)
    expected = (
        getattr(series, operation)("linear")
        if operation == "detrend"
        else getattr(series, operation)(**params)
    )
    assert type(result).__name__ == type(expected).__name__ + "Dict"
    np.testing.assert_allclose(result["a"].value, expected.value)
    assert result["a"].unit == expected.unit


@pytest.mark.contract("SIG-0008")
def test_extract_matrix_indices_are_strict_and_preserve_metadata():
    from gwexpy_studio.ops.registry import REGISTRY

    matrix = TimeSeriesMatrix(
        np.ones((1, 2, 10)), dt=0.1, units=[["m", "s"]], names=[["A", "B"]]
    )
    spec = REGISTRY["data.extract"]
    selected = spec.apply({"self": matrix}, {"selector": {"row": 0, "col": 1}})
    assert selected.unit.to_string() == "s"
    assert selected.name == "B"
    for selector in (
        {"row": -1, "col": 0},
        {"row": True, "col": 0},
        {"index": 0},
        {"row": 0, "col": 2},
    ):
        with pytest.raises((ValueError, IndexError)):
            spec.apply({"self": matrix}, {"selector": selector})


@pytest.mark.contract("SIG-0009")
def test_extract_list_and_spectrogram_batch_return_independent_leaves():
    from astropy import units as u
    from gwexpy.spectrogram import SpectrogramMatrix
    from gwexpy.timeseries import TimeSeriesList

    from gwexpy_studio.ops.native_selection import native_extract

    leaf = TimeSeries([1.0, 2.0], unit="m")
    listed = TimeSeriesList([leaf])
    selected = native_extract(listed, {"index": 0})
    assert selected.unit == leaf.unit
    assert not np.shares_memory(selected.value, leaf.value)
    batch = SpectrogramMatrix(
        np.ones((2, 3, 4)), times=np.arange(3) * u.s, frequencies=np.arange(4) * u.Hz
    )
    selected = native_extract(batch, {"batch": 1})
    assert selected.shape == (3, 4)
    for value, selector in (
        (leaf, {"index": 0}),
        (listed, {"index": -1}),
        (listed, None),
        (TimeSeriesDict(a=leaf), {"key": True}),
    ):
        with pytest.raises(ValueError):
            native_extract(value, selector)


@pytest.mark.parametrize(
    "params", [{"start": -1}, {"end": 100}, {"start": 2, "end": 1}]
)
def test_legacy_container_crop_rejects_invalid_bounds(params):
    from gwexpy_studio.ops.registry import REGISTRY

    series = TimeSeries(np.arange(8.0), sample_rate=1)
    with pytest.raises(ValueError, match="Crop"):
        REGISTRY["timeseries.crop"].apply({"self": TimeSeriesDict(a=series)}, params)
