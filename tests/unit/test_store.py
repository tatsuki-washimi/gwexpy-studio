"""Contract tests for the worker-local scientific object store."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from astropy import units as u

from gwexpy_studio.runtime.store import ObjectStore


def _assert_axes(
    reference: Any,
    expected: dict[str, tuple[float, Any]],
) -> None:
    """Require exact axis records and Astropy units with scale semantics."""
    assert set(reference.axes) == set(expected)
    for axis_name, (expected_value, expected_unit) in expected.items():
        axis = reference.axes[axis_name]
        assert set(axis) == {"value", "unit"}
        assert float(axis["value"]) == pytest.approx(
            expected_value, rel=1e-12, abs=1e-12
        )
        assert u.Unit(axis["unit"]) == expected_unit


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(
            "timeseries",
            id="timeseries",
            marks=pytest.mark.contract("B-029"),
        ),
        pytest.param(
            "frequencyseries",
            id="frequencyseries",
            marks=pytest.mark.contract("B-053"),
        ),
        pytest.param(
            "spectrogram",
            id="spectrogram",
            marks=pytest.mark.contract("B-054"),
        ),
    ],
)
def test_object_store_put_get_list_preserves_identity_and_axes(
    kind: str, timeseries: Any, sine_32hz: Any
) -> None:
    """Store round trips scientific identity, units, and axis metadata."""
    if kind == "timeseries":
        value = timeseries
    elif kind == "frequencyseries":
        value = sine_32hz.asd(fftlength=4.0)
    else:
        import gwexpy

        gwexpy.register_all(include_io=False)
        value = sine_32hz.spectrogram(stride=4.0, fftlength=2.0)

    original = np.array(value.value, copy=True)
    store = ObjectStore()

    reference = store.put(value)

    assert reference.object_id == "obj-1"
    assert reference.kind == type(value).__name__
    assert reference.shape == tuple(value.shape)
    assert reference.dtype == str(value.value.dtype)
    assert u.Unit(reference.unit) == u.Unit(value.unit)
    assert reference.produced_by is None
    assert np.isfinite(np.asarray(value.value)).all()

    if kind == "timeseries":
        assert reference.name == "X1:STUDIO-TEST"
        assert reference.channel == "X1:STUDIO-CHANNEL"
        _assert_axes(
            reference,
            {
                "t0": (1_000_000_000.0, u.s),
                "dt": (1.0 / 256.0, u.s),
            },
        )
    elif kind == "frequencyseries":
        _assert_axes(
            reference,
            {"f0": (0.0, u.Hz), "df": (0.25, u.Hz)},
        )
    else:
        _assert_axes(
            reference,
            {
                "t0": (1_000_000_000.0, u.s),
                "dt": (4.0, u.s),
                "f0": (0.0, u.Hz),
                "df": (0.5, u.Hz),
            },
        )

    assert store.get(reference.object_id) is value
    assert store.list_objects() == (reference,)
    assert np.array_equal(value.value, original)


@pytest.mark.contract("B-030")
def test_object_store_get_reports_missing_object_ids() -> None:
    """A missing object cannot be mistaken for a materialized result."""
    with pytest.raises(KeyError):
        ObjectStore().get("obj-1")


@pytest.mark.contract("B-031")
def test_object_store_delete_reports_missing_object_ids() -> None:
    """Deletion has an explicit missing-object error boundary."""
    with pytest.raises(KeyError):
        ObjectStore().delete("obj-1")


@pytest.mark.contract("B-032")
def test_empty_object_store_lists_no_materialized_references() -> None:
    """A fresh store has a deterministic empty materialization view."""
    assert ObjectStore().list_objects() == ()
