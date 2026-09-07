"""Worker RPC arrays, native members, and transfer cleanup contracts."""

from __future__ import annotations

import importlib
from multiprocessing import shared_memory

import numpy as np
import pytest

from gwexpy_studio.runtime.store import ObjectStore
from gwexpy_studio.worker.service import DefaultStore
from gwexpy_studio.worker.shm import SharedMemoryDescriptor, attach_block


def _service():
    module = importlib.import_module("gwexpy_studio.worker.signal_service")
    objects, transfers = ObjectStore(), DefaultStore()
    return module.SignalService(objects, transfers), objects, transfers


def _values(raw):
    descriptor = SharedMemoryDescriptor(**{**raw, "shape": tuple(raw["shape"])})
    preview = attach_block(descriptor)
    try:
        return preview.values.copy()
    finally:
        preview.handle.close()


@pytest.mark.contract("SIG-0043")
def test_member_preview_preserves_complex_values_axes_and_store_contents():
    from gwexpy.frequencyseries import FrequencySeries, FrequencySeriesDict

    service, objects, transfers = _service()
    frequency = np.array([1, 3, 8], dtype=np.longdouble) / 11
    original = FrequencySeries(
        [1 + 2j, 3 + 4j, 5 + 6j], frequencies=frequency, unit="V", name="signal"
    )
    ref = objects.put(FrequencySeriesDict({"sensor": original}))
    try:
        page = service.dispatch(
            "list_members", {"object_id": ref.object_id, "limit": 1}
        )
        assert page["members"][0]["selector"] == {"key": "sensor"}
        response = service.dispatch(
            "preview_data", {"object_id": ref.object_id, "selector": {"key": "sensor"}}
        )
        assert response["object_ref"]["kind"] == "FrequencySeries"
        arrays = response["arrays"]
        np.testing.assert_array_equal(_values(arrays["values"]), original.value)
        recovered = _values(arrays["x_coordinates"])
        assert recovered.dtype == frequency.dtype
        np.testing.assert_array_equal(recovered, frequency)
        assert arrays["y_coordinates"] is None
        assert len(objects) == 1
    finally:
        transfers.cleanup()


@pytest.mark.contract("SIG-0044")
def test_partial_preview_allocation_failure_releases_every_new_block(monkeypatch):
    from gwexpy.spectrogram import Spectrogram

    service, objects, transfers = _service()
    module = importlib.import_module("gwexpy_studio.worker.signal_service")
    objects.put(
        Spectrogram(
            np.ones((3, 4)), times=[0.0, 0.3, 1.0], frequencies=[0.0, 0.2, 0.7, 2.0]
        )
    )
    original_create = module.create_block
    names = []

    def fail_second(array):
        if names:
            raise RuntimeError("allocation failed")
        block = original_create(array)
        names.append(block.name)
        return block

    monkeypatch.setattr(module, "create_block", fail_second)
    with pytest.raises(RuntimeError, match="allocation"):
        service.dispatch("preview_data", {"object_id": "obj-1"})
    assert transfers.pending == set()
    for name in names:
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)


@pytest.mark.contract("SIG-0045")
def test_write_requires_explicit_overwrite_confirmation_and_keeps_store_unchanged(
    tmp_path,
):
    from gwexpy.timeseries import TimeSeries

    service, objects, transfers = _service()
    native = TimeSeries([1.0, 2.0, 3.0], dt=1, t0=0, unit="m", name="signal")
    objects.put(native)
    target = tmp_path / "out.h5"
    request = {"target": str(target), "format": "hdf5", "kwargs": {"overwrite": True}}
    service.dispatch("write_data", {"object_id": "obj-1", "request": request})
    before = target.read_bytes()
    with pytest.raises((ValueError, PermissionError), match="confirm"):
        service.dispatch("write_data", {"object_id": "obj-1", "request": request})
    assert target.read_bytes() == before
    assert len(objects) == 1
    assert objects.get("obj-1") is native


@pytest.mark.contract("SIG-0046")
def test_io_inspection_revalidation_detects_file_change(tmp_path):
    service, _, _ = _service()
    path = tmp_path / "source.csv"
    path.write_text("time,value\n0,1\n")
    request = {"datatype": "TimeSeries", "paths": [str(path)], "format": "csv"}
    inspected = service.dispatch("inspect_io", {"request": request})["io_inspection"]
    assert (
        service.dispatch("inspect_io", {"request": request, "expected": inspected})[
            "io_inspection"
        ]
        == inspected
    )
    path.write_text("changed source\n")
    with pytest.raises(ValueError, match="changed"):
        service.dispatch("inspect_io", {"request": request, "expected": inspected})


@pytest.mark.contract("SIG-0047")
def test_recorded_filter_preview_uses_original_coefficients_without_live_inputs():
    service, objects, transfers = _service()
    details = {
        "filter_recipes": [
            {
                "format": "sos",
                "coefficients": [[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]],
                "sample_rate": 128.0,
                "filtfilt": True,
                "operation": "timeseries.lowpass",
                "label": "recorded sensor",
            }
        ]
    }
    try:
        response = service.dispatch(
            "filter_preview",
            {
                "op_name": "timeseries.lowpass",
                "params": {"frequency": 10},
                "inputs": {},
                "recorded_details": details,
                "generation": 7,
            },
        )
        assert response["generation"] == 7
        assert response["members"][0]["label"] == "recorded sensor"
        values = _values(response["members"][0]["arrays"]["values"])
        assert np.iscomplexobj(values)
        np.testing.assert_allclose(
            values, np.ones(values.shape, dtype=complex), rtol=0, atol=1e-15
        )
        assert len(objects) == 0
    finally:
        transfers.cleanup()


@pytest.mark.contract("SIG-0048")
def test_preview_coordinates_use_canonical_units_without_lowering_precision():
    from astropy import units as u
    from gwexpy.frequencyseries import FrequencySeries

    service, objects, transfers = _service()
    frequency = np.array([1, 3, 8], dtype=np.longdouble) * u.kHz
    source = FrequencySeries([1.0, 2.0, 3.0], frequencies=frequency)
    objects.put(source)
    try:
        response = service.dispatch("preview_data", {"object_id": "obj-1"})
        descriptor = response["arrays"]["x_coordinates"]
        assert descriptor["unit"] == "Hz"
        assert _values(descriptor).dtype == np.dtype(np.longdouble)
        np.testing.assert_array_equal(
            _values(descriptor), source.frequencies.to_value("Hz")
        )
        assert objects.get("obj-1") is source
    finally:
        transfers.cleanup()


@pytest.mark.contract("SIG-0049")
def test_write_accepts_single_path_from_the_shared_io_panel_request(tmp_path):
    from gwexpy.timeseries import TimeSeries

    service, objects, _ = _service()
    objects.put(TimeSeries([1.0, 2.0, 3.0], dt=1, name="signal"))
    path = tmp_path / "written.h5"
    response = service.dispatch(
        "write_data",
        {
            "object_id": "obj-1",
            "request": {
                "paths": [str(path)],
                "format": "hdf5",
                "args": [],
                "kwargs": {},
            },
        },
    )
    assert response["target"] == str(path)
    assert path.is_file()
