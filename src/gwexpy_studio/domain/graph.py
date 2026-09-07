"""Append-only operation graph interface."""

from __future__ import annotations

from collections.abc import Collection

from ..errors import OperationError
from .model import Operation


class OperationGraph:
    """Mutable container holding an append-only DAG of semantic operation nodes."""

    def __init__(
        self,
        operations: Collection[Operation] = (),
        *,
        known_root_object_ids: Collection[str] = (),
    ) -> None:
        """Create an operation graph, optionally initialized with operations.

        Every operation is routed through :meth:`add`, so the same DAG
        invariant checks (duplicate op_id, duplicate output, forward
        reference / unknown input) apply whether the graph is built
        incrementally or restored in bulk (e.g. from a loaded project
        document). This is the single validation entry point — do not
        re-implement these checks elsewhere.

        ``known_root_object_ids`` declares object ids that may be used as
        operation inputs even though no operation in this graph produces
        them (e.g. objects materialized from a project's ``sources``/
        ``objects`` sections rather than computed by an operation). Callers
        MUST exclude any object id that is also produced by an operation
        anywhere in ``operations`` — including one that appears later in
        the sequence — otherwise a genuine forward reference/cycle through
        that id would silently bypass validation. See
        ``Project.from_dict`` for the caller that derives this set safely
        (materialized object ids minus the union of all declared operation
        outputs).
        """
        self._operations: list[Operation] = []
        self._operation_by_id: dict[str, Operation] = {}
        self._producer_by_output: dict[str, Operation] = {}
        self._known_root_object_ids: frozenset[str] = frozenset(known_root_object_ids)
        for operation in operations:
            self.add(operation)

    @property
    def operations(self) -> tuple[Operation, ...]:
        """Return graph operations in insertion order as an immutable tuple."""
        return tuple(self._operations)

    def add(self, operation: Operation) -> None:
        """Add an operation after DAG reference and duplicate validation."""
        if operation.op_id in self._operation_by_id:
            raise OperationError(f"Duplicate operation op_id: {operation.op_id}")

        seen_output_ids: set[str] = set()
        for output_id in operation.outputs:
            if output_id in self._producer_by_output:
                producer = self._producer_by_output[output_id]
                raise OperationError(
                    f"Duplicate output object_id: {output_id} already produced by "
                    f"{producer.op_id}"
                )
            if output_id in seen_output_ids:
                # A single operation declaring the same output twice is a
                # duplicate too, even though nothing else in the graph has
                # produced it yet — check within this operation's own
                # outputs, not just against already-registered producers.
                raise OperationError(
                    f"Duplicate output object_id: {output_id} declared twice "
                    f"by operation {operation.op_id}"
                )
            seen_output_ids.add(output_id)

        for role, input_object_id in operation.inputs.items():
            if (
                input_object_id not in self._producer_by_output
                and input_object_id not in self._known_root_object_ids
            ):
                raise OperationError(
                    f"Forward reference or unknown input '{input_object_id}' "
                    f"for role '{role}'"
                )

        self._operations.append(operation)
        self._operation_by_id[operation.op_id] = operation
        for output_id in operation.outputs:
            self._producer_by_output[output_id] = operation

    def producer_of(self, object_id: str) -> Operation | None:
        """Return the producing operation for an object in the graph, or None."""
        return self._producer_by_output.get(object_id)

    def ancestors(self, target_object_ids: Collection[str]) -> tuple[Operation, ...]:
        """Return dependency operations in graph topological order."""
        selected_op_ids: set[str] = set()
        pending_objects = list(target_object_ids)

        while pending_objects:
            obj_id = pending_objects.pop()
            producer = self.producer_of(obj_id)
            if producer is not None and producer.op_id not in selected_op_ids:
                selected_op_ids.add(producer.op_id)
                pending_objects.extend(producer.inputs.values())

        return tuple(op for op in self._operations if op.op_id in selected_op_ids)
