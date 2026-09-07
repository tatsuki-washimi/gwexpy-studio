"""Contract tests for in-process execution and failure provenance."""

from __future__ import annotations

import warnings
from collections.abc import Callable, Mapping
from typing import Any, cast

import numpy as np
import pytest
from astropy import units as u

from gwexpy_studio.domain.model import DataObjectRef, Operation
from gwexpy_studio.errors import OperationError
from gwexpy_studio.ops.spec import OperationSpec, ParamSpec
from gwexpy_studio.runtime.executor import execute_operation
from gwexpy_studio.runtime.store import ObjectStore


class _RecordingStore(ObjectStore):
    """Small injected store that exposes executor materialization evidence."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = dict(values)
        self.put_values: list[Any] = []

    def get(self, object_id: str) -> Any:
        return self.values[object_id]

    def put(self, value: Any) -> DataObjectRef:
        self.put_values.append(value)
        object_id = f"obj-{len(self.put_values) + 1}"
        values = np.asarray(value.value)
        reference = DataObjectRef(
            object_id=object_id,
            kind=cast(Any, type(value).__name__),
            shape=tuple(values.shape),
            dtype=str(values.dtype),
            unit=str(value.unit),
            name=getattr(value, "name", None),
            channel=str(getattr(value, "channel", "")),
        )
        self.values[object_id] = value
        return reference


def _registry_for(
    apply: Callable[[Mapping[str, Any], Mapping[str, Any]], Any],
) -> Mapping[str, OperationSpec]:
    """Inject one operation adapter while exercising the real executor boundary."""
    return _registry_for_operation(apply)


def _registry_for_operation(
    apply: Callable[[Mapping[str, Any], Mapping[str, Any]], Any],
    *,
    operation_id: str = "timeseries.crop",
    result_kind: str = "TimeSeries",
    params: tuple[ParamSpec, ...] | None = None,
) -> Mapping[str, OperationSpec]:
    """Build an injected typed spec with the parameters execution must normalize."""
    declared_params = params or (
        ParamSpec(name="start", kind="gps"),
        ParamSpec(name="end", kind="gps"),
    )
    return {
        operation_id: OperationSpec(
            operation_id=operation_id,
            schema_version=1,
            input_roles=("self",),
            result_kind=result_kind,
            params=declared_params,
            apply=apply,
            emit=lambda _context: "",
        )
    }


def _crop_operation() -> Operation:
    """Return one stable operation identity for executor contracts."""
    return Operation(
        op_id="op-1",
        operation_id="timeseries.crop",
        operation_schema=1,
        inputs={"self": "obj-1"},
        params={"start": 1_000_000_001.0, "end": 1_000_000_010.0},
        outputs=("obj-2",),
    )


@pytest.mark.contract("B-033")
def test_executor_materializes_result_and_preserves_input(
    timeseries: Any,
) -> None:
    """Execution resolves inputs, materializes one result, and leaves source data."""
    original = np.array(timeseries.value, copy=True)
    store = _RecordingStore({"obj-1": timeseries})
    operation = _crop_operation()
    seen_kwargs: dict[str, Any] = {}

    def apply(inputs: Mapping[str, Any], kwargs: Mapping[str, Any]) -> Any:
        seen_kwargs.update(kwargs)
        return inputs["self"].crop(kwargs["start"], kwargs["end"])

    registry = _registry_for(apply)
    assert tuple(param.name for param in registry["timeseries.crop"].params) == (
        "start",
        "end",
    )
    reference = execute_operation(
        operation,
        registry=registry,
        store=store,
        clock=lambda: 123.0,
    )

    assert reference.object_id == "obj-2"
    assert len(store.put_values) == 1
    assert seen_kwargs == dict(operation.params)
    assert np.array_equal(timeseries.value, original)


@pytest.mark.contract("B-034")
def test_executor_does_not_silence_operation_warnings(
    timeseries: Any,
) -> None:
    """Warnings from scientific adapters remain available for provenance."""
    store = _RecordingStore({"obj-1": timeseries})

    def apply(inputs: Mapping[str, Any], _kwargs: Mapping[str, Any]) -> Any:
        warnings.warn("window default selected", UserWarning, stacklevel=2)
        return inputs["self"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        execute_operation(
            _crop_operation(),
            registry=_registry_for(apply),
            store=store,
            clock=lambda: 123.0,
        )

    assert any("window default selected" in str(item.message) for item in caught)


@pytest.mark.contract("B-035")
def test_executor_wraps_failure_with_operation_provenance(
    timeseries: Any,
) -> None:
    """Failed execution keeps the operation identity and does not materialize output."""
    store = _RecordingStore({"obj-1": timeseries})

    def apply(_inputs: Mapping[str, Any], _kwargs: Mapping[str, Any]) -> Any:
        raise RuntimeError("spectral failure")

    with pytest.raises(OperationError) as raised:
        execute_operation(
            _crop_operation(),
            registry=_registry_for(apply),
            store=store,
            clock=lambda: 123.0,
        )

    assert "op-1" in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert not store.put_values


@pytest.mark.contract("B-048")
def test_executor_normalizes_saved_quantity_before_public_asd_call(
    timeseries: Any,
) -> None:
    """A saved 4000 ms quantity reaches the public ASD API as float 4.0."""
    operation = Operation(
        op_id="op-asd",
        operation_id="timeseries.asd",
        operation_schema=1,
        inputs={"self": "obj-1"},
        params={"fftlength": {"value": 4000.0, "unit": "ms"}},
        outputs=("obj-2",),
    )
    store = _RecordingStore({"obj-1": timeseries})
    seen_kwargs: dict[str, Any] = {}

    def apply(inputs: Mapping[str, Any], kwargs: Mapping[str, Any]) -> Any:
        seen_kwargs.update(kwargs)
        assert isinstance(kwargs["fftlength"], float)
        assert kwargs["fftlength"] == pytest.approx(4.0, rel=1e-12, abs=1e-12)
        return inputs["self"].asd(fftlength=kwargs["fftlength"])

    result_ref = timeseries.asd(fftlength=4.0)
    registry = _registry_for_operation(
        apply,
        operation_id="timeseries.asd",
        result_kind="FrequencySeries",
        params=(
            ParamSpec(
                name="fftlength",
                kind="quantity",
                canonical_unit="s",
                required=True,
            ),
        ),
    )
    asd_spec = registry["timeseries.asd"]
    assert asd_spec.params[0].name == "fftlength"
    assert asd_spec.params[0].canonical_unit == "s"
    execute_operation(
        operation,
        registry=registry,
        store=store,
        clock=lambda: 123.0,
    )

    assert operation.params["fftlength"] == {"value": 4000.0, "unit": "ms"}
    assert seen_kwargs["fftlength"] == pytest.approx(4.0, rel=1e-12, abs=1e-12)
    actual = store.put_values[0]
    actual_values = np.asarray(actual.value)
    reference_values = np.asarray(result_ref.value)
    assert np.isfinite(actual_values).all()
    assert np.isfinite(reference_values).all()
    assert u.Unit(actual.unit) == u.Unit(result_ref.unit)
    actual_values_in_reference_unit = (actual_values * u.Unit(actual.unit)).to_value(
        u.Unit(result_ref.unit)
    )
    tolerance = 1e-12 * max(1.0, float(np.max(np.abs(reference_values))))
    np.testing.assert_allclose(
        actual_values_in_reference_unit,
        reference_values,
        rtol=1e-12,
        atol=tolerance,
    )
