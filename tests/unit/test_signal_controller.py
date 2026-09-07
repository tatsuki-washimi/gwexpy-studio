"""Application semantics for native operations and external write records."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from gwexpy_studio.application.alpha import AlphaController
from gwexpy_studio.domain.model import DataObjectRef, ExecutionRecord
from gwexpy_studio.errors import OperationError


def controller(kind="TimeSeries"):
    app = AlphaController(session=SimpleNamespace(client=None))
    app.project.objects = (
        DataObjectRef(
            object_id="obj-0001", kind=kind, shape=(8,), dtype="float64", unit="m"
        ),
    )

    def replay(*, targets):
        op = app.project.graph.producer_of(targets[0])
        ref = replace(
            app.project.objects[0], object_id=targets[0], produced_by=op.op_id
        )
        app.project.objects += (ref,)
        app.project.executions += (
            ExecutionRecord(
                execution_id=op.op_id,
                op_id=op.op_id,
                started_at="now",
                duration_s=0,
                status="succeeded",
            ),
        )

    app.session.replay = replay
    return app


@pytest.mark.contract("SIG-0011")
def test_native_kind_rejection_precedes_graph_mutation():
    app = controller("FrequencySeries")
    with pytest.raises(OperationError) as error:
        app.apply_multi("timeseries.psd", {"self": {"object_id": "obj-0001"}}, {})
    assert error.value.code == "invalid_input_kind"
    assert not app.project.graph.operations


@pytest.mark.contract("SIG-0012")
def test_optional_operand_is_preserved_and_bad_roles_rejected():
    app = controller()
    app.apply_multi(
        "data.add",
        {"self": {"object_id": "obj-0001"}, "other": {"object_id": "obj-0001"}},
        {},
    )
    assert set(app.project.graph.operations[-1].inputs) == {"self", "other"}
    count = len(app.project.graph.operations)
    with pytest.raises(OperationError):
        app.apply_multi("data.add", {"wrong": {"object_id": "obj-0001"}}, {})
    assert len(app.project.graph.operations) == count


@pytest.mark.contract("SIG-0013")
def test_member_extract_is_recorded_only_when_used():
    app = controller("TimeSeriesDict")
    app.apply_multi(
        "data.multiply",
        {"self": {"object_id": "obj-0001", "selector": {"key": "x"}}},
        {"operand_mode": "scalar", "scalar": 2},
    )
    assert [op.operation_id for op in app.project.graph.operations] == [
        "data.extract",
        "data.multiply",
    ]
    assert (
        app.project.graph.operations[-1].inputs["self"]
        == app.project.graph.operations[0].outputs[0]
    )


@pytest.mark.contract("SIG-0014")
def test_write_requires_overwrite_confirmation_and_records_failure(tmp_path):
    app = controller()
    target = tmp_path / "結果.h5"
    target.write_text("original")
    request = {
        "datatype": "TimeSeries",
        "paths": [str(target)],
        "format": "hdf5",
        "args": [],
        "kwargs": {"overwrite": True},
    }
    with pytest.raises(OperationError, match="Overwrite"):
        app.write_data("obj-0001", request)
    assert not app.project.activities

    def fail(*args, **kwargs):
        raise OperationError("writer failed", code="operation_failed")

    app._signal_request = fail
    with pytest.raises(OperationError, match="incomplete"):
        app.write_data("obj-0001", {**request, "overwrite_confirmed": True})
    assert app.project.activities[-1].status == "failed"
    assert not app.project.graph.operations
    assert target.read_text() == "original"


@pytest.mark.contract("SIG-0068")
def test_native_spectrogram_grid_error_is_preserved_and_can_be_corrected():
    import numpy as np
    from gwexpy.timeseries import TimeSeries

    from gwexpy_studio.ops.native_selection import native_legacy

    original = TimeSeries(
        np.random.default_rng(0).normal(size=1000), times=np.arange(1000) * 0.001
    )
    selected = original.crop(0.1, 0.9).detrend("linear")
    saved = selected.value.copy()
    try:
        expected = selected.spectrogram(0.1, fftlength=0.1)
    except ValueError as native_error:
        with pytest.raises(ValueError, match=str(native_error)):
            native_legacy(
                "spectrogram", {"self": selected}, {"stride": 0.1, "fftlength": 0.1}
            )
    else:
        actual = native_legacy(
            "spectrogram", {"self": selected}, {"stride": 0.1, "fftlength": 0.1}
        )
        np.testing.assert_array_equal(actual.value, expected.value)
    corrected = native_legacy(
        "spectrogram", {"self": selected}, {"stride": 0.2, "fftlength": 0.2}
    )
    np.testing.assert_array_equal(
        corrected.value, selected.spectrogram(0.2, fftlength=0.2).value
    )
    np.testing.assert_array_equal(selected.value, saved)
