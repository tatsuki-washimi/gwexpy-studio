"""Contract tests for the pure-Python domain record declarations."""

from __future__ import annotations

import json
from dataclasses import asdict, fields, is_dataclass

import pytest

from gwexpy_studio.domain.model import (
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
    PlotSpec,
)

EXPECTED_FIELDS = {
    DataSourceRef: ("source_id", "uri", "format", "size_bytes", "mtime"),
    DataObjectRef: (
        "object_id",
        "kind",
        "shape",
        "dtype",
        "unit",
        "name",
        "channel",
        "axes",
        "produced_by",
        "metadata",
        "members",
    ),
    Operation: (
        "op_id",
        "operation_id",
        "operation_schema",
        "inputs",
        "params",
        "outputs",
    ),
    ExecutionRecord: (
        "execution_id",
        "op_id",
        "started_at",
        "duration_s",
        "status",
        "warnings",
        "error",
        "environment",
        "details",
    ),
    PlotSpec: (
        "plot_id",
        "kind",
        "object_ids",
        "xscale",
        "yscale",
        "xlim",
        "ylim",
        "phase_ylim",
        "title",
        "xlabel",
        "ylabel",
        "legend",
        "styles",
        "component",
        "magnitude_scale",
        "db_reference",
        "display_unit",
        "phase_unwrap",
        "selectors",
    ),
}


@pytest.mark.contract("C-A-001")
def test_domain_dataclass_field_order_and_frozen_slots() -> None:
    """Domain records expose the approved field order and immutable shape."""
    for record_type, expected_names in EXPECTED_FIELDS.items():
        assert is_dataclass(record_type)
        assert tuple(field.name for field in fields(record_type)) == expected_names
        assert getattr(record_type, "__dataclass_params__").frozen is True
        assert set(getattr(record_type, "__slots__")) == set(expected_names)


@pytest.mark.contract("C-A-002")
def test_domain_records_cross_a_json_primitive_boundary() -> None:
    """Every domain record can be represented with deterministic JSON values."""
    records = [
        DataSourceRef(
            source_id="src-999",
            uri="/data/source.h5",
            format="hdf5",
            size_bytes=0,
            mtime=0.0,
        ),
        DataObjectRef(
            object_id="obj-999",
            kind="TimeSeries",
            shape=(0,),
            dtype="float64",
            unit="m",
            name=None,
            channel="X1:STUDIO-CHANNEL",
            axes={"t0": {"value": 1_000_000_000.0, "unit": "s"}},
            produced_by=None,
        ),
        Operation(
            op_id="op-999",
            operation_id="timeseries.read",
            operation_schema=1,
            inputs={},
            params={"source": "/data/source.h5"},
            outputs=("obj-999",),
        ),
        ExecutionRecord(
            execution_id="exec-999",
            op_id="op-999",
            started_at="2026-08-16T00:00:00Z",
            duration_s=0.0,
            status="failed",
            warnings=("deterministic warning",),
            error={"code": "operation_failed", "message": "fixture failure"},
            environment={"python": "3.12.12", "gwexpy": "0.1.14"},
        ),
        PlotSpec(
            plot_id="plot-999",
            kind="line",
            object_ids=("obj-999",),
            xlim=None,
            ylim=(0.0, 1.0),
            title="deterministic",
            xlabel=None,
            ylabel="m",
            legend=True,
            styles={"color": "black"},
        ),
    ]

    encoded = json.dumps([asdict(record) for record in records], allow_nan=False)
    decoded = json.loads(encoded)

    assert isinstance(decoded, list)
    assert len(decoded) == len(records)
    assert decoded[0]["source_id"] == "src-999"
    assert decoded[1]["shape"] == [0]
    assert decoded[2]["outputs"] == ["obj-999"]
    assert decoded[3]["status"] == "failed"
    assert decoded[4]["legend"] is True
