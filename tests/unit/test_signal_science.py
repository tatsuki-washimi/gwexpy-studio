import operator

import numpy as np
import pytest
from astropy import units as u
from gwexpy.frequencyseries import FrequencySeries
from gwexpy.spectrogram import Spectrogram
from gwexpy.timeseries import (
    TimeSeries,
    TimeSeriesDict,
    TimeSeriesList,
    TimeSeriesMatrix,
)

from gwexpy_studio.ops.spec import normalize_params


def science_apply(operation_id, inputs, params):
    from gwexpy_studio.ops.scientific import SCIENTIFIC_REGISTRY

    spec = SCIENTIFIC_REGISTRY[operation_id]
    return spec.apply(inputs, normalize_params(params, spec=spec))


def assert_native_equal(actual, expected):
    assert type(actual) is type(expected)
    np.testing.assert_allclose(actual.value, expected.value, equal_nan=True)
    assert actual.unit == expected.unit
    assert actual.name == expected.name
    for axis in ("times", "frequencies"):
        if hasattr(expected, axis):
            np.testing.assert_array_equal(
                getattr(actual, axis), getattr(expected, axis)
            )


@pytest.mark.parametrize(
    "operation", ["add", "subtract", "multiply", "divide", "power"]
)
@pytest.mark.parametrize("family", ["time", "frequency", "spectrogram"])
def test_leaf_arithmetic_matches_native(operation, family):
    classes = {
        "time": TimeSeries,
        "frequency": FrequencySeries,
        "spectrogram": Spectrogram,
    }
    kwargs = {
        "time": {"t0": 123, "dt": 0.1},
        "frequency": {"f0": 2, "df": 0.5},
        "spectrogram": {"t0": 123, "dt": 0.1, "f0": 2, "df": 0.5},
    }
    shape = (4, 5) if family == "spectrogram" else (20,)
    left = classes[family](
        np.arange(1, 21).reshape(shape), unit="m", name="left", **kwargs[family]
    )
    right = classes[family](np.full(shape, 2), unit="m", name="right", **kwargs[family])
    op = {
        "add": operator.add,
        "subtract": operator.sub,
        "multiply": operator.mul,
        "divide": operator.truediv,
        "power": operator.pow,
    }[operation]
    if operation == "power":
        expected = op(left, 2)
        params = {"operand_mode": "scalar", "scalar": 2}
        inputs = {"self": left}
    else:
        expected = op(left, right)
        params, inputs = {}, {"self": left, "other": right}
    result = science_apply("data." + operation, inputs, params)
    assert_native_equal(result, expected)


@pytest.mark.parametrize("reverse", [False, True])
def test_unitful_scalar_order(reverse):
    left = TimeSeries([1, 2, 3], unit="m", name="a", dt=0.5)
    scalar = 5 * u.cm
    expected = scalar - left if reverse else left - scalar
    result = science_apply(
        "data.subtract",
        {"self": left},
        {
            "operand_mode": "scalar",
            "scalar": {"value": 5, "unit": "cm"},
            "reverse": reverse,
        },
    )
    assert_native_equal(result, expected)


@pytest.mark.parametrize("container", [TimeSeriesList, TimeSeriesDict])
def test_container_arithmetic_pairs_without_concat_or_all_pairs(container):
    leaves = [
        TimeSeries([1.0, 2, 3], unit="m", name="a", dt=0.5),
        TimeSeries([2.0, 3, 4], unit="s", name="b", dt=0.5),
    ]
    left = container(
        {"a": leaves[0], "b": leaves[1]} if container is TimeSeriesDict else leaves
    )
    right = container(
        {"b": leaves[1], "a": leaves[0]} if container is TimeSeriesDict else leaves
    )
    result = science_apply("data.add", {"self": left, "other": right}, {})
    assert type(result) is container
    if container is TimeSeriesDict:
        assert list(result) == ["a", "b"]
    for i, leaf in enumerate(leaves):
        key = ("a", "b")[i] if container is TimeSeriesDict else i
        assert_native_equal(result[key], leaf + leaf)


@pytest.mark.contract("SIG-0050")
def test_matrix_arithmetic_preserves_per_cell_units_and_labels():
    left = TimeSeriesMatrix(
        np.arange(12).reshape(2, 1, 6),
        dt=0.5,
        units=[["m"], ["s"]],
        names=[["a"], ["b"]],
        rows=["r1", "r2"],
        cols=["c"],
    )
    result = science_apply(
        "data.multiply",
        {"self": left},
        {"operand_mode": "scalar", "scalar": {"value": 2, "unit": "s"}},
    )
    assert type(result) is TimeSeriesMatrix
    assert result.row_keys() == left.row_keys()
    assert result.col_keys() == left.col_keys()
    for r in range(2):
        assert_native_equal(result[r, 0], left[r, 0] * (2 * u.s))


@pytest.mark.parametrize(
    "change", ["origin", "cadence", "shape", "kind", "coordinates"]
)
def test_arithmetic_rejects_mismatched_native_axes(change):
    left = TimeSeries([1.0, 2.0, 3.0], t0=10, dt=1, unit="m")
    right = left.copy()
    if change == "origin":
        right.t0 = 11
    elif change == "cadence":
        right.dt = 2
    elif change == "shape":
        right = right[:2]
    elif change == "kind":
        right = FrequencySeries([1.0, 2.0, 3.0], unit="m")
    else:
        right = TimeSeries([1.0, 2.0, 3.0], times=[10, 11, 13], unit="m")
    with pytest.raises(
        (ValueError, TypeError), match="match|same|axis|kind|coordinate"
    ):
        science_apply("data.add", {"self": left, "other": right}, {})


@pytest.mark.contract("SIG-0051")
def test_failed_batch_does_not_mutate_inputs():
    left = TimeSeriesList(
        [TimeSeries([1.0, 2], unit="m"), TimeSeries([3.0, 4], unit="s")]
    )
    right = TimeSeriesList(
        [TimeSeries([1.0, 2], unit="m"), TimeSeries([3.0, 4], unit="kg")]
    )
    snapshots = [x.copy() for x in left + right]
    with pytest.raises(u.UnitConversionError):
        science_apply("data.add", {"self": left, "other": right}, {})
    for actual, snapshot in zip(list(left) + list(right), snapshots, strict=True):
        assert_native_equal(actual, snapshot)


@pytest.mark.contract("SIG-0052")
def test_division_preserves_nonfinite_results_and_warning():
    left = TimeSeries([1.0, 0.0], unit="m")
    right = TimeSeries([0.0, 0.0], unit="s")
    with pytest.warns(RuntimeWarning):
        result = science_apply("data.divide", {"self": left, "other": right}, {})
    assert np.isinf(result.value[0])
    assert np.isnan(result.value[1])


@pytest.mark.contract("SIG-0053")
def test_boolean_parameter_is_strict():
    with pytest.raises((TypeError, ValueError), match="bool"):
        science_apply(
            "data.add",
            {"self": TimeSeries([1.0])},
            {"operand_mode": "scalar", "scalar": 1, "reverse": "false"},
        )
