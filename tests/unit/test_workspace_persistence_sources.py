"""Whole-input content evidence without preview or wire-size truncation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.mark.contract("WSP-0068")
def test_fingerprint_covers_unpreviewed_content_and_order(tmp_path):
    from gwexpy_studio.ops.source_fingerprint import fingerprint_sources

    paths = []
    for index in range(150):
        path = tmp_path / f"波形-{index:04d}.csv"
        path.write_bytes(b"abcd")
        paths.append(str(path))
    request = {"paths": paths}
    original = fingerprint_sources(request)
    info = Path(paths[-1]).stat()
    Path(paths[-1]).write_bytes(b"dcba")
    os.utime(paths[-1], ns=(info.st_atime_ns, info.st_mtime_ns))
    changed = fingerprint_sources(request)
    assert changed["content_sha256"] != original["content_sha256"]
    assert changed["entry_count"] == 150
    assert fingerprint_sources({"paths": paths[::-1]}) != changed
    assert len(json.dumps(changed)) < 1024


@pytest.mark.contract("WSP-0069")
def test_same_bytes_with_new_mtime_match_and_directory_inventory_is_ordered(tmp_path):
    from gwexpy_studio.ops.source_fingerprint import fingerprint_sources

    directory = tmp_path / "測定"
    directory.mkdir()
    source = directory / "a.dat"
    source.write_bytes(b"signal")
    before = fingerprint_sources({"paths": [str(directory)]})
    info = source.stat()
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    assert fingerprint_sources({"paths": [str(directory)]}) == before
    (directory / "empty").mkdir()
    after = fingerprint_sources({"paths": [str(directory)]})
    assert after["inventory_sha256"] != before["inventory_sha256"]
    assert after["total_bytes"] == 6


@pytest.mark.contract("WSP-0070")
def test_fingerprint_bounded_and_detects_source_changed_during_read(
    tmp_path, monkeypatch
):
    from gwexpy_studio.ops import source_fingerprint

    source = tmp_path / "source"
    source.write_bytes(b"content")
    with pytest.raises(ValueError, match="byte"):
        source_fingerprint.fingerprint_sources({"paths": [str(source)], "max_bytes": 3})
    original_read = os.read

    def changing_read(fd, size):
        result = original_read(fd, size)
        if result:
            info = source.stat()
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        return result

    monkeypatch.setattr(os, "read", changing_read)
    with pytest.raises(ValueError, match="changed while"):
        source_fingerprint.fingerprint_sources({"paths": [str(source)]})


@pytest.mark.contract("WSP-0071")
def test_fingerprint_rpc_does_not_repeat_long_paths_or_require_native_format(tmp_path):
    from types import SimpleNamespace

    from gwexpy_studio.runtime.store import ObjectStore
    from gwexpy_studio.worker.protocol import encode_message
    from gwexpy_studio.worker.signal_service import SignalService

    directory = tmp_path
    for _ in range(16):
        directory /= "長" * 50
    directory.mkdir(parents=True)
    source = directory / "wave.unknown"
    source.write_bytes(b"x")
    request = {"paths": [str(source)] * 128}
    service = SignalService(ObjectStore(), SimpleNamespace(pending=set()))
    payload = {"mode": "fingerprint", "request": request}
    response = service.dispatch("inspect_io", payload)
    manifest = response["source_manifest"]
    assert manifest["entry_count"] == 128
    envelope = {
        "protocol": 2,
        "request_id": "00000000-0000-4000-8000-000000000123",
        "type": "result",
        "payload": response,
    }
    assert len(encode_message(envelope)) < 1024
    assert (
        service.dispatch("inspect_io", {**payload, "expected_manifest": manifest})
        == response
    )
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        service.dispatch("inspect_io", {**payload, "expected_manifest": manifest})


@pytest.mark.parametrize(
    "input_request",
    [
        pytest.param(
            {"paths": []}, marks=pytest.mark.contract("WSP-0072"), id="empty-paths"
        ),
        pytest.param(
            {"paths": ["x"], "max_bytes": True},
            marks=pytest.mark.contract("WSP-0073"),
            id="boolean-byte-bound",
        ),
        pytest.param(
            {"paths": ["x"], "max_entries": 0},
            marks=pytest.mark.contract("WSP-0074"),
            id="zero-entry-bound",
        ),
    ],
)
def test_fingerprint_rejects_invalid_bounds_without_reading_sources(input_request):
    from gwexpy_studio.ops.source_fingerprint import fingerprint_sources

    with pytest.raises(ValueError):
        fingerprint_sources(input_request)


@pytest.mark.contract("WSP-0075")
def test_fingerprint_missing_fifo_directory_cycle_and_entry_limit(tmp_path):
    from gwexpy_studio.ops.source_fingerprint import fingerprint_sources

    with pytest.raises(FileNotFoundError):
        fingerprint_sources({"paths": [str(tmp_path / "missing")]})
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        fingerprint_sources({"paths": [str(fifo)]})
    fifo.unlink()
    (tmp_path / "cycle").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="cycle"):
        fingerprint_sources({"paths": [str(tmp_path)]})
    (tmp_path / "cycle").unlink()
    for name in ("a", "b"):
        (tmp_path / name).write_bytes(b"x")
    with pytest.raises(ValueError, match="entry"):
        fingerprint_sources({"paths": [str(tmp_path)], "max_entries": 1})
    with pytest.raises(ValueError, match="entry"):
        fingerprint_sources({"paths": [str(tmp_path / "a")] * 2, "max_entries": 1})


@pytest.mark.contract("WSP-0076")
def test_fingerprint_for_ten_thousand_inputs_fits_existing_wire_bound(tmp_path):
    from types import SimpleNamespace

    from gwexpy_studio.runtime.store import ObjectStore
    from gwexpy_studio.worker.protocol import encode_message
    from gwexpy_studio.worker.signal_service import SignalService

    source = tmp_path / "x"
    source.write_bytes(b"x")
    payload = {"mode": "fingerprint", "request": {"paths": [str(source)] * 10000}}
    common = {"protocol": 2, "request_id": "00000000-0000-4000-8000-000000000123"}
    assert (
        len(encode_message({**common, "type": "inspect_io", "payload": payload}))
        < 1048576
    )
    service = SignalService(ObjectStore(), SimpleNamespace(pending=set()))
    response = service.dispatch("inspect_io", payload)
    assert response["source_manifest"]["entry_count"] == 10000
    assert len(encode_message({**common, "type": "result", "payload": response})) < 1024
