import ast
import importlib
import inspect
import json
import operator

import numpy as np
import pytest
from astropy import units as u
from gwexpy.timeseries import (
    TimeSeries,
    TimeSeriesDict,
    TimeSeriesList,
    TimeSeriesMatrix,
)

from gwexpy_studio.ops.spec import normalize_params
from tests.unit.test_signal_science import assert_native_equal, science_apply


@pytest.mark.parametrize(
    "family,suffix",
    [
        (family, suffix)
        for family in ("TimeSeries", "FrequencySeries", "Spectrogram")
        for suffix in ("Dict", "List", "Matrix")
    ]
    + [("Spectrogram", "Batch")],
)
@pytest.mark.parametrize(
    "operation", ["add", "subtract", "multiply", "divide", "power"]
)
def test_every_container_family_uses_leaf_arithmetic(family, suffix, operation):
    module = importlib.import_module("gwexpy." + family.lower())
    cls = getattr(module, family)
    kwargs = {
        "TimeSeries": {"t0": 5, "dt": 0.5},
        "FrequencySeries": {"f0": 2, "df": 0.5},
        "Spectrogram": {"t0": 5, "dt": 0.5, "f0": 2, "df": 0.5},
    }[family]
    shape = (6, 7) if family == "Spectrogram" else (42,)
    leaves = [
        cls(np.arange(1.0, 43.0).reshape(shape), unit="m", name="A", **kwargs),
        cls(np.arange(2.0, 44.0).reshape(shape), unit="s", name="B", **kwargs),
    ]
    if suffix == "Dict":
        container = getattr(module, family + suffix)({"A": leaves[0], "B": leaves[1]})
    elif suffix == "List":
        container = getattr(module, family + suffix)(leaves)
    else:
        values = np.stack([leaf.value for leaf in leaves])
        if suffix == "Matrix":
            values = values[:, None]
        if family == "Spectrogram":
            from gwexpy.types.metadata import MetaDataMatrix

            kwargs = {"times": leaves[0].times, "frequencies": leaves[0].frequencies}
            kwargs["meta"] = MetaDataMatrix(
                [[{"unit": "m", "name": "A"}], [{"unit": "s", "name": "B"}]]
            )
        container = getattr(module, family + "Matrix")(
            values,
            units=[["m"], ["s"]],
            names=[["A"], ["B"]],
            rows=["rA", "rB"],
            cols=["c"],
            **kwargs,
        )
    op = {
        "add": operator.add,
        "subtract": operator.sub,
        "multiply": operator.mul,
        "divide": operator.truediv,
        "power": operator.pow,
    }[operation]
    if operation == "power":
        result = science_apply(
            "data.power", {"self": container}, {"operand_mode": "scalar", "scalar": 2}
        )
    else:
        result = science_apply(
            "data." + operation, {"self": container, "other": container}, {}
        )
    assert type(result) is type(container)
    for index, leaf in enumerate(leaves):
        if suffix == "Dict":
            actual = result[("A", "B")[index]]
        elif suffix == "List":
            actual = result[index]
        else:
            from gwexpy_studio.ops.native_containers import science_members

            actual = science_members(result)[index][1]
        assert_native_equal(actual, op(leaf, 2 if operation == "power" else leaf))


@pytest.mark.parametrize("container", ["dict", "list", "matrix"])
def test_container_key_length_and_matrix_label_mismatches(container):
    leaf = TimeSeries([1.0, 2.0], unit="m")
    if container == "dict":
        left, right = TimeSeriesDict(a=leaf), TimeSeriesDict(b=leaf)
    elif container == "list":
        left, right = TimeSeriesList([leaf]), TimeSeriesList([leaf, leaf])
    else:
        left = TimeSeriesMatrix(np.ones((1, 1, 5)), dt=1, rows=["A"], cols=["x"])
        right = TimeSeriesMatrix(np.ones((1, 1, 5)), dt=1, rows=["B"], cols=["x"])
    with pytest.raises(ValueError, match="same|matching"):
        science_apply("data.add", {"self": left, "other": right}, {})


@pytest.mark.contract("SIG-0054")
def test_canonical_time_units_match_without_alignment():
    first = TimeSeries([1.0, 2.0, 3.0], times=np.array([1.0, 2.0, 3.0]) * u.s, unit="m")
    second = TimeSeries(
        [3.0, 4.0, 5.0], times=np.array([1000.0, 2000.0, 3000.0]) * u.ms, unit="cm"
    )
    result = science_apply("data.add", {"self": first, "other": second}, {})
    assert_native_equal(result, first + second)


@pytest.mark.parametrize(
    "params,inputs",
    [
        ({"operand_mode": "scalar"}, {"self": TimeSeries([1.0])}),
        ({"operand_mode": "scalar", "scalar": [1, 2]}, {"self": TimeSeries([1.0])}),
        ({"operand_mode": "scalar", "scalar": True}, {"self": TimeSeries([1.0])}),
        ({"operand_mode": "scalar", "scalar": "x"}, {"self": TimeSeries([1.0])}),
        (
            {"operand_mode": "scalar", "scalar": {"wrong": 1}},
            {"self": TimeSeries([1.0])},
        ),
        (
            {"operand_mode": "scalar", "scalar": 2},
            {"self": TimeSeries([1.0]), "other": TimeSeries([1.0])},
        ),
        ({}, {"self": TimeSeries([1.0])}),
        ({"scalar": 2}, {"self": TimeSeries([1.0]), "other": TimeSeries([1.0])}),
    ],
)
def test_invalid_scalar_contract_fails_explicitly(params, inputs):
    with pytest.raises((TypeError, ValueError)):
        science_apply("data.add", inputs, params)


@pytest.mark.parametrize(
    "scalar", [2 * u.m, 2 + 3j, {"__type__": "float", "value": "nan"}]
)
def test_literal_normalization_is_json_safe_and_export_matches_apply(scalar):
    from gwexpy_studio.ops.scientific import SCIENTIFIC_REGISTRY

    spec = SCIENTIFIC_REGISTRY["data.multiply"]
    params = normalize_params({"operand_mode": "scalar", "scalar": scalar}, spec=spec)
    json.dumps(params, allow_nan=False)
    series = TimeSeries([1.0, 2.0], unit="m")
    source = spec.emit(
        {"variable": "output", "inputs": {"self": "series"}, "params": params}
    )
    namespace = {"series": series}
    from gwexpy_studio.ops.native_arithmetic import science_arithmetic

    namespace["science_arithmetic"] = science_arithmetic
    exec(source, namespace)
    assert_native_equal(namespace["output"], spec.apply({"self": series}, params))


@pytest.mark.contract("SIG-0055")
def test_recorded_filter_coefficients_replay_independent_of_design(monkeypatch):
    from gwexpy_studio.ops.native_filters import science_filter_from_recipes

    series = TimeSeries(np.random.default_rng(1).normal(size=256), sample_rate=128)
    result = science_apply("timeseries.lowpass", {"self": series}, {"frequency": 15})

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must use recorded coefficients")

    monkeypatch.setattr("gwexpy.signal.filter_design.lowpass", forbidden)
    replay = science_filter_from_recipes({"self": series}, result.details)
    assert_native_equal(replay, result.value)
    shifted = series.resample(64)
    with pytest.raises(ValueError, match="sample rate"):
        science_filter_from_recipes({"self": shifted}, result.details)


@pytest.mark.contract("SIG-0056")
def test_standalone_helpers_embed_without_studio_dependency():
    modules = [
        "values",
        "native_containers",
        "native_arithmetic",
        "native_results",
        "native_filters",
        "native_spectral",
    ]
    bodies = []
    for module in modules:
        parsed = ast.parse(
            inspect.getsource(importlib.import_module("gwexpy_studio.ops." + module))
        )
        bodies.extend(
            node
            for node in parsed.body
            if not isinstance(node, ast.ImportFrom)
            or node.level == 0
            and node.module != "__future__"
        )
    source = "from __future__ import annotations\n" + ast.unparse(
        ast.Module(body=bodies, type_ignores=[])
    )
    assert "gwexpy_studio" not in source
    namespace = {"__name__": "__main__"}
    exec(source, namespace)
    result = namespace["science_arithmetic"](
        "multiply",
        {"self": TimeSeries([1.0, 2.0])},
        {"operand_mode": "scalar", "scalar": 3},
    )
    np.testing.assert_array_equal(result.value, [3, 6])


@pytest.mark.contract("SIG-0057")
def test_spectrogram_matrix_preserves_independent_attributes():
    from gwexpy.spectrogram import SpectrogramMatrix

    source = SpectrogramMatrix(
        np.ones((2, 1, 3, 4)), times=np.arange(3) * u.s, frequencies=np.arange(4) * u.Hz
    )
    source.attrs = {"instrument": {"calibration": "v1"}}
    result = science_apply(
        "data.multiply", {"self": source}, {"operand_mode": "scalar", "scalar": 2}
    )
    assert result.attrs == source.attrs
    result.attrs["instrument"]["calibration"] = "v2"
    assert source.attrs["instrument"]["calibration"] == "v1"


@pytest.mark.contract("SIG-0058")
def test_frequency_matrix_leaf_preserves_epoch():
    from gwexpy.frequencyseries import FrequencySeriesMatrix

    from gwexpy_studio.ops.native_containers import science_members

    source = FrequencySeriesMatrix(np.ones((1, 1, 5)), df=1, epoch=1234567890)
    leaf = science_members(source)[0][1]
    assert leaf.epoch is not None
    assert float(leaf.epoch.gps) == 1234567890


@pytest.mark.contract("SIG-0059")
def test_filter_emitter_uses_recorded_recipe():
    from gwexpy_studio.ops.native_filters import science_filter_from_recipes
    from gwexpy_studio.ops.scientific import SCIENTIFIC_REGISTRY

    series = TimeSeries(np.random.default_rng(1).normal(size=256), sample_rate=128)
    spec = SCIENTIFIC_REGISTRY["timeseries.lowpass"]
    result = spec.apply({"self": series}, {"frequency": 15})
    emitted = spec.emit(
        {
            "variable": "output",
            "inputs": {"self": "series"},
            "params": {"frequency": 99},
            "details": result.details,
        }
    )
    namespace = {
        "series": series,
        "science_filter_from_recipes": science_filter_from_recipes,
    }
    exec(emitted, namespace)
    assert_native_equal(namespace["output"], result.value)
