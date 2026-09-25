"""Native format delegation, typed options, and bounded intake regressions."""

import numpy as np
import pytest
from astropy import units as u


def _assert_timeseries_equal(adapted, direct):
    """Compare data, representation, coordinates, and units across read paths."""
    assert type(adapted) is type(direct)
    np.testing.assert_array_equal(adapted.value, direct.value)
    assert adapted.t0 == direct.t0
    assert adapted.dt == direct.dt
    if hasattr(adapted, "unit"):
        assert adapted.unit == direct.unit
    else:
        np.testing.assert_array_equal(adapted.units, direct.units)


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


@pytest.mark.parametrize(
    ("datatype", "products"),
    [
        ("TimeSeries", "TS"),
        ("TimeSeriesDict", "TS"),
        ("TimeSeriesMatrix", "TS"),
    ],
)
def test_diaggui_adapter_forwards_products_without_claiming_reader_success(
    monkeypatch, tmp_path, datatype, products
):
    """Pass the required product option; this fake-reader test is not a parse test."""
    from gwexpy_studio.ops import io

    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)
    calls = []

    class Native:
        @classmethod
        def read(cls, source, *args, **kwargs):
            calls.append((source, args, kwargs))
            return "native-result"

    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)

    result = io.read_data(
        datatype,
        str(tmp_path / "measurement.xml"),
        format="xml.diaggui",
        kwargs={"station": "X1"},
    )

    assert result == "native-result"
    assert calls == [
        (
            str(tmp_path / "measurement.xml"),
            (),
            {
                "station": "X1",
                "format": "xml.diaggui",
                "products": products,
            },
        )
    ]


@pytest.mark.parametrize(
    "datatype", ["TimeSeries", "TimeSeriesDict", "TimeSeriesMatrix"]
)
@pytest.mark.parametrize("format_name", ["xml.diaggui", None])
def test_diaggui_rejects_product_incompatible_with_selected_timeseries_type(
    monkeypatch, tmp_path, datatype, format_name
):
    """The selected Studio datatype, not custom options, selects XML products."""
    from gwexpy_studio.ops import io

    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)
    calls = []

    class Native:
        @classmethod
        def read(cls, source, *args, **kwargs):
            calls.append((source, args, kwargs))
            return "native-result"

    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)

    with pytest.raises(ValueError, match="products='TS'"):
        io.read_data(
            datatype,
            str(tmp_path / "measurement.xml"),
            format=format_name,
            kwargs={"products": "ASD"},
        )

    assert calls == []


@pytest.mark.parametrize(
    ("datatype", "format_name"),
    [
        ("TimeSeries", "gwf.lalframe"),
        ("TimeSeries", "gwf"),
        ("TimeSeriesDict", "gwf.lalframe"),
        ("TimeSeriesDict", "gwf"),
        ("TimeSeries", "hdf.ndscope"),
        ("TimeSeriesDict", "hdf.ndscope"),
    ],
)
def test_explicit_trial_format_reaches_native_reader_unchanged(
    monkeypatch, tmp_path, datatype, format_name
):
    """Do not alias or auto-detect a trial reader after explicit user selection."""
    from gwexpy_studio.ops import io

    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)
    calls = []

    class Native:
        @classmethod
        def read(cls, source, *args, **kwargs):
            calls.append((source, args, kwargs))
            return "native-result"

    monkeypatch.setattr(io, "io_class", lambda _datatype: Native)
    source = str(tmp_path / "selected-input")

    result = io.read_data(datatype, source, format=format_name)

    assert result == "native-result"
    assert calls == [(source, (), {"format": format_name})]


@pytest.mark.parametrize("format_name", ["gwf.lalframe", "gwf"])
def test_gwf_adapter_matches_native_reader_for_real_fixture(monkeypatch, format_name):
    """Read real frame data through both reviewed GWF formats and the adapter."""
    if format_name == "gwf.lalframe":
        pytest.importorskip("lalframe")
    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)

    from gwexpy.timeseries import TimeSeries, TimeSeriesDict
    from gwpy.testing.utils import TEST_GWF_FILE

    from gwexpy_studio.ops.io import read_data

    direct_dict = TimeSeriesDict.read(TEST_GWF_FILE, format=format_name)
    adapted_dict = read_data("TimeSeriesDict", TEST_GWF_FILE, format=format_name)

    assert type(adapted_dict) is type(direct_dict) is TimeSeriesDict
    assert tuple(adapted_dict) == tuple(direct_dict)
    assert adapted_dict
    channel = next(iter(direct_dict))
    direct_series = TimeSeries.read(TEST_GWF_FILE, channel=channel, format=format_name)
    adapted_series = read_data(
        "TimeSeries",
        TEST_GWF_FILE,
        format=format_name,
        kwargs={"channel": channel},
    )

    assert type(adapted_series) is type(direct_series) is TimeSeries
    _assert_timeseries_equal(adapted_series, direct_series)
    for name in direct_dict:
        _assert_timeseries_equal(adapted_dict[name], direct_dict[name])


def test_ndscope_adapter_matches_native_reader_for_real_fixture(monkeypatch, tmp_path):
    """Read a real NDScope-schema HDF5 file through GWexpy and the adapter."""
    pytest.importorskip("h5py")
    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)

    from gwexpy.timeseries import TimeSeries, TimeSeriesDict

    from gwexpy_studio.ops.io import read_data

    channel = "H1:TEST-CHANNEL"
    source = TimeSeriesDict(
        {
            channel: TimeSeries(
                [0.25, -1.5, 2.0, 0.75],
                t0=1126259462,
                sample_rate=4,
                unit="ct",
            )
        }
    )
    path = tmp_path / "ndscope.h5"
    source.write(str(path), format="hdf.ndscope")

    direct_dict = TimeSeriesDict.read(str(path), format="hdf.ndscope")
    adapted_dict = read_data("TimeSeriesDict", str(path), format="hdf.ndscope")
    direct_series = TimeSeries.read(str(path), channel=channel, format="hdf.ndscope")
    adapted_series = read_data(
        "TimeSeries",
        str(path),
        format="hdf.ndscope",
        kwargs={"channel": channel},
    )

    assert type(adapted_dict) is type(direct_dict) is TimeSeriesDict
    assert tuple(adapted_dict) == tuple(direct_dict) == (channel,)
    assert type(adapted_series) is type(direct_series) is TimeSeries
    _assert_timeseries_equal(adapted_dict[channel], direct_dict[channel])
    _assert_timeseries_equal(adapted_series, direct_series)


@pytest.mark.parametrize(
    "datatype", ["TimeSeries", "TimeSeriesDict", "TimeSeriesMatrix"]
)
def test_diaggui_adapter_matches_native_reader_for_real_fixture(
    monkeypatch, tmp_path, datatype
):
    """Compare real XML parsing through Studio with GWexpy's direct reader."""
    pytest.importorskip(
        "dttxml", reason="GWexpy's native fallback parser does not decode TS products"
    )
    monkeypatch.delenv("GWEXPY_STUDIO_IO_CAPABILITIES", raising=False)

    from gwexpy_studio.ops.io import io_class, read_data
    from tests.support.trial_io_fixtures import write_minimal_diaggui_timeseries

    source = write_minimal_diaggui_timeseries(tmp_path / "measurement.xml")
    native_class = io_class(datatype)
    direct = native_class.read(str(source), format="xml.diaggui", products="TS")
    adapted = read_data(datatype, str(source), format="xml.diaggui")

    assert type(adapted) is type(direct)
    if datatype == "TimeSeriesDict":
        assert direct
        assert set(adapted) == set(direct)
        for name in direct:
            _assert_timeseries_equal(adapted[name], direct[name])
    else:
        assert len(direct) > 0
        _assert_timeseries_equal(adapted, direct)


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
