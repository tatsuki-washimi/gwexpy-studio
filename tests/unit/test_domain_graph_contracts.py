"""Contract tests for the append-only OperationGraph boundary."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import Operation
from gwexpy_studio.errors import OperationError


def _operation(
    op_id: str,
    *,
    inputs: Mapping[str, str] | None = None,
    outputs: tuple[str, ...],
) -> Operation:
    """Build a deterministic operation record for graph contracts."""
    return Operation(
        op_id=op_id,
        operation_id=f"timeseries.{op_id}",
        operation_schema=1,
        inputs=dict(inputs or {}),
        params={},
        outputs=outputs,
    )


@pytest.mark.contract("C-A-010")
def test_graph_operations_property_is_an_ordered_tuple() -> None:
    """The public graph view is an immutable insertion-order snapshot."""
    graph = OperationGraph()

    assert graph.operations == ()


@pytest.mark.contract("C-A-011")
def test_graph_branch_topology_and_producer_closure() -> None:
    """Branches retain insertion topology and close over all ancestors."""
    graph = OperationGraph()
    root = _operation("op-1", outputs=("obj-1",))
    left = _operation("op-2", inputs={"self": "obj-1"}, outputs=("obj-2",))
    right = _operation("op-3", inputs={"self": "obj-1"}, outputs=("obj-3",))
    merge = _operation(
        "op-4",
        inputs={"left": "obj-2", "right": "obj-3"},
        outputs=("obj-4",),
    )

    for operation in (root, left, right, merge):
        graph.add(operation)

    assert graph.operations == (root, left, right, merge)
    assert graph.producer_of("obj-4") == merge
    assert graph.producer_of("obj-missing") is None
    assert tuple(operation.op_id for operation in graph.ancestors(("obj-4",))) == (
        "op-1",
        "op-2",
        "op-3",
        "op-4",
    )


@pytest.mark.contract("C-A-012")
def test_graph_resolves_existing_output_references() -> None:
    """An input may resolve only to an output already present in the graph."""
    graph = OperationGraph()
    root = _operation("op-1", outputs=("obj-1",))
    child = _operation("op-2", inputs={"self": "obj-1"}, outputs=("obj-2",))

    graph.add(root)
    graph.add(child)

    assert graph.producer_of("obj-1") == root
    assert graph.producer_of("obj-2") == child
    assert tuple(operation.op_id for operation in graph.ancestors(("obj-2",))) == (
        "op-1",
        "op-2",
    )


@pytest.mark.contract("C-A-013")
def test_graph_rejects_forward_references() -> None:
    """An operation cannot refer to an output declared by a future operation."""
    graph = OperationGraph()
    forward = _operation("op-2", inputs={"self": "obj-1"}, outputs=("obj-2",))

    try:
        graph.add(forward)
    except OperationError:
        return
    raise AssertionError("forward reference was accepted")


@pytest.mark.parametrize(
    ("duplicate_operation_id", "duplicate_outputs"),
    [
        pytest.param(
            True,
            ("obj-2",),
            id="duplicate-operation-id",
            marks=pytest.mark.contract("C-A-014"),
        ),
        pytest.param(
            False,
            ("obj-1",),
            id="duplicate-output",
            marks=pytest.mark.contract("C-A-015"),
        ),
    ],
)
def test_graph_rejects_duplicate_operation_or_output_atomically(
    duplicate_operation_id: bool,
    duplicate_outputs: tuple[str, ...],
) -> None:
    """A duplicate operation or output leaves the graph unchanged."""
    graph = OperationGraph()
    original = _operation("op-1", outputs=("obj-1",))
    candidate = _operation(
        "op-1" if duplicate_operation_id else "op-2",
        outputs=duplicate_outputs,
    )

    graph.add(original)
    before = graph.operations
    try:
        graph.add(candidate)
    except OperationError:
        pass
    else:
        raise AssertionError("duplicate graph item was accepted")

    assert graph.operations == before
    assert graph.producer_of("obj-1") == original
    assert graph.producer_of("obj-2") is None


@pytest.mark.contract("C-A-016")
def test_graph_failed_add_is_atomic_and_retryable() -> None:
    """A failed add preserves indexes and the same node can be retried."""
    graph = OperationGraph()
    original = _operation("op-1", outputs=("obj-1",))
    candidate = _operation(
        "op-2",
        inputs={"self": "obj-missing"},
        outputs=("obj-2",),
    )

    graph.add(original)
    before = graph.operations
    before_original_producer = graph.producer_of("obj-1")
    before_candidate_producer = graph.producer_of("obj-2")

    try:
        graph.add(candidate)
    except OperationError:
        pass
    else:
        raise AssertionError("invalid graph item was accepted")

    assert graph.operations == before
    assert graph.producer_of("obj-1") == before_original_producer
    assert graph.producer_of("obj-2") == before_candidate_producer

    producer = _operation("op-3", outputs=("obj-missing",))
    graph.add(producer)
    graph.add(candidate)

    assert graph.producer_of("obj-2") == candidate


@pytest.mark.parametrize(
    ("operations", "case_id"),
    [
        pytest.param(
            (
                _operation("op-2", inputs={"self": "obj-1"}, outputs=("obj-2",)),
                _operation("op-1", outputs=("obj-1",)),
            ),
            "forward-reference",
            id="forward-reference",
            marks=pytest.mark.contract("C-A-075"),
        ),
        pytest.param(
            (
                _operation("op-1", inputs={"self": "obj-2"}, outputs=("obj-1",)),
                _operation("op-2", inputs={"self": "obj-1"}, outputs=("obj-2",)),
            ),
            "cycle",
            id="cycle",
            marks=pytest.mark.contract("C-A-076"),
        ),
        pytest.param(
            (
                _operation("op-1", outputs=("obj-1",)),
                _operation("op-1", outputs=("obj-2",)),
            ),
            "duplicate-op-id",
            id="duplicate-op-id",
            marks=pytest.mark.contract("C-A-077"),
        ),
    ],
)
def test_graph_bulk_constructor_rejects_invariant_violations(
    operations: tuple[Operation, ...], case_id: str
) -> None:
    """``OperationGraph(operations)`` enforces the same invariants as ``add()``.

    Bulk construction is also used when ``Project.from_dict`` restores a
    project, so cycles, forward references, and duplicate operation IDs must
    take the same validation path as incremental additions. Because every
    operation is routed through ``add()`` in array order, the bulk constructor
    rejects not only genuine cycles/forward-references but also a **valid**
    DAG whose ``operations`` array happens not to be listed in topological
    order -- a later-indexed operation cannot be consumed by an
    earlier-indexed one even if no cycle actually exists among them. Studio
    itself never produces such an array (only ``add()`` ever appends to a
    graph, so anything Studio serializes is already topological by
    construction), but ADR-0012 treats project documents as untrusted input,
    so an externally authored, order-shuffled-but-acyclic document would be
    rejected here as a forward reference even though it encodes a valid DAG.
    This is a documented design precondition of the serialized operation list.
    """
    try:
        OperationGraph(operations)
    except OperationError:
        return
    raise AssertionError(f"{case_id} was accepted by the bulk constructor")
