"""Unit tests for PreviewData model and axis boundary validation."""

from __future__ import annotations

import numpy as np
import pytest

from gwexpy_studio.application.preview import PreviewData
from gwexpy_studio.domain.model import DataObjectRef


def _make_ref(
    *,
    object_id: str = "obj-1",
    kind: str = "TimeSeries",
    shape: tuple[int, ...] = (100,),
    dtype: str = "float64",
    unit: str = "m",
    axes: dict | None = None,
) -> DataObjectRef:
    default_axes = {
        "t0": {"value": 0.0, "unit": "s"},
        "dt": {"value": 0.01, "unit": "s"},
    }
    return DataObjectRef(
        object_id=object_id,
        kind=kind,
        shape=shape,
        dtype=dtype,
        unit=unit,
        axes=default_axes if axes is None else axes,
    )


@pytest.mark.contract("C-APP-014")
def test_preview_data_valid_timeseries() -> None:
    """Valid TimeSeries PreviewData creates successfully."""
    ref = _make_ref()
    arr = np.zeros(100, dtype=np.float64)
    preview = PreviewData(ref=ref, values=arr, unit="m")
    assert preview.ref == ref
    assert np.array_equal(preview.values, arr)
    assert preview.unit == "m"


@pytest.mark.contract("C-APP-015")
def test_preview_data_valid_frequencyseries() -> None:
    """Valid FrequencySeries PreviewData creates successfully."""
    axes = {
        "f0": {"value": 0.0, "unit": "Hz"},
        "df": {"value": 1.0, "unit": "Hz"},
    }
    ref = _make_ref(kind="FrequencySeries", axes=axes)
    arr = np.ones(100, dtype=np.float64)
    preview = PreviewData(ref=ref, values=arr, unit="m")
    assert preview.ref.kind == "FrequencySeries"


@pytest.mark.contract("C-APP-016")
def test_preview_data_valid_spectrogram() -> None:
    """Valid Spectrogram PreviewData creates successfully."""
    axes = {
        "t0": {"value": 0.0, "unit": "s"},
        "dt": {"value": 0.5, "unit": "s"},
        "f0": {"value": 10.0, "unit": "Hz"},
        "df": {"value": 2.0, "unit": "Hz"},
    }
    ref = _make_ref(kind="Spectrogram", shape=(10, 20), axes=axes)
    arr = np.zeros((10, 20), dtype=np.float64)
    preview = PreviewData(ref=ref, values=arr, unit="m")
    assert preview.ref.kind == "Spectrogram"


@pytest.mark.contract("C-APP-017")
def test_preview_data_rejects_shape_mismatch() -> None:
    """Shape mismatch between ref and values is rejected."""
    ref = _make_ref(shape=(100,))
    arr = np.zeros(50, dtype=np.float64)
    with pytest.raises(ValueError, match="shape"):
        PreviewData(ref=ref, values=arr, unit="m")


@pytest.mark.contract("C-APP-018")
def test_preview_data_rejects_dtype_mismatch() -> None:
    """Dtype mismatch between ref and values is rejected."""
    ref = _make_ref(dtype="float64")
    arr = np.zeros(100, dtype=np.float32)
    with pytest.raises(ValueError, match="dtype"):
        PreviewData(ref=ref, values=arr, unit="m")


@pytest.mark.contract("C-APP-019")
def test_preview_data_rejects_unit_mismatch() -> None:
    """Unit mismatch between ref and values is rejected."""
    ref = _make_ref(unit="m")
    arr = np.zeros(100, dtype=np.float64)
    with pytest.raises(ValueError, match="unit"):
        PreviewData(ref=ref, values=arr, unit="V")


@pytest.mark.contract("C-APP-020")
def test_preview_data_rejects_unsupported_kind() -> None:
    """Unsupported ref kind is rejected."""
    ref = _make_ref(kind="UnsupportedKind")
    arr = np.zeros(100, dtype=np.float64)
    with pytest.raises(ValueError, match="Unsupported preview kind"):
        PreviewData(ref=ref, values=arr, unit="m")


@pytest.mark.contract("C-APP-021")
def test_preview_data_rejects_missing_axis() -> None:
    """Missing required axis is rejected."""
    ref = _make_ref(axes={"t0": {"value": 0.0, "unit": "s"}})
    arr = np.zeros(100, dtype=np.float64)
    with pytest.raises(ValueError, match="Missing required axis 'dt'"):
        PreviewData(ref=ref, values=arr, unit="m")


@pytest.mark.contract("C-APP-022")
def test_preview_data_rejects_non_positive_step() -> None:
    """Non-positive dt or df is rejected."""
    axes = {
        "t0": {"value": 0.0, "unit": "s"},
        "dt": {"value": 0.0, "unit": "s"},
    }
    ref = _make_ref(axes=axes)
    arr = np.zeros(100, dtype=np.float64)
    with pytest.raises(ValueError, match="strictly positive"):
        PreviewData(ref=ref, values=arr, unit="m")


@pytest.mark.contract("C-APP-023")
def test_preview_data_rejects_wrong_axis_unit() -> None:
    """Wrong axis unit is rejected."""
    axes = {
        "t0": {"value": 0.0, "unit": "Hz"},  # should be 's'
        "dt": {"value": 0.01, "unit": "s"},
    }
    ref = _make_ref(axes=axes)
    arr = np.zeros(100, dtype=np.float64)
    with pytest.raises(ValueError, match="Axis 't0' unit must be 's'"):
        PreviewData(ref=ref, values=arr, unit="m")


@pytest.mark.contract("C-APP-024")
def test_preview_data_rejects_boolean_or_non_finite_axis() -> None:
    """Boolean or non-finite axis value is rejected."""
    axes = {
        "t0": {"value": True, "unit": "s"},
        "dt": {"value": 0.01, "unit": "s"},
    }
    ref = _make_ref(axes=axes)
    arr = np.zeros(100, dtype=np.float64)
    with pytest.raises(ValueError, match="non-bool"):
        PreviewData(ref=ref, values=arr, unit="m")

    axes_inf = {
        "t0": {"value": float("inf"), "unit": "s"},
        "dt": {"value": 0.01, "unit": "s"},
    }
    ref_inf = _make_ref(axes=axes_inf)
    with pytest.raises(ValueError, match="finite"):
        PreviewData(ref=ref_inf, values=arr, unit="m")
