"""Native object and container metadata boundaries."""

from __future__ import annotations

import numpy as np
import pytest

from gwexpy_studio.runtime.store import ObjectStore


@pytest.mark.parametrize("suffix", ["Dict", "List"])
def test_member_copy_preserves_exact_native_cadence_after_explicit_crop(suffix):
    from gwexpy.timeseries import TimeSeries, TimeSeriesDict, TimeSeriesList

    source = (
        TimeSeries(np.arange(1000), times=np.arange(1000) * 0.001)
        .crop(0.1, 0.9)
        .detrend("linear")
    )
    source.attrs = {"nested": ["source"]}
    container = (
        TimeSeriesDict({"x": source}) if suffix == "Dict" else TimeSeriesList([source])
    )
    selector = {"key": "x"} if suffix == "Dict" else {"index": 0}
    store = ObjectStore()
    parent = store.put(container)
    member = store.get_member(parent.object_id, selector)
    assert member.dt == source.dt
    assert type(member) is type(source)
    assert not np.shares_memory(member.value, source.value)
    np.testing.assert_array_equal(member.times.value, source.times.value)
    member.times.value[0] = 99
    assert source.times.value[0] == 0.1
    member.attrs["nested"].append("member")
    assert source.attrs == {"nested": ["source"]}
    extracted = store.extract_member(parent.object_id, selector)
    assert store.get(extracted.object_id).dt == source.dt


@pytest.mark.parametrize("family", ["TimeSeries", "FrequencySeries"])
def test_matrix_member_retains_parent_declared_cadence_and_full_coordinates(family):
    import importlib

    cls = getattr(
        importlib.import_module("gwexpy." + family.lower()), family + "Matrix"
    )
    step, origin, coordinates = (
        ("dt", "t0", "times") if family == "TimeSeries" else ("df", "f0", "frequencies")
    )
    source = cls(np.ones((1, 1, 800)), **{origin: 0.1, step: 0.001})
    original = getattr(source, coordinates).copy()
    original_step = getattr(source, step).copy()
    store = ObjectStore()
    parent = store.put(source)
    member = store.get_member(parent.object_id, {"row": 0, "col": 0})
    assert getattr(member, step) == getattr(source, step)
    np.testing.assert_array_equal(getattr(member, coordinates), original)
    store.list_members(parent.object_id)
    assert getattr(source, step) == original_step
    np.testing.assert_array_equal(getattr(source, coordinates), original)
    assert len(store) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_spectrogram_matrix_members_with_independent_epoch_use_explicit_axes(batch):
    from astropy import units as u
    from gwexpy.spectrogram import SpectrogramMatrix
    from gwexpy.types.metadata import MetaDataMatrix

    values = np.arange(600.0).reshape((1, 20, 30) if batch else (1, 1, 20, 30))
    source = SpectrogramMatrix(
        values,
        times=(0.1 + np.arange(20) * 0.001) * u.s,
        frequencies=(0.1 + np.arange(30) * 0.001) * u.Hz,
        meta=MetaDataMatrix([[{"unit": "m / Hz", "name": "sensor"}]]),
    )
    original_times, original_frequencies = (
        source.times.copy(),
        source.frequencies.copy(),
    )
    source.attrs = {"nested": ["source"]}
    store = ObjectStore()
    parent = store.put(source)
    selector = {"batch": 0} if batch else {"row": 0, "col": 0}
    assert store.list_members(parent.object_id)[0].unit == "m / Hz"
    assert len(store) == 1
    leaf = store.get_member(parent.object_id, selector)
    assert type(leaf).__name__ == "Spectrogram"
    assert leaf.name == "sensor"
    assert leaf.unit == u.m / u.Hz
    np.testing.assert_array_equal(leaf.value, values.reshape(20, 30))
    np.testing.assert_array_equal(leaf.times, original_times)
    np.testing.assert_array_equal(leaf.frequencies, original_frequencies)
    leaf.attrs["nested"].append("leaf")
    assert source.attrs == {"nested": ["source"]}
    ref = store.extract_member(parent.object_id, selector)
    assert store.get(ref.object_id).dt == source.dt
    np.testing.assert_array_equal(source.times, original_times)
    np.testing.assert_array_equal(source.frequencies, original_frequencies)


@pytest.mark.contract("SIG-0082")
def test_spectrogram_matrix_missing_native_frequency_axis_is_not_invented():
    from astropy import units as u
    from gwexpy.spectrogram import SpectrogramMatrix

    source = SpectrogramMatrix(np.ones((1, 1, 3, 4)), times=np.arange(3) * u.s)
    store = ObjectStore()
    parent = store.put(source)
    with pytest.raises(ValueError, match="native.*coordinates"):
        store.get_member(parent.object_id, {"row": 0, "col": 0})
    assert source.frequencies is None
    assert len(store) == 1


def _native(kind: str):
    import importlib

    family = next(
        k
        for k in ("TimeSeries", "FrequencySeries", "Spectrogram")
        if kind.startswith(k)
    )
    module = importlib.import_module(f"gwexpy.{family.lower()}")
    cls = getattr(module, kind)
    single = getattr(module, family)
    kwargs = {"t0": 0, "dt": 0.25} if family == "TimeSeries" else {"f0": 0, "df": 0.5}
    if family == "Spectrogram":
        kwargs = {"t0": 0, "dt": 0.25, "f0": 0, "df": 0.5}
    shape = (3, 4) if family == "Spectrogram" else (4,)
    values = np.arange(np.prod(shape)).reshape(shape).astype(complex) + 1j
    a = single(values, unit="m", name="length", **kwargs)
    b = single(values, unit="V", name="voltage", **kwargs)
    if kind.endswith("Dict"):
        return cls({"length": a, 7: b})
    if kind.endswith("List"):
        return cls([a, b])
    if kind.endswith("Matrix"):
        if family == "Spectrogram":
            kwargs = {"times": np.arange(3) * 0.25, "frequencies": np.arange(4) * 0.5}
        return cls(np.stack([values, values]).reshape(1, 2, *shape), **kwargs)
    return a


@pytest.mark.parametrize("family", ["TimeSeries", "FrequencySeries", "Spectrogram"])
@pytest.mark.parametrize("suffix", ["", "Dict", "List", "Matrix"])
def test_native_class_and_members_are_preserved_without_browse_materialization(
    family, suffix
):
    value = _native(family + suffix)
    store = ObjectStore()
    ref = store.put(value)
    assert store.get(ref.object_id) is value
    assert store.describe(ref.object_id) == ref
    assert ref.kind == family + suffix
    assert ref.metadata["native_class"].endswith("." + family + suffix)
    members = store.list_members(ref.object_id)
    assert len(store) == 1
    if suffix:
        assert ref.metadata["member_count"] == 2
        assert ref.members == ()
        assert len(members) == 2
        assert members[0].kind == family
        assert store.list_members(ref.object_id, offset=1, limit=1) == members[1:]
        selected = store.get_member(ref.object_id, members[0].selector)
        assert type(selected).__name__ == family
        before = selected.value.copy()
        selected.value[...] = 99
        np.testing.assert_array_equal(
            store.get_member(ref.object_id, members[0].selector).value, before
        )
        assert len(store) == 1
        extracted = store.extract_member(ref.object_id, members[0].selector)
        assert len(store) == 2
        assert extracted.metadata["member_selector"] == members[0].selector
    else:
        assert members == ()
        assert ref.dtype == "complex128"


@pytest.mark.parametrize("suffix", ["Dict", "List"])
def test_mixed_collection_never_invents_uniform_unit_or_dtype(suffix):
    value = _native("TimeSeries" + suffix)
    if suffix == "Dict":
        value[7] = value[7].astype(np.complex64)
    else:
        value[1] = value[1].astype(np.complex64)
    store = ObjectStore()
    ref = store.put(value)
    assert ref.dtype is None
    assert ref.unit is None
    assert ref.shape == (2,)
    members = store.list_members(ref.object_id)
    assert [member.unit for member in members] == ["m", "V"]
    assert [member.dtype for member in members] == ["complex128", "complex64"]
    assert [member.name for member in members] == ["length", "voltage"]
    assert members[1].selector == ({"key": 7} if suffix == "Dict" else {"index": 1})


@pytest.mark.parametrize(
    "family,axis", [("TimeSeries", "time"), ("FrequencySeries", "frequency")]
)
def test_irregular_coordinates_remain_exact_and_outside_json_arrays(family, axis):
    import importlib

    cls = getattr(importlib.import_module(f"gwexpy.{family.lower()}"), family)
    coordinates = np.array([1, 3, 8], dtype=np.longdouble) / 11
    value = cls(
        [1 + 2j, 3 + 4j, 5 + 6j],
        **{("times" if axis == "time" else "frequencies"): coordinates},
    )
    store = ObjectStore()
    ref = store.put(value)
    descriptor = ref.axes[axis]
    assert descriptor == {
        "kind": "explicit",
        "length": 3,
        "dtype": str(coordinates.dtype),
        "unit": "s" if axis == "time" else "Hz",
    }
    assert ("dt" if axis == "time" else "df") not in ref.axes
    recovered = store.get_axis_values(ref.object_id, axis)
    assert recovered.dtype == coordinates.dtype
    np.testing.assert_array_equal(recovered, coordinates)
    recovered[...] = 0
    np.testing.assert_array_equal(
        store.get_axis_values(ref.object_id, axis), coordinates
    )


@pytest.mark.contract("SIG-0027")
def test_invalid_member_selectors_and_axis_names_are_rejected():
    store = ObjectStore()
    ref = store.put(_native("TimeSeriesList"))
    for selector in (
        {"index": -1},
        {"index": True},
        {"key": "length"},
        {"index": 0, "extra": 1},
    ):
        with pytest.raises((KeyError, ValueError, TypeError, IndexError)):
            store.get_member(ref.object_id, selector)
    with pytest.raises(ValueError):
        store.get_axis_values(ref.object_id, "anything")


@pytest.mark.contract("SIG-0028")
def test_explicit_ids_do_not_get_overwritten_by_automatic_allocation():
    store = ObjectStore()
    value = _native("TimeSeries")
    store.put(value, object_id="obj-1")
    assert store.put(value.copy()).object_id == "obj-2"


@pytest.mark.parametrize("family", ["TimeSeries", "FrequencySeries", "Spectrogram"])
@pytest.mark.parametrize("suffix", ["Dict", "List"])
def test_hdf5_loaded_collections_keep_native_parents_and_member_metadata(
    tmp_path, family, suffix
):
    import gwexpy

    gwexpy.register_all()
    source = _native(family + suffix)
    if suffix == "Dict":
        source = type(source)({"length": source["length"], "voltage": source[7]})
    path = str(tmp_path / "native.h5")
    source.write(path, format="hdf5")
    reader = type(source)() if family == "Spectrogram" else type(source)
    loaded = reader.read(path, format="hdf5")
    store = ObjectStore()
    ref = store.put(loaded)
    assert store.get(ref.object_id) is loaded
    assert ref.kind == family + suffix
    assert [member.unit for member in store.list_members(ref.object_id)] == ["m", "V"]
    assert ref.unit is None
    assert len(store) == 1


@pytest.mark.contract("SIG-0029")
def test_matrix_units_labels_and_batch_selectors_preserve_native_meaning():
    from gwexpy.spectrogram import SpectrogramMatrix
    from gwexpy.timeseries import TimeSeriesMatrix

    value = TimeSeriesMatrix(
        np.ones((1, 2, 4)),
        t0=0,
        dt=0.25,
        unit=["m", "V"],
        rows=["sensor"],
        cols=["length", "voltage"],
    )
    store = ObjectStore()
    ref = store.put(value)
    assert ref.unit is None
    assert ref.metadata["row_labels"] == ["sensor"]
    assert ref.metadata["column_labels"] == ["length", "voltage"]
    assert [member.unit for member in store.list_members(ref.object_id)] == ["m", "V"]
    assert store.list_members(ref.object_id)[1].selector == {"row": 0, "col": 1}
    batch = SpectrogramMatrix(
        np.ones((2, 3, 4)), times=np.array([0.0, 0.3, 1.0]), frequencies=np.arange(4)
    )
    batch_ref = store.put(batch)
    assert batch_ref.axes["time"]["kind"] == "explicit"
    assert "dt" not in batch_ref.axes
    assert [member.selector for member in store.list_members(batch_ref.object_id)] == [
        {"batch": 0},
        {"batch": 1},
    ]
    assert (
        type(store.get_member(batch_ref.object_id, {"batch": 1})).__name__
        == "Spectrogram"
    )
    np.testing.assert_array_equal(
        store.get_axis_values(batch_ref.object_id, "time"), batch.times
    )


@pytest.mark.contract("SIG-0030")
def test_matrix_parent_registration_and_member_pages_avoid_unrequested_slices(
    monkeypatch,
):
    value = _native("TimeSeriesMatrix")
    native_getitem = type(value).__getitem__
    selections = []

    def tracked_getitem(self, key):
        selections.append(key)
        return native_getitem(self, key)

    monkeypatch.setattr(type(value), "__getitem__", tracked_getitem)
    store = ObjectStore()
    ref = store.put(value)
    assert selections == []
    assert len(store.list_members(ref.object_id, offset=1, limit=1)) == 1
    assert selections == [(0, 1)]


@pytest.mark.contract("SIG-0031")
def test_integer_coordinates_above_float_precision_are_not_mistaken_for_uniform():
    from gwexpy.spectrogram import SpectrogramMatrix

    times = np.array([2**60, 2**60 + 1, 2**60 + 3], dtype=np.int64)
    value = SpectrogramMatrix(np.ones((1, 3, 4)), times=times, frequencies=np.arange(4))
    store = ObjectStore()
    ref = store.put(value)
    assert ref.axes["time"]["kind"] == "explicit"
    assert ref.axes["time"]["dtype"] == "int64"
    np.testing.assert_array_equal(store.get_axis_values(ref.object_id, "time"), times)
