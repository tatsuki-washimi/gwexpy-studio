"""Exact native cadence preservation across detached scientific execution."""

import numpy as np
import pytest
from astropy import units as u
from gwexpy.timeseries import (
    TimeSeries,
    TimeSeriesDict,
    TimeSeriesList,
    TimeSeriesMatrix,
)

from gwexpy_studio.ops.native_containers import science_members, science_pairs
from gwexpy_studio.ops.native_selection import native_extract
from gwexpy_studio.ops.registry import REGISTRY


def cropped_series():
    raw = TimeSeries(
        np.random.default_rng(51).normal(size=1000),
        times=np.arange(1000) * 0.001,
        unit="m",
        name="sample",
        channel="X1:TEST",
    )
    return raw.crop(0.1, 0.9).detrend("linear")


@pytest.mark.contract("SIG-0076")
def test_cropped_native_cadence_survives_copy_and_member_extraction():
    source = cropped_series()
    assert source.dt.value == 0.001
    assert source.copy().dt != source.dt  # Reproduce upstream re-inference.
    parents = (source, TimeSeriesDict(a=source), TimeSeriesList([source]))
    copies = [science_members(parent)[0][1] for parent in parents]
    copies.extend(
        (
            native_extract(parents[1], {"key": "a"}),
            native_extract(parents[2], {"index": 0}),
        )
    )
    for copied in copies:
        assert type(copied) is type(source)
        assert copied.dt == source.dt
        assert copied.t0 == source.t0
        assert copied.unit == source.unit
        assert copied.name == source.name
        assert copied.channel == source.channel
        np.testing.assert_array_equal(copied.value, source.value)
        np.testing.assert_array_equal(copied.times, source.times)
        assert not np.shares_memory(copied.value, source.value)
        assert not np.shares_memory(copied.times, source.times)


@pytest.mark.contract("SIG-0077")
def test_original_cadence_mismatch_rejected_before_copy_can_equalize_it(monkeypatch):
    from gwexpy_studio.ops import native_containers

    source = cropped_series()
    changed = source.copy()
    np.testing.assert_array_equal(source.times, changed.times)

    def copy_forbidden(value):
        raise AssertionError("Axis mismatch must be rejected before copying")

    monkeypatch.setattr(native_containers, "science_copy", copy_forbidden)
    for left, right in (
        (source, changed),
        (TimeSeriesDict(a=source), TimeSeriesDict(a=changed)),
        (TimeSeriesList([source]), TimeSeriesList([changed])),
    ):
        with pytest.raises(ValueError, match="dt"):
            science_pairs(left, right)


@pytest.mark.contract("SIG-0078")
def test_cropped_psd_retains_native_fft_bin_count_and_values():
    source = cropped_series()
    params = {"fftlength": 0.1, "overlap": 0.0}
    expected = source.psd(**params)
    for parent in (source, TimeSeriesDict(a=source), TimeSeriesList([source])):
        result = REGISTRY["timeseries.psd"].apply({"self": parent}, params)
        actual = science_members(result)[0][1]
        assert actual.shape == expected.shape
        assert actual.df == expected.df
        np.testing.assert_array_equal(actual.frequencies, expected.frequencies)
        np.testing.assert_allclose(actual.value, expected.value)
        assert actual.unit == expected.unit


@pytest.mark.contract("SIG-0079")
def test_matrix_member_and_rebuild_preserve_declared_cadence():
    source = TimeSeriesMatrix(np.ones((1, 1, 800)), t0=0.1, dt=0.001)
    assert source.dt.value == 0.001
    original_times = source.times.copy()
    member = native_extract(source, {"row": 0, "col": 0})
    assert member.dt == source.dt
    np.testing.assert_array_equal(member.times, original_times)
    rebuilt = REGISTRY["data.multiply"].apply(
        {"self": source}, {"operand_mode": "scalar", "scalar": 2}
    )
    assert rebuilt.dt == source.dt
    np.testing.assert_array_equal(rebuilt.times, original_times)
    np.testing.assert_array_equal(rebuilt.value, source.value * 2)
    assert not np.shares_memory(rebuilt.times, source.times)


@pytest.mark.parametrize("family", ["FrequencySeries", "Spectrogram"])
def test_other_native_families_copy_detaches_metadata_and_coordinates(family):
    from gwexpy_studio.ops.native_containers import science_class, science_copy

    shape = (1000, 8) if family == "Spectrogram" else (1000,)
    kwargs = {"t0": 0.0, "dt": 0.001, "f0": 0.1, "df": 0.01}
    if family == "FrequencySeries":
        kwargs = {"f0": 0.0, "df": 0.001}
    source = science_class(family)(np.ones(shape), unit="m", **kwargs)[100:900]
    source.attrs = {"nested": ["original"]}
    copied = science_copy(source)
    for prop in ("dt", "t0", "df", "f0"):
        original = getattr(source, prop, None)
        if original is not None:
            assert getattr(copied, prop) == original
    for prop in ("times", "frequencies"):
        original = getattr(source, prop, None)
        if original is not None:
            np.testing.assert_array_equal(getattr(copied, prop), original)
            assert not np.shares_memory(getattr(copied, prop), original)
    copied.attrs["nested"].append("new")
    assert source.attrs == {"nested": ["original"]}
    assert not np.shares_memory(source.value, copied.value)


@pytest.mark.parametrize("family", ["FrequencySeries", "Spectrogram"])
def test_matrix_frequency_metadata_preserves_exact_declared_spacing(family):
    from gwexpy_studio.ops.native_containers import science_class

    if family == "Spectrogram":
        source = science_class(family + "Matrix")(
            np.ones((1, 1, 20, 30)),
            times=(0.1 + np.arange(20) * 0.001) * u.s,
            frequencies=(0.1 + np.arange(30) * 0.001) * u.Hz,
        )
        source.dt[...] = 0.001 * u.s
        source.df[...] = 0.001 * u.Hz
    else:
        source = science_class(family + "Matrix")(np.ones((1, 1, 30)), f0=0.1, df=0.001)
    member = native_extract(source, {"row": 0, "col": 0})
    rebuilt = REGISTRY["data.multiply"].apply(
        {"self": source}, {"operand_mode": "scalar", "scalar": 2}
    )
    for target in (member, rebuilt):
        assert target.df == source.df
        np.testing.assert_array_equal(target.frequencies, source.frequencies)
        assert not np.shares_memory(target.frequencies, source.frequencies)
        if family == "Spectrogram":
            assert target.dt == source.dt
            np.testing.assert_array_equal(target.times, source.times)
