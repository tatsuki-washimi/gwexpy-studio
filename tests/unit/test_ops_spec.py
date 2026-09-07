"""Contract tests for curated operation specifications and normalization."""

from __future__ import annotations

import ast

import pytest
from astropy import units as u

from gwexpy_studio.ops.spec import (
    OperationSpec,
    ParamSpec,
    normalize_params,
    quantity_to_canonical_float,
    render_param,
)
from gwexpy_studio.ops.timeseries import REGISTRY, SUPPORTED_FORMATS


@pytest.mark.contract("B-001")
def test_curated_registry_contains_only_approved_operations() -> None:
    """The registry is the exact five-operation scientific boundary."""
    assert tuple(REGISTRY) == (
        "timeseries.read",
        "timeseries.crop",
        "timeseries.detrend",
        "timeseries.asd",
        "timeseries.spectrogram",
    )
    assert SUPPORTED_FORMATS == ("hdf5", "csv_enhanced")
    assert all(spec.schema_version == 1 for spec in REGISTRY.values())


@pytest.mark.contract("B-002")
def test_read_requires_an_explicit_approved_format() -> None:
    """Read exposes only explicit hdf5/csv_enhanced format selection."""
    params = {param.name: param for param in REGISTRY["timeseries.read"].params}

    assert params["source"].required
    assert params["format"].required
    assert params["format"].choices == SUPPORTED_FORMATS
    assert not params["name"].required
    assert "nproc" not in params


@pytest.mark.contract("B-003")
def test_unknown_operation_is_not_resolvable_from_the_curated_registry() -> None:
    """Unknown operation IDs do not silently select an available operation."""
    assert REGISTRY.get("timeseries.unknown") is None


@pytest.mark.contract("B-004")
def test_unknown_operation_schema_is_rejected_by_the_normalizer() -> None:
    """Only the declared operation schema version may reach execution."""
    invalid_spec = OperationSpec(
        operation_id="timeseries.asd",
        schema_version=99,
        input_roles=("self",),
        result_kind="FrequencySeries",
        params=(),
        apply=lambda _inputs, _kwargs: None,
        emit=lambda _context: "",
    )

    with pytest.raises((TypeError, ValueError)):
        normalize_params({}, spec=invalid_spec)


@pytest.mark.contract("B-005")
def test_quantity_to_canonical_float_preserves_astropy_unit_semantics() -> None:
    """Saved quantity values are converted to one canonical float for calls."""
    canonical = quantity_to_canonical_float(4000 * u.ms, canonical_unit="s")

    assert canonical == pytest.approx(4.0, rel=1e-12, abs=1e-12)
    assert isinstance(canonical, float)


@pytest.mark.contract("B-006")
def test_render_param_uses_canonical_literals_and_escapes_strings() -> None:
    """The shared renderer emits parseable literals without Studio imports."""
    quantity_spec = REGISTRY["timeseries.asd"].params[0]
    rendered_quantity = render_param("fftlength", 4.0, spec=quantity_spec)
    string_spec = ParamSpec(name="name", kind="str")
    rendered_string = render_param("name", "X1:'quoted'\nchannel", spec=string_spec)

    quantity_statement = ast.parse(f"call({rendered_quantity})").body[0]
    assert isinstance(quantity_statement, ast.Expr)
    quantity_call = quantity_statement.value
    assert isinstance(quantity_call, ast.Call)
    quantity_keyword = next(
        keyword for keyword in quantity_call.keywords if keyword.arg == "fftlength"
    )
    assert ast.literal_eval(quantity_keyword.value) == 4.0
    assert "gwexpy_studio" not in rendered_quantity
    string_statement = ast.parse(f"call({rendered_string})").body[0]
    assert isinstance(string_statement, ast.Expr)
    string_call = string_statement.value
    assert isinstance(string_call, ast.Call)
    assert len(string_call.keywords) == 1
    assert string_call.keywords[0].arg == "name"
    assert ast.literal_eval(string_call.keywords[0].value) == "X1:'quoted'\nchannel"


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(
            {"fftlength": 4.0, "mystery": 1},
            id="unknown-param",
            marks=pytest.mark.contract("B-007"),
        ),
        pytest.param(
            {"fftlength": 4.0, "nproc": 2},
            id="nproc",
            marks=pytest.mark.contract("B-008"),
        ),
        pytest.param(
            {"fftlength": True},
            id="bool-as-int",
            marks=pytest.mark.contract("B-009"),
        ),
        pytest.param(
            {"fftlength": float("nan")},
            id="nan",
            marks=pytest.mark.contract("B-010"),
        ),
        pytest.param(
            {"fftlength": float("inf")},
            id="infinity",
            marks=pytest.mark.contract("B-011"),
        ),
        pytest.param(
            {"fftlength": 4 * u.m},
            id="incompatible-unit",
            marks=pytest.mark.contract("B-012"),
        ),
    ],
)
def test_normalize_params_rejects_invalid_param_values(
    params: dict[str, object],
) -> None:
    """Every invalid parameter shape is rejected before a worker call."""
    with pytest.raises((TypeError, ValueError)):
        normalize_params(params, spec=REGISTRY["timeseries.asd"])


@pytest.mark.contract("B-013")
def test_normalize_params_preserves_saved_quantity_and_returns_call_float() -> None:
    """Persistence shape remains a mapping while calls receive canonical seconds."""
    saved_quantity = {"value": 4000.0, "unit": "ms"}
    params = {"fftlength": saved_quantity}

    normalized = normalize_params(params, spec=REGISTRY["timeseries.asd"])

    assert params["fftlength"] == saved_quantity
    assert normalized["fftlength"] == pytest.approx(4.0, rel=1e-12, abs=1e-12)


@pytest.mark.contract("B-079")
def test_normalize_params_rejects_invalid_crop_bounds() -> None:
    """Crop parameter normalization rejects start >= end bounds."""
    for start, end in [(10.0, 5.0), (5.0, 5.0)]:
        with pytest.raises(ValueError):
            normalize_params(
                {"start": start, "end": end}, spec=REGISTRY["timeseries.crop"]
            )
