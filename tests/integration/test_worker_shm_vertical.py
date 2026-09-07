"""Vertical shared-memory transport contract across a real worker subprocess."""

from __future__ import annotations

import multiprocessing
import uuid
from dataclasses import replace
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy import units as u

from gwexpy_studio.errors import SharedMemoryError
from gwexpy_studio.worker import client as client_module
from gwexpy_studio.worker.client import WorkerClient
from gwexpy_studio.worker.service import worker_main
from gwexpy_studio.worker.shm import FetchedArray, attach_block
from tests.support.fixtures import SAMPLE_RATE_HZ, T0_GPS, TEST_NAME, write_named_hdf5

pytestmark = pytest.mark.integration

_TARGET_OBJECT_ID = "obj-4"


def _client() -> WorkerClient:
    """Use an explicit spawn context and bounded lifecycle deadlines."""
    context = multiprocessing.get_context("spawn")
    return WorkerClient(
        context=context,
        process_factory=context.Process,
        connection_factory=context.Pipe,
        worker_target=worker_main,
        startup_timeout_s=60.0,
        request_timeout_s=10.0,
        join_timeout_s=5.0,
    )


def _oracle(source: Path) -> Any:
    """Compute the expected ASD with direct gwexpy calls and no worker."""
    from gwexpy.timeseries import TimeSeries

    raw = TimeSeries.read(source, format="hdf5", name=TEST_NAME)
    cropped = raw.crop(T0_GPS + 1.0, T0_GPS + 10.0)
    detrended = cropped.detrend("linear")
    return detrended.asd(fftlength=4.0)


def _asd_operations(source: Path) -> tuple[dict[str, Any], ...]:
    """Declare the curated read -> crop -> detrend -> asd chain as payloads."""
    return (
        {
            "op_id": "op-1",
            "operation_id": "timeseries.read",
            "operation_schema": 1,
            "inputs": {},
            "params": {"source": str(source), "format": "hdf5", "name": TEST_NAME},
            "outputs": ["obj-1"],
        },
        {
            "op_id": "op-2",
            "operation_id": "timeseries.crop",
            "operation_schema": 1,
            "inputs": {"self": "obj-1"},
            "params": {"start": T0_GPS + 1.0, "end": T0_GPS + 10.0},
            "outputs": ["obj-2"],
        },
        {
            "op_id": "op-3",
            "operation_id": "timeseries.detrend",
            "operation_schema": 1,
            "inputs": {"self": "obj-2"},
            "params": {"detrend": "linear"},
            "outputs": ["obj-3"],
        },
        {
            "op_id": "op-4",
            "operation_id": "timeseries.asd",
            "operation_schema": 1,
            "inputs": {"self": "obj-3"},
            "params": {"fftlength": {"value": 4.0, "unit": "s"}},
            "outputs": [_TARGET_OBJECT_ID],
        },
    )


def _execute(client: WorkerClient, op_payload: dict[str, Any]) -> dict[str, Any]:
    """Send one execute request and require a successful result envelope."""
    outputs = list(op_payload["outputs"])
    response: dict[str, Any] = dict(
        client.request(
            {
                "protocol": 2,
                "request_id": str(uuid.uuid4()),
                "type": "execute",
                "payload": {
                    "operation": op_payload,
                    "graph_id": "graph-shm-vertical",
                    "operation_id": op_payload["op_id"],
                    "input_object_ids": list(op_payload["inputs"].values()),
                    "output_object_id": outputs[0] if outputs else "",
                },
            }
        )
    )
    assert response["type"] == "result", response
    payload = response["payload"]
    assert payload["object_id"] == outputs[0]
    return dict(payload)


def _assert_block_absent(name: str) -> None:
    """Require that no shared-memory segment answers to ``name`` any more."""
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=name)


def _copy_out(fetched: FetchedArray) -> np.ndarray:
    """Copy the attached values out and close the consumer handle."""
    try:
        return np.array(fetched.preview.values, copy=True)
    finally:
        fetched.preview.handle.close()


@pytest.mark.contract("C-SHM-021")
def test_fetch_array_round_trips_worker_arrays_and_releases_them(
    sine_32hz: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Walk ObjectStore -> descriptor -> consumer copy-out -> release -> unlink.

    ``WorkerClient.get_array`` only ever returned a descriptor, so until now no
    production code attached one: the whole
    ``get_array -> attach_block -> release_shm`` path existed exclusively inside
    private test helpers. This exercises it through the public
    ``fetch_array``/``release_array`` pair against a **real spawned worker**,
    with a direct-gwexpy oracle as the correctness reference.

    The ASD (1-D) branch is used deliberately: of the five curated operations
    only ``spectrogram`` produces 2-D output, and it is currently unavailable
    for an unrelated, external gwexpy reason. The 2-D
    ``create_block``/``attach_block`` round trip is covered unit-level by
    ``C-SHM-020``; re-parametrizing this test over both branches is the
    follow-up once Spectrogram is usable again.
    """
    source = write_named_hdf5(sine_32hz, tmp_path / "shm-vertical-asd.h5")
    reference = _oracle(source)
    expected_values = np.asarray(reference.value)

    client = _client()
    client.start()
    try:
        for op_payload in _asd_operations(source):
            _execute(client, op_payload)

        fetched = client.fetch_array(_TARGET_OBJECT_ID, preview_stride=None)
        assert isinstance(fetched, FetchedArray)

        # The descriptor -- not a sidecar field -- is the single source of
        # truth for transfer metadata, so the two wire units must agree.
        assert fetched.descriptor.unit == fetched.unit
        assert u.Unit(fetched.unit) == u.Unit(reference.unit)
        assert fetched.descriptor.shape == tuple(expected_values.shape)
        assert fetched.descriptor.dtype == expected_values.dtype.name

        released_name = fetched.descriptor.name
        values = _copy_out(fetched)
        assert values.dtype == expected_values.dtype
        assert values.shape == expected_values.shape
        np.testing.assert_allclose(values, expected_values, rtol=1e-12)
        assert np.isfinite(values).all()
        assert (values >= 0.0).all()
        frequencies = np.asarray(reference.frequencies.to_value("Hz"))
        assert frequencies[int(np.argmax(values))] == pytest.approx(32.0, abs=1e-12)
        assert frequencies[-1] == pytest.approx(SAMPLE_RATE_HZ / 2.0, abs=1e-12)

        # `preview_stride` decimates the consumer's copy-out only: the worker
        # is always asked for full resolution, which is observable here as a
        # full-length descriptor shape behind a strided preview.
        strided = client.fetch_array(_TARGET_OBJECT_ID, preview_stride=4)
        assert strided.descriptor.shape == tuple(expected_values.shape)
        assert strided.descriptor.name != released_name
        strided_name = strided.descriptor.name
        strided_values = _copy_out(strided)
        np.testing.assert_allclose(strided_values, expected_values[::4], rtol=1e-12)
        client.release_array(strided_name)
        _assert_block_absent(strided_name)

        # release_array must reach the worker's release_shm handler, which
        # unlinks the segment; a closed consumer handle alone would not.
        client.release_array(released_name)
        _assert_block_absent(released_name)

        # Re-attaching the *retained* descriptor (not a fresh fetch, which
        # would mint a new block) proves release fails closed downstream.
        with pytest.raises(SharedMemoryError) as reattach_error:
            attach_block(fetched.descriptor, preview_stride=None)
        assert reattach_error.value.code == "shm_not_found"

        # An attach failure must not orphan the block the worker already
        # created. The failure injected here is genuine: the real attach_block
        # runs, on a real worker-minted descriptor whose nbytes has been
        # shifted by one byte (the C-SHM-018 self-inconsistency pattern), so
        # only fetch_array's own cleanup can remove the segment -- the caller
        # never receives its name.
        real_attach = client_module.attach_block
        seen: list[Any] = []

        def _attach_a_corrupted_descriptor(
            descriptor: Any, *, preview_stride: int | None = None
        ) -> Any:
            seen.append(descriptor)
            return real_attach(
                replace(descriptor, nbytes=descriptor.nbytes + 1),
                preview_stride=preview_stride,
            )

        with monkeypatch.context() as patch:
            patch.setattr(client_module, "attach_block", _attach_a_corrupted_descriptor)
            with pytest.raises(SharedMemoryError) as attach_error:
                client.fetch_array(_TARGET_OBJECT_ID, preview_stride=None)
        assert attach_error.value.code == "shm_invalid_descriptor"
        assert len(seen) == 1
        orphan_candidate = seen[0].name
        assert orphan_candidate not in {released_name, strided_name}
        _assert_block_absent(orphan_candidate)
    finally:
        client.shutdown()
