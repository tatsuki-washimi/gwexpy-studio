"""Real source inspection through bounded protocol envelopes, without SHM."""

from __future__ import annotations

import itertools
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from gwexpy_studio.application.signal_io_controller import SignalIOController
from gwexpy_studio.domain.graph import OperationGraph
from gwexpy_studio.domain.model import DataObjectRef, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.domain.project_v2 import record_dict
from gwexpy_studio.errors import ProtocolValidationError
from gwexpy_studio.ops.intake import inspect_io
from gwexpy_studio.runtime.store import ObjectStore
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.protocol import encode_message
from gwexpy_studio.worker.signal_service import SignalService


class WireHost(SignalIOController):
    def __init__(self):
        sequence = itertools.count(1)
        self.project = SimpleNamespace(
            sources=(),
            new_source_id=lambda: f"source-{next(sequence)}",
            project_id="wire",
            new_operation_id=lambda: "op-1",
            new_object_id=lambda: "obj-1",
        )
        self.service = SignalService(ObjectStore(), SimpleNamespace(pending=set()))
        self.frames = []
        self.executed = []

    def _signal_request(self, kind, payload):
        common = {"protocol": 2, "request_id": "00000000-0000-4000-8000-000000000123"}
        incoming = encode_message({**common, "type": kind, "payload": payload})
        result = self.service.dispatch(kind, payload)
        outgoing = encode_message({**common, "type": "result", "payload": result})
        self.frames.append((payload, len(incoming), len(outgoing)))
        return result

    def _execute_signal(self, name, inputs, params):
        self.executed.append((name, inputs, params))
        return DataObjectRef(
            object_id="obj-1", kind="TimeSeries", shape=(1,), dtype="float64", unit="m"
        )


def _encode_actual_session_read(params):
    operation = Operation(
        op_id="op-1",
        operation_id="data.read",
        operation_schema=1,
        params=params,
        outputs=("obj-1",),
    )
    project = Project(project_id="wire", graph=OperationGraph([operation]))
    frames = []

    def request(envelope):
        frames.append(encode_message(envelope))
        ref = DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(1,),
            dtype="float64",
            unit="m",
            produced_by="op-1",
        )
        return {
            "protocol": 2,
            "request_id": envelope["request_id"],
            "type": "result",
            "payload": {"object_id": ref.object_id, "object_ref": record_dict(ref)},
        }

    StudioSession(project=project, client=SimpleNamespace(request=request)).replay()
    assert len(frames) == 1
    return frames[0]


@pytest.mark.contract("SIG-0086")
def test_large_accepted_inspection_and_revalidation_stay_inside_wire_limit():
    with tempfile.TemporaryDirectory(prefix="io-wire-") as temporary:
        directory = Path(temporary)
        source = directory / ("x" * (84 - len(temporary) - 1))
        source.write_bytes(b"x")
        request = {
            "datatype": "TimeSeries",
            "paths": [str(source)] * 10_000,
            "format": "csv",
            "combine": "combined",
            "args": [{"__type__": "tuple", "items": [1, "x"]}],
            "kwargs": {"label": "x" * 16_384},
        }
        host = WireHost()
        inspection = host.inspect_io(request)
        assert inspection == inspect_io(request)
        assert inspection["request"]["args"] == request["args"]
        assert inspection["request"]["kwargs"] == request["kwargs"]
        assert host.read_io(inspection)[0].object_id == "obj-1"
        assert host.executed[0][2]["source"] == request["paths"]
        assert host.executed[0][2]["kwargs"] == request["kwargs"]
        assert len(_encode_actual_session_read(host.executed[0][2])) < 1_048_576
        assert len(host.frames) == 2
        assert all(
            incoming < 1_048_576 and outgoing < 1_048_576
            for _, incoming, outgoing in host.frames
        )
        assert host.frames[0][2] < 131_072
        assert host.frames[1][2] < 1024


@pytest.mark.contract("SIG-0087")
def test_compact_revalidation_rescans_unpreviewed_sources(tmp_path):
    paths = []
    for index in range(200):
        path = tmp_path / f"sample-{index:04d}.csv"
        path.write_text("x")
        paths.append(str(path))
    host = WireHost()
    inspection = host.inspect_io({"paths": paths, "format": "csv"})
    assert paths[-1] not in [entry["path"] for entry in inspection["entries"]]
    Path(paths[-1]).write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        host.read_io(inspection)
    assert host.project.sources == ()
    assert host.executed == []


@pytest.mark.contract("SIG-0088")
def test_compact_normalization_preserves_urls_home_relative_and_order(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "signal space.csv"
    source.write_text("x")
    request = {
        "paths": [source.as_uri(), "~/signal space.csv", "signal space.csv"],
        "format": "csv",
    }
    host = WireHost()
    inspection = host.inspect_io(request)
    assert inspection == inspect_io(request)
    assert inspection["paths"] == [str(source)] * 3


@pytest.mark.contract("SIG-0089")
def test_long_native_paths_have_byte_bounded_inspection_previews(tmp_path):
    directory = tmp_path
    for _ in range(18):
        directory /= "x" * 180
    directory.mkdir(parents=True)
    source = directory / "source.csv"
    source.write_text("x")
    request = {"paths": [str(source)] * 128, "format": "csv"}
    host = WireHost()
    inspection = host.inspect_io(request)
    assert inspection["entry_count"] == 128
    assert host.frames[0][2] < 131_072
    assert 0 < inspection["entries_previewed"] < 128


@pytest.mark.parametrize("field", ["gwexpy_version", "format", "args", "paths"])
def test_confirmation_digest_covers_version_format_options_and_order(tmp_path, field):
    paths = [tmp_path / "a.csv", tmp_path / "b.csv"]
    for path in paths:
        path.write_text("x")
    host = WireHost()
    inspection = host.inspect_io(
        {"paths": [str(path) for path in paths], "format": "csv"}
    )
    if field == "gwexpy_version":
        inspection[field] = "changed"
    elif field == "format":
        inspection["request"][field] = "hdf5"
    elif field == "args":
        inspection["request"][field] = ["changed"]
    else:
        inspection["request"][field] = list(reversed(inspection["request"][field]))
    with pytest.raises(ValueError, match="changed"):
        host.read_io(inspection)
    assert host.project.sources == ()


@pytest.mark.contract("SIG-0090")
def test_inspection_preflights_larger_normalized_revalidation_before_confirmation(
    tmp_path,
):
    source = tmp_path / "source.csv"
    source.write_text("x")
    request = {"paths": [str(source)], "format": "csv", "kwargs": {"label": ""}}
    initial = {
        "protocol": 2,
        "request_id": "00000000-0000-4000-8000-000000000123",
        "type": "inspect_io",
        "payload": {"request": request, "compact": True},
    }
    request["kwargs"]["label"] = "x" * (1_048_576 - len(encode_message(initial)) - 10)
    assert len(encode_message(initial)) < 1_048_576
    host = WireHost()
    with pytest.raises(ProtocolValidationError, match="limit"):
        host.inspect_io(request)
    assert host.project.sources == ()
    assert host.executed == []
