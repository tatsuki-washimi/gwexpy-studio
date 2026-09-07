"""Failure cleanup and stable member identity at the application preview boundary."""

from types import SimpleNamespace

import numpy as np
import pytest

from gwexpy_studio.application.alpha import AlphaController
from gwexpy_studio.application.preview import PreviewData
from gwexpy_studio.application.signal_preview import default_native_plot
from gwexpy_studio.domain.model import DataObjectRef
from gwexpy_studio.domain.project_v2 import record_dict
from gwexpy_studio.errors import OperationError


def bundle():
    ref = DataObjectRef(
        object_id="obj-1", kind="TimeSeries", shape=(2,), dtype="float64", unit="m"
    )
    return {
        "object_ref": record_dict(ref),
        "arrays": {
            key: {
                "name": key,
                "dtype": "float64",
                "shape": [2],
                "nbytes": 16,
                "order": "C",
                "unit": unit,
            }
            for key, unit in (("values", "m"), ("x_coordinates", "s"))
        },
    }


@pytest.mark.contract("SIG-0069")
def test_failed_preview_attach_releases_every_announced_buffer(monkeypatch):
    released, closed = [], []
    client = SimpleNamespace(release_array=released.append)
    session = SimpleNamespace(client=client, close=lambda: closed.append(True))
    app = AlphaController(session=session)

    def fail(*args):
        raise ValueError("attachment failed")

    monkeypatch.setattr("gwexpy_studio.application.signal_preview.attach_block", fail)
    with pytest.raises(ValueError, match="attachment"):
        app._detach_bundles([bundle()])
    assert released == ["values", "x_coordinates"]
    assert not closed


@pytest.mark.contract("SIG-0070")
def test_release_failure_closes_worker_and_keeps_primary_error(monkeypatch):
    released, closed = [], []

    def release(name):
        released.append(name)
        raise RuntimeError("release failure")

    def close():
        closed.append(True)
        raise RuntimeError("close failure")

    app = AlphaController(
        session=SimpleNamespace(
            client=SimpleNamespace(release_array=release), close=close
        )
    )
    monkeypatch.setattr(
        "gwexpy_studio.application.signal_preview.attach_block",
        lambda descriptor: (_ for _ in ()).throw(ValueError("copy unavailable")),
    )
    with pytest.raises(ValueError, match="copy unavailable") as error:
        app._detach_bundles([bundle()])
    assert any("release" in note for note in error.value.__notes__)
    assert released == ["values", "x_coordinates"]
    assert closed == [True]


@pytest.mark.contract("SIG-0071")
def test_malformed_preview_descriptor_closes_owner_before_consumption():
    closed = []
    app = AlphaController(
        session=SimpleNamespace(client=object(), close=lambda: closed.append(True))
    )
    malformed = bundle()
    del malformed["arrays"]["values"]["dtype"]
    with pytest.raises(TypeError):
        app._detach_bundles([malformed])
    assert closed == [True]


@pytest.mark.contract("SIG-0081")
def test_transferred_unit_must_match_object_metadata_after_cleanup(monkeypatch):
    released = []
    app = AlphaController(
        session=SimpleNamespace(client=SimpleNamespace(release_array=released.append))
    )
    monkeypatch.setattr(
        "gwexpy_studio.application.signal_preview.attach_block",
        lambda descriptor: SimpleNamespace(
            values=np.array([1.0, 2.0]), handle=SimpleNamespace(close=lambda: None)
        ),
    )
    malformed = bundle()
    malformed["arrays"]["values"]["unit"] = "s"
    with pytest.raises(ValueError, match="unit"):
        app._detach_bundles([malformed])
    assert released == ["values", "x_coordinates"]


@pytest.mark.contract("SIG-0072")
def test_member_plot_identity_distinguishes_integer_and_text_keys():
    specs = []
    for key in (1, "1"):
        ref = DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(2,),
            dtype="float64",
            unit="m",
            metadata={"member_selector": {"key": key}},
        )
        specs.append(
            default_native_plot(PreviewData(ref, np.ones(2), "m", np.array([1.0, 2.0])))
        )
    assert specs[0].plot_id != specs[1].plot_id
    assert specs[0].selectors == {"obj-1": {"key": 1}}
    assert specs[1].selectors == {"obj-1": {"key": "1"}}


@pytest.mark.contract("SIG-0073")
def test_filter_preview_rejects_invalid_kinds_and_missing_recorded_recipe():
    app = AlphaController(session=SimpleNamespace(client=None))
    app.project.objects = (
        DataObjectRef(
            object_id="obj-1",
            kind="FrequencySeries",
            shape=(2,),
            dtype="complex128",
            unit="",
        ),
    )
    with pytest.raises(OperationError, match="supported filter"):
        app.filter_preview("data.add", {}, {})
    with pytest.raises(OperationError, match="time series"):
        app.filter_preview(
            "timeseries.lowpass", {"self": {"object_id": "obj-1"}}, {"frequency": 1}
        )
    with pytest.raises(OperationError, match="recorded filter"):
        app.filter_preview(
            "timeseries.lowpass", {}, {"frequency": 1}, recorded_object_id="obj-1"
        )
