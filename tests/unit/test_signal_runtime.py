"""Scientific execution, protocol-v2, and session provenance regression tests."""

from __future__ import annotations

import json
import uuid
import warnings
from pathlib import Path

import numpy as np
import pytest

from gwexpy_studio.domain.model import Operation
from gwexpy_studio.errors import OperationError, ProtocolValidationError
from gwexpy_studio.ops.native_results import ScientificResult
from gwexpy_studio.ops.spec import OperationSpec
from gwexpy_studio.runtime import executor
from gwexpy_studio.runtime.store import ObjectStore
from gwexpy_studio.worker import protocol


@pytest.mark.contract("SIG-0074")
def test_worker_execution_records_actual_installed_environment():
    import importlib.metadata
    import platform

    from gwexpy.timeseries import TimeSeries

    from gwexpy_studio.worker.service import DefaultRegistry

    registry = DefaultRegistry()
    registry.store.put(TimeSeries(np.arange(128.0), dt=0.01))
    response = registry.execute(
        {
            "operation": {
                "op_id": "op-1",
                "operation_id": "timeseries.crop",
                "operation_schema": 1,
                "inputs": {"self": "obj-1"},
                "params": {"start": 0.1, "end": 0.9},
                "outputs": ["obj-2"],
            }
        }
    )
    assert response["environment"]["python"] == platform.python_version()
    for package in ("gwexpy", "gwpy", "numpy", "scipy", "astropy"):
        assert response["environment"][package] == importlib.metadata.version(package)


def _spec(apply):
    return OperationSpec(
        operation_id="test.add",
        schema_version=1,
        input_roles=("self",),
        optional_input_roles=("other",),
        accepted_input_kinds={"self": ("TimeSeries",), "other": ("TimeSeries",)},
        result_kind="input",
        params=(),
        apply=apply,
        emit=lambda _: "",
    )


@pytest.mark.contract("SIG-0039")
def test_detailed_executor_resolves_optional_inputs_and_records_warnings():
    from gwexpy.timeseries import TimeSeries

    store = ObjectStore()
    first = store.put(TimeSeries(np.arange(4.0), t0=0, dt=1, unit="m"))
    second = store.put(TimeSeries(np.ones(4), t0=0, dt=1, unit="m"))

    def apply(inputs, params):
        warnings.warn("native note", UserWarning)
        return ScientificResult(
            value=inputs["self"] + inputs["other"], details={"coefficients": [1, 2]}
        )

    operation = Operation(
        op_id="op-1",
        operation_id="test.add",
        operation_schema=1,
        inputs={"self": first.object_id, "other": second.object_id},
        outputs=("obj-3",),
    )
    outcome = executor.execute_operation_detailed(
        operation, registry={"test.add": _spec(apply)}, store=store
    )
    assert outcome.object_ref.object_id == "obj-3"
    assert outcome.object_ref.produced_by == "op-1"
    assert outcome.details == {"coefficients": [1, 2]}
    assert outcome.warnings == ("native note",)
    np.testing.assert_array_equal(store.get("obj-3").value, np.arange(4.0) + 1)
    np.testing.assert_array_equal(store.get("obj-1").value, np.arange(4.0))


@pytest.mark.contract("SIG-0040")
def test_executor_rejects_wrong_native_kind_before_apply():
    from gwexpy.frequencyseries import FrequencySeries

    store = ObjectStore()
    store.put(FrequencySeries([1.0, 2.0], f0=0, df=1))
    called = []
    operation = Operation(
        op_id="op-1",
        operation_id="test.add",
        operation_schema=1,
        inputs={"self": "obj-1"},
    )
    with pytest.raises(OperationError, match="kind"):
        executor.execute_operation(
            operation,
            registry={"test.add": _spec(lambda *_: called.append(1))},
            store=store,
        )
    assert called == []
    assert len(store) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "catalog_io",
        "inspect_io",
        "list_members",
        "preview_data",
        "filter_preview",
        "write_data",
    ],
)
def test_protocol_v2_accepts_signal_messages_and_rejects_v1(kind):
    request = {
        "protocol": 2,
        "request_id": str(uuid.uuid4()),
        "type": kind,
        "payload": {},
    }
    assert protocol.decode_message(protocol.encode_message(request)) == request
    assert protocol.PROTOCOL_VERSION == 2
    with pytest.raises(ProtocolValidationError, match="version"):
        protocol.encode_message({**request, "protocol": 1})


@pytest.mark.parametrize("plot_id", ["plot-1-key=sensor", "plot-filter-response-1"])
def test_v2_schema_allows_member_and_transient_plot_identifiers(plot_id):
    from jsonschema import Draft202012Validator

    path = (
        Path(__file__).parents[2] / "src/gwexpy_studio/schemas/project-v2.schema.json"
    )
    rule = json.loads(path.read_text())["$defs"]["plotId"]
    assert list(Draft202012Validator(rule).iter_errors(plot_id)) == []
