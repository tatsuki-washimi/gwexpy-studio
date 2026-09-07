"""Native format delegation, typed options, and bounded intake regressions."""

import numpy as np
import pytest
from astropy import units as u


@pytest.mark.contract("SIG-0032")
def test_typed_options_preserve_quantities_and_complex_values():
    from gwexpy_studio.ops.values import decode_value, encode_value

    original = {"quantity": 2 * u.m, "roots": (1 + 2j, 1 - 2j), "pad": np.nan}
    encoded = encode_value(original)
    restored = decode_value(encoded)
    assert restored["quantity"] == 2 * u.m
    assert restored["roots"] == original["roots"]
    assert np.isnan(restored["pad"])
    with pytest.raises(ValueError, match="Unknown"):
        decode_value({"__type__": "python", "value": "raise RuntimeError()"})


@pytest.mark.parametrize("family", ["TimeSeries", "FrequencySeries", "Spectrogram"])
@pytest.mark.parametrize("container", ["", "Dict", "List"])
def test_native_hdf5_all_single_dict_list_classes(tmp_path, family, container):
    from gwexpy_studio.ops.io import io_class, read_data, write_data

    cls = io_class(family)
    kwargs = {"unit": "m", "name": "channel"}
    if family == "TimeSeries":
        leaf = cls(np.arange(16.0), t0=10, dt=0.25, **kwargs)
    elif family == "FrequencySeries":
        leaf = cls(np.arange(16.0) + 1j, f0=1, df=0.5, **kwargs)
    else:
        leaf = cls(np.arange(48.0).reshape(3, 16), t0=10, dt=1, f0=1, df=0.5, **kwargs)
    target_class = io_class(family + container)
    value = (
        target_class({"日本語": leaf})
        if container == "Dict"
        else (target_class([leaf]) if container == "List" else leaf)
    )
    path = tmp_path / "日本語 data.h5"
    write_data(value, str(path), format="hdf5")
    result = read_data(family + container, str(path), format="hdf5")
    assert type(result) is type(value)
    result_leaf = (
        result["日本語"]
        if container == "Dict"
        else (result[0] if container == "List" else result)
    )
    np.testing.assert_array_equal(result_leaf.value, leaf.value)
    assert result_leaf.unit == leaf.unit


@pytest.mark.contract("SIG-0033")
def test_new_registered_format_reaches_native_io_without_adapter_changes(tmp_path):
    from gwpy.io.registry import default_registry

    from gwexpy_studio.ops.io import io_catalog, io_class, read_data, write_data

    cls = io_class("TimeSeries")
    format_name = "studio_future_signal_test"

    def reader(path, *, offset=0, start=None, end=None):
        assert start is None and end is None
        return cls(np.loadtxt(path) + offset, dt=0.25, unit="V")

    def writer(data, path, *, multiplier=1):
        np.savetxt(path, data.value * multiplier)

    default_registry.register_reader(format_name, cls, reader)
    default_registry.register_writer(format_name, cls, writer)
    try:
        catalogue = io_catalog("TimeSeries", "read")
        assert format_name in {item["format"] for item in catalogue["formats"]}
        path = tmp_path / "unknown.custom"
        original = cls([1.0, 2.0], dt=0.25, unit="V")
        write_data(original, str(path), format=format_name, kwargs={"multiplier": 3})
        result = read_data(
            "TimeSeries", str(path), format=format_name, kwargs={"offset": 4}
        )
        np.testing.assert_array_equal(result.value, [7, 10])
    finally:
        default_registry.unregister_reader(format_name, cls)
        default_registry.unregister_writer(format_name, cls)


@pytest.mark.contract("SIG-0034")
def test_reserved_kwargs_cannot_override_visible_io_selection(tmp_path):
    from gwexpy_studio.ops.io import read_data

    with pytest.raises(ValueError, match="reserved"):
        read_data(
            "TimeSeries", str(tmp_path / "a"), format="hdf5", kwargs={"format": "csv"}
        )


@pytest.mark.contract("SIG-0035")
def test_directory_bounds_and_identity_change(tmp_path):
    from gwexpy_studio.ops.intake import inspect_io, validate_inspection

    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    first.write_text("0,1\n1,2\n")
    second.write_text("0,3\n1,4\n")
    request = {
        "paths": [str(tmp_path)],
        "datatype": "TimeSeriesDict",
        "format": "csv",
        "max_entries": 2,
    }
    inspection = inspect_io(request)
    assert inspection["entry_count"] == 2
    validate_inspection(inspection)
    first.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        validate_inspection(inspection)
    with pytest.raises(ValueError, match="entry"):
        inspect_io({**request, "max_entries": 1})
    with pytest.raises(ValueError, match="byte"):
        inspect_io({**request, "max_bytes": 1})


@pytest.mark.contract("SIG-0036")
def test_intake_rejects_empty_sources_and_cycles(tmp_path):
    from gwexpy_studio.ops.intake import inspect_io

    with pytest.raises(ValueError, match="source"):
        inspect_io({"paths": [], "datatype": "TimeSeries"})
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="cycle"):
        inspect_io(
            {"paths": [str(tmp_path)], "datatype": "TimeSeriesDict", "format": "hdf5"}
        )


@pytest.mark.parametrize(
    "options",
    [
        {"paths": []},
        {"paths": ["https://example.test/data"]},
        {"paths": ["file://remote/data"]},
        {"paths": [""]},
        {"max_bytes": True},
        {"max_entries": 0},
        {"combine": "implicit"},
        {"args": {}},
        {"kwargs": []},
        {"format": " "},
        {"format": "x" * 257},
        {"datatype": "UntrustedClass"},
    ],
)
def test_invalid_intake_options_do_not_reach_readers(tmp_path, options):
    from gwexpy_studio.ops.intake import inspect_io

    path = tmp_path / "data.csv"
    path.write_text("0,1\n1,2\n")
    with pytest.raises((ValueError, TypeError)):
        inspect_io({"paths": [str(path)], "format": "csv", **options})


@pytest.mark.contract("SIG-0037")
def test_native_direct_io_outside_registry_and_safe_options(tmp_path, monkeypatch):
    from gwexpy_studio.ops.io import io_class, read_data, write_data

    cls = io_class("SpectrogramList")
    seen = []

    def native_read(self, path, *args, **kwargs):
        seen.append((path, args, kwargs))
        assert path == str(tmp_path / "data.direct")
        return self

    monkeypatch.setattr(cls, "read", native_read)
    result = read_data(
        "SpectrogramList",
        str(tmp_path / "data.direct"),
        format="future_direct",
        args=[{"__type__": "quantity", "value": 2, "unit": "s"}],
    )
    assert type(result) is cls
    assert seen[0][1] == (2 * u.s,)
    for args, kwargs in (({}, {}), ([], []), ([], {"source": "hidden"})):
        with pytest.raises(ValueError):
            read_data("TimeSeries", "path", args=args, kwargs=kwargs)
    with pytest.raises(ValueError):
        read_data("TimeSeries", [])
    monkeypatch.setattr(
        cls, "write", lambda self, path, **kwargs: seen.append((path, (), kwargs))
    )
    write_data(result, "out.direct", format="future_direct", kwargs={"option": [1, 2]})
    assert seen[-1][2] == {"format": "future_direct", "option": [1, 2]}


@pytest.mark.contract("SIG-0038")
def test_native_auto_identification_unique_and_ambiguous(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from gwexpy_studio.ops import io
    from gwexpy_studio.ops.intake import inspect_io

    path = tmp_path / "a.native"
    path.write_bytes(b"actual local file")
    registry = SimpleNamespace(identify_format=lambda *args: ["future_native"])
    monkeypatch.setattr(io, "_io_registries", lambda: (registry, registry))
    inspected = inspect_io({"paths": [path.as_uri()], "datatype": "TimeSeries"})
    assert inspected["format"] == "future_native"
    assert inspected["paths"] == [str(path)]
    registry.identify_format = lambda *args: ["one", "two"]
    with pytest.raises(ValueError, match="explicitly"):
        inspect_io({"paths": [str(path)]})


@pytest.mark.contract("SIG-0080")
def test_default_ten_thousand_entry_inspection_fits_control_plane(tmp_path):
    import json

    from gwexpy_studio.ops.intake import inspect_io, validate_inspection
    from gwexpy_studio.worker.protocol import CONTROL_MESSAGE_LIMIT_BYTES

    for index in range(10_000):
        (tmp_path / f"signal_{index:05d}.csv").write_bytes(b"0,1\n")
    inspected = inspect_io(
        {"datatype": "TimeSeriesDict", "paths": [str(tmp_path)], "format": "csv"}
    )
    assert inspected["entry_count"] == 10_000
    assert len(json.dumps(inspected).encode()) < CONTROL_MESSAGE_LIMIT_BYTES - 1000
    validate_inspection(inspected)
    (tmp_path / "signal_09999.csv").write_bytes(b"1,2\n")
    with pytest.raises(ValueError, match="changed"):
        validate_inspection(inspected)
