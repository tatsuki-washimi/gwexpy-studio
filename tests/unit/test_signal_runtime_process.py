"""Real worker transport of native container previews and execution recipes."""

from __future__ import annotations

import uuid
from multiprocessing import shared_memory

import numpy as np
import pytest

from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient
from gwexpy_studio.worker.shm import SharedMemoryDescriptor, attach_block


def _rpc(client, kind, payload):
    response = client.request(
        {
            "protocol": 2,
            "request_id": str(uuid.uuid4()),
            "type": kind,
            "payload": payload,
        }
    )
    assert response["type"] == "result", response
    return response["payload"]


def _consume(client, bundle):
    arrays = {}
    for key, raw in bundle["arrays"].items():
        if raw is None:
            arrays[key] = None
            continue
        descriptor = SharedMemoryDescriptor(**{**raw, "shape": tuple(raw["shape"])})
        attached = attach_block(descriptor)
        try:
            arrays[key] = attached.values.copy()
        finally:
            attached.handle.close()
            client.release_array(descriptor.name)
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=descriptor.name)
    return arrays


@pytest.mark.contract("SIG-0041")
def test_real_worker_native_members_preview_catalog_and_write(tmp_path):
    import gwexpy
    from gwexpy.frequencyseries import FrequencySeries, FrequencySeriesDict

    gwexpy.register_all()
    values = FrequencySeries(
        [1 + 2j, 3 + 4j, 5 + 6j], f0=0, df=0.25, unit="m", name="a"
    )
    source = FrequencySeriesDict(
        {
            "a": values,
            "b": FrequencySeries([4.0, 5.0, 6.0], f0=0, df=1, unit="V", name="b"),
        }
    )
    path = tmp_path / "source.h5"
    source.write(str(path), format="hdf5")
    with WorkerClient() as client:
        catalog = _rpc(
            client,
            "catalog_io",
            {"datatype": "FrequencySeriesDict", "direction": "read"},
        )
        assert len(catalog["io_catalog"]["classes"]) == 12
        inspected = _rpc(
            client,
            "inspect_io",
            {
                "request": {
                    "paths": [str(path)],
                    "datatype": "FrequencySeriesDict",
                    "format": "hdf5",
                }
            },
        )
        assert inspected["io_inspection"]["entry_count"] == 1
        loaded = _rpc(
            client,
            "execute",
            {
                "operation": {
                    "op_id": "op-1",
                    "operation_id": "data.read",
                    "operation_schema": 1,
                    "inputs": {},
                    "params": {
                        "datatype": "FrequencySeriesDict",
                        "source": str(path),
                        "format": "hdf5",
                    },
                    "outputs": ["obj-1"],
                }
            },
        )
        assert loaded["object_ref"]["kind"] == "FrequencySeriesDict"
        assert loaded["object_ref"]["unit"] is None
        assert loaded["object_ref"]["produced_by"] == "op-1"
        members = _rpc(
            client, "list_members", {"object_id": "obj-1", "offset": 0, "limit": 1}
        )
        selector = members["members"][0]["selector"]
        preview = _rpc(
            client, "preview_data", {"object_id": "obj-1", "selector": selector}
        )
        copied = _consume(client, preview)
        np.testing.assert_array_equal(copied["values"], values.value)
        np.testing.assert_array_equal(copied["x_coordinates"], values.frequencies.value)
        assert _rpc(client, "list_objects", {})["object_ids"] == ["obj-1"]
        target = tmp_path / "member.h5"
        written = _rpc(
            client,
            "write_data",
            {
                "object_id": "obj-1",
                "selector": selector,
                "request": {"target": str(target), "format": "hdf5"},
            },
        )
        assert written["details"]["datatype"] == "FrequencySeries"
        np.testing.assert_array_equal(
            FrequencySeries.read(str(target), format="hdf5").value, values.value
        )


@pytest.mark.contract("SIG-0042")
def test_real_session_replay_records_filter_details_and_native_metadata(tmp_path):
    import gwexpy
    from gwexpy.timeseries import TimeSeries

    gwexpy.register_all()
    source = TimeSeries(
        np.sin(np.arange(1024) / 3), dt=1 / 128, t0=0, name="source", unit="m"
    )
    path = tmp_path / "source.h5"
    source.write(str(path), format="hdf5")
    graph = OperationGraph(
        [
            Operation(
                op_id="op-1",
                operation_id="data.read",
                operation_schema=1,
                params={
                    "datatype": "TimeSeries",
                    "source": str(path),
                    "format": "hdf5",
                },
                outputs=("obj-1",),
            ),
            Operation(
                op_id="op-2",
                operation_id="timeseries.lowpass",
                operation_schema=1,
                inputs={"self": "obj-1"},
                params={"frequency": 10, "filtfilt": True},
                outputs=("obj-2",),
            ),
        ]
    )
    project = Project(project_id="runtime", graph=graph)
    with WorkerClient() as client:
        session = StudioSession(project=project, client=client)
        session.replay()
        assert project.objects[0].metadata["native_class"].endswith("TimeSeries")
        recorded = project.executions[-1].details
        assert recorded["filter_recipes"][0]["sample_rate"] == 128
        result = _rpc(
            client,
            "filter_preview",
            {
                "op_name": "timeseries.lowpass",
                "inputs": {"self": {"object_id": "obj-1"}},
                "params": {"frequency": 10, "filtfilt": True},
                "recorded_details": recorded,
                "generation": 3,
            },
        )
        assert result["generation"] == 3
        assert len(result["members"]) == 1
        arrays = _consume(client, result["members"][0])
        assert arrays["values"].dtype == np.dtype("complex128")
        assert len(arrays["values"]) == 4096
        assert len(_rpc(client, "list_objects", {})["objects"]) == 2
