"""Scientific execution with native results and reproducible provenance."""

from __future__ import annotations

import inspect
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..domain.model import DataObjectRef, Operation
from ..domain.project_v2 import record_dict
from ..errors import OperationError
from ..ops.native_filters import science_filter_from_recipes
from ..ops.native_results import ScientificResult
from ..ops.registry import REGISTRY as DEFAULT_REGISTRY
from ..ops.spec import OperationSpec, normalize_params
from .store import ObjectStore

_RECORDED_FILTER_OPERATIONS = frozenset(
    f"timeseries.{name}" for name in ("lowpass", "highpass", "bandpass", "notch", "zpk")
)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """A materialized native object and the diagnostics from its execution."""

    object_ref: DataObjectRef
    details: Mapping[str, Any]
    warnings: tuple[str, ...]


def resolve_inputs(
    operation: Operation, spec: OperationSpec, store: ObjectStore
) -> dict[str, Any]:
    """Resolve declared roles and reject incompatible native kinds before apply."""
    accepted_roles = {*spec.input_roles, *spec.optional_input_roles}
    unknown = set(operation.inputs) - accepted_roles
    if unknown:
        raise OperationError(
            f"Unknown input roles for {operation.op_id}: {sorted(unknown)}"
        )
    resolved = {}
    for role in (*spec.input_roles, *spec.optional_input_roles):
        if role not in operation.inputs:
            if role in spec.optional_input_roles:
                continue
            raise OperationError(
                f"Missing required input role {role!r} for operation {operation.op_id}"
            )
        object_id = operation.inputs[role]
        try:
            value = store.get(object_id)
        except KeyError as exc:
            raise OperationError(
                f"Input object {object_id!r} not found for operation {operation.op_id}",
                code="object_not_found",
            ) from exc
        kinds = (spec.accepted_input_kinds or {}).get(role)
        if kinds is not None and type(value).__name__ not in kinds:
            raise OperationError(
                f"Input role {role!r} has unsupported native kind "
                f"{type(value).__name__!r} for {operation.op_id}"
            )
        resolved[role] = value
    return resolved


def execute_operation_detailed(
    operation: Operation,
    *,
    registry: Mapping[str, OperationSpec] | None = None,
    store: ObjectStore | None = None,
    recorded_details: Mapping[str, Any] | None = None,
) -> ExecutionOutcome:
    """Execute once, collecting warnings and the native scientific recipe."""
    reg = registry if registry is not None else DEFAULT_REGISTRY
    objects = store if store is not None else ObjectStore()
    if operation.operation_id not in reg:
        raise OperationError(
            f"Unknown operation {operation.operation_id!r} "
            f"for operation {operation.op_id}"
        )
    spec = reg[operation.operation_id]
    if operation.operation_schema != spec.schema_version:
        raise OperationError(f"Unsupported operation schema for {operation.op_id}")
    try:
        params = normalize_params(operation.params, spec=spec)
    except Exception as exc:
        raise OperationError(
            f"Parameter normalization failed for operation {operation.op_id}: {exc}"
        ) from exc
    inputs = resolve_inputs(operation, spec, objects)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if recorded_details and recorded_details.get("filter_recipes"):
                recipes = recorded_details["filter_recipes"]
                if operation.operation_id not in _RECORDED_FILTER_OPERATIONS:
                    raise OperationError(
                        "Recorded filter recipes require a filter operation"
                    )
                if not isinstance(recipes, (list, tuple)) or any(
                    not isinstance(recipe, Mapping)
                    or recipe.get("operation") != operation.operation_id
                    for recipe in recipes
                ):
                    raise OperationError(
                        "Recorded filter recipe operation does not match"
                    )
                result = ScientificResult(
                    value=science_filter_from_recipes(inputs, recorded_details),
                    details=recorded_details,
                )
            else:
                result = spec.apply(inputs, params)
        details = (
            record_dict(result.details) if isinstance(result, ScientificResult) else {}
        )
        value = result.value if isinstance(result, ScientificResult) else result
        output_id = operation.outputs[0] if operation.outputs else None
        signature = inspect.signature(objects.put)
        if "produced_by" in signature.parameters:
            ref = objects.put(value, produced_by=operation.op_id, object_id=output_id)
        else:
            # Retain the injected minimal store protocol used by headless clients.
            ref = objects.put(value)
    except Exception as exc:
        raise OperationError(
            f"Execution failed for operation {operation.op_id}: {exc}"
        ) from exc
    return ExecutionOutcome(ref, details, tuple(str(item.message) for item in caught))


def execute_operation(
    operation: Operation,
    *,
    registry: Mapping[str, OperationSpec] | None = None,
    store: ObjectStore | None = None,
    clock: Callable[[], float] | None = None,
) -> DataObjectRef:
    """Execute an operation, preserving the original reference-returning API."""
    del clock
    outcome = execute_operation_detailed(operation, registry=registry, store=store)
    for message in outcome.warnings:
        warnings.warn(message, UserWarning, stacklevel=2)
    return outcome.object_ref
