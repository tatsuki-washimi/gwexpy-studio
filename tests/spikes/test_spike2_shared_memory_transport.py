"""Spike 2 contracts for shared-memory copy-out and ownership cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from gwexpy_studio.errors import PrototypeNotImplementedError

pytestmark = pytest.mark.integration


def _run_bounded_probe(probe: str, *arguments: str, timeout_s: float = 30.0) -> bytes:
    """Run the complete SHM scenario in an externally supervised process group."""
    repository_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    source_paths = [str(repository_root / "src"), str(repository_root)]
    if environment.get("PYTHONPATH"):
        source_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(source_paths)
    supervisor = subprocess.Popen(
        [sys.executable, "-c", probe, *arguments],
        cwd=repository_root / "tests",
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = supervisor.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(supervisor.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            supervisor.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(supervisor.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            supervisor.wait(timeout=3.0)
        try:
            os.killpg(supervisor.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        assert supervisor.poll() is not None, "SHM supervisor survived timeout cleanup"
        raise AssertionError("shared-memory scenario exceeded watchdog") from error
    assert supervisor.returncode == 0, stderr.decode("utf-8", errors="replace")
    return stdout


def _raise_reported_sentinel(report: Mapping[str, Any]) -> None:
    """Re-raise a fresh-child sentinel with its exact production owner."""
    if report.get("status") != "sentinel":
        return
    assert report["owner"] == "gwexpy_studio.worker.shm.create_block"
    raise PrototypeNotImplementedError(
        code=str(report["code"]), owner=str(report["owner"])
    )


def _shared_memory_child(mode: str, nbytes: int, connection: Any) -> None:
    """Spawn-importable child for the production SHM ownership scenarios."""
    import json as child_json
    from multiprocessing import shared_memory

    import numpy as child_numpy

    from gwexpy_studio.errors import PrototypeNotImplementedError as ChildSentinel
    from gwexpy_studio.errors import SharedMemoryError
    from gwexpy_studio.worker.shm import (
        create_block,
        release_block,
    )

    def send(message: dict[str, Any]) -> None:
        connection.send_bytes(child_json.dumps(message).encode("utf-8"))

    def receive() -> dict[str, Any]:
        return child_json.loads(connection.recv_bytes().decode("utf-8"))

    def descriptor_json(descriptor: Any) -> dict[str, Any]:
        return {
            "name": descriptor.name,
            "dtype": descriptor.dtype,
            "shape": list(descriptor.shape),
            "nbytes": descriptor.nbytes,
            "order": descriptor.order,
            "unit": str(descriptor.unit),
        }

    def array_for_size(size: int) -> Any:
        assert size % child_numpy.dtype(child_numpy.float64).itemsize == 0
        return child_numpy.arange(
            size // child_numpy.dtype(child_numpy.float64).itemsize,
            dtype=child_numpy.float64,
        )

    if mode == "crash":
        block = create_block(array_for_size(nbytes))
        send(
            {
                "status": "created",
                "creator_pid": os.getpid(),
                "descriptor": descriptor_json(block.descriptor),
            }
        )
        connection.close()
        os._exit(17)

    block = None
    released = False
    try:
        try:
            block = create_block(array_for_size(nbytes))
        except ChildSentinel as error:
            send(
                {
                    "status": "sentinel",
                    "code": error.code,
                    "owner": error.owner,
                }
            )
            return
        descriptor = descriptor_json(block.descriptor)
        send(
            {
                "status": "created",
                "creator_pid": os.getpid(),
                "descriptor": descriptor,
            }
        )
        if mode == "normal":
            assert receive() == {"command": "descriptor_received"}
            send({"status": "receipt_confirmed"})
            assert receive() == {"command": "mutate"}
            owner_values = child_numpy.ndarray(
                tuple(descriptor["shape"]),
                dtype=child_numpy.dtype(descriptor["dtype"]),
                buffer=block.shm.buf,
                order=descriptor["order"],
            )
            owner_values[...] = -123.0
            send({"status": "mutated"})
            assert receive() == {"command": "release", "acknowledged": True}
        elif mode == "ack":
            assert receive() == {"command": "release_without_ack"}
            try:
                release_block(descriptor["name"], acknowledged=False)
            except SharedMemoryError as error:
                assert error.code == "operation_failed"
                send({"status": "ack_failed", "code": error.code})
            else:
                raise AssertionError(
                    "release without acknowledgement unexpectedly passed"
                )
            assert receive() == {"command": "release", "acknowledged": True}
        else:
            raise AssertionError(f"unknown SHM child mode: {mode}")
        release_block(descriptor["name"], acknowledged=True)
        released = True
        try:
            shared_memory.SharedMemory(name=descriptor["name"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("acknowledged release did not unlink exact block")
        send({"status": "unlinked"})
    finally:
        if block is not None:
            block.shm.close()
        connection.close()
        assert released or block is None


def _normal_shm_probe(nbytes: int, preview_stride: int) -> dict[str, Any]:
    """Exercise descriptor transfer, copy-out, mutation isolation, and release."""
    probe = r"""
import json
import multiprocessing
import os
import sys
from multiprocessing import shared_memory

import numpy as np

from gwexpy_studio.errors import PrototypeNotImplementedError
from gwexpy_studio.worker.shm import (
    SharedMemoryDescriptor,
    attach_block,
    create_block,
    release_block,
)


def send(connection, message):
    connection.send_bytes(json.dumps(message, sort_keys=True).encode("utf-8"))


def receive(connection):
    return json.loads(connection.recv_bytes().decode("utf-8"))


def descriptor_json(descriptor):
    return {
        "name": descriptor.name,
        "dtype": descriptor.dtype,
        "shape": list(descriptor.shape),
        "nbytes": descriptor.nbytes,
        "order": descriptor.order,
        "unit": str(descriptor.unit),
    }


def array_for_size(nbytes):
    assert nbytes % np.dtype(np.float64).itemsize == 0
    return np.arange(nbytes // np.dtype(np.float64).itemsize, dtype=np.float64)


def finish_child(child, started):
    if not started:
        return
    if child.is_alive():
        child.terminate()
        child.join(timeout=5.0)
    if child.is_alive():
        child.kill()
        child.join(timeout=5.0)
    alive = child.is_alive()
    exitcode = child.exitcode
    child.close()
    assert not alive, "SHM creator child leaked"
    assert exitcode is not None


def emergency_unlink(name):
    if not name:
        return
    try:
        handle = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return
    try:
        handle.unlink()
    finally:
        handle.close()


def creator_worker(nbytes, connection):
    block = None
    released = False
    try:
        try:
            block = create_block(array_for_size(nbytes))
        except PrototypeNotImplementedError as error:
            send(connection, {
                "status": "sentinel",
                "code": error.code,
                "owner": error.owner,
            })
            return
        descriptor = descriptor_json(block.descriptor)
        send(connection, {
            "status": "created",
            "creator_pid": os.getpid(),
            "descriptor": descriptor,
        })
        assert receive(connection) == {"command": "descriptor_received"}
        send(connection, {"status": "receipt_confirmed"})
        assert receive(connection) == {"command": "mutate"}
        owner_values = np.ndarray(
            tuple(descriptor["shape"]),
            dtype=np.dtype(descriptor["dtype"]),
            buffer=block.shm.buf,
            order=descriptor["order"],
        )
        owner_values[...] = -123.0
        send(connection, {"status": "mutated"})
        assert receive(connection) == {"command": "release", "acknowledged": True}
        release_block(descriptor["name"], acknowledged=True)
        released = True
        try:
            shared_memory.SharedMemory(name=descriptor["name"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("creator release did not unlink its exact block")
        send(connection, {"status": "unlinked"})
    finally:
        if block is not None:
            block.shm.close()
        connection.close()
        assert released or block is None


def main():
    from tests.spikes.test_spike2_shared_memory_transport import _shared_memory_child

    nbytes = int(sys.argv[1])
    preview_stride = int(sys.argv[2])
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    child = context.Process(
        target=_shared_memory_child,
        args=("normal", nbytes, child_connection),
    )
    started = False
    parent_closed = False
    child_closed = False
    descriptor_name = None
    product_cleanup_asserted = False
    try:
        child.start()
        started = True
        child_connection.close()
        child_closed = True
        first = receive(parent_connection)
        if first["status"] == "sentinel":
            child.join(timeout=5.0)
            alive = child.is_alive()
            exitcode = child.exitcode
            child.close()
            child_closed = True
            started = False
            assert not alive
            assert exitcode == 0
            print(json.dumps(first, sort_keys=True))
            return
        assert set(first) == {"status", "creator_pid", "descriptor"}
        assert first["status"] == "created"
        assert first["creator_pid"] != os.getpid()
        descriptor = first["descriptor"]
        descriptor_name = str(descriptor["name"])
        assert set(descriptor) == {
            "name", "dtype", "shape", "nbytes", "order", "unit"
        }
        assert descriptor["dtype"] == "float64"
        assert descriptor["shape"] == [nbytes // 8]
        assert descriptor["nbytes"] == nbytes
        assert descriptor["order"] == "C"
        assert descriptor["unit"] == ""
        send(parent_connection, {"command": "descriptor_received"})
        assert receive(parent_connection) == {"status": "receipt_confirmed"}
        descriptor_obj = SharedMemoryDescriptor(
            name=descriptor_name,
            dtype=str(descriptor["dtype"]),
            shape=tuple(descriptor["shape"]),
            nbytes=int(descriptor["nbytes"]),
            order=descriptor["order"],
            unit=str(descriptor["unit"]),
        )
        preview = attach_block(
            descriptor_obj,
            preview_stride=preview_stride,
        )
        display_values = np.asarray(preview.values).copy()
        display_coordinates = np.asarray(preview.coordinates).copy()
        expected_values = array_for_size(nbytes)[::preview_stride]
        expected_coordinates = np.arange(
            nbytes // np.dtype(np.float64).itemsize, dtype=np.int64
        )[::preview_stride]
        assert np.array_equal(display_values, expected_values)
        assert np.array_equal(display_coordinates, expected_coordinates)
        copied_values = display_values.copy()
        copied_coordinates = display_coordinates.copy()
        preview.handle.close()
        send(parent_connection, {"command": "mutate"})
        assert receive(parent_connection) == {"status": "mutated"}
        assert np.array_equal(copied_values, expected_values)
        assert np.array_equal(copied_coordinates, expected_coordinates)
        assert np.array_equal(display_values, copied_values)
        assert np.array_equal(display_coordinates, copied_coordinates)
        send(parent_connection, {"command": "release", "acknowledged": True})
        assert receive(parent_connection) == {"status": "unlinked"}
        child.join(timeout=10.0)
        alive = child.is_alive()
        exitcode = child.exitcode
        child.close()
        child_closed = True
        started = False
        assert not alive
        assert exitcode == 0
        with __import__("pytest").raises(FileNotFoundError):
            shared_memory.SharedMemory(name=descriptor_name)
        product_cleanup_asserted = True
        print(json.dumps({
            "status": "ok",
            "nbytes": nbytes,
            "shape": descriptor["shape"],
            "preview_count": len(display_values),
            "descriptor_name": descriptor_name,
        }, sort_keys=True))
    finally:
        if started:
            finish_child(child, started)
            started = False
            child_closed = True
        if not parent_closed:
            parent_connection.close()
            parent_closed = True
        if not child_closed:
            child_connection.close()
            child_closed = True
        if not product_cleanup_asserted:
            emergency_unlink(descriptor_name)


main()
"""
    output = _run_bounded_probe(probe, str(nbytes), str(preview_stride))
    return json.loads(output.decode("utf-8").splitlines()[-1])


def _crash_shm_probe() -> dict[str, Any]:
    """Exercise failed acknowledgement and exact-name crash cleanup."""
    probe = r"""
import json
import multiprocessing
import os
from multiprocessing import shared_memory

import numpy as np

from gwexpy_studio.errors import PrototypeNotImplementedError, SharedMemoryError
from gwexpy_studio.worker.shm import create_block, release_block


def send(connection, message):
    connection.send_bytes(json.dumps(message, sort_keys=True).encode("utf-8"))


def receive(connection):
    return json.loads(connection.recv_bytes().decode("utf-8"))


def descriptor_json(descriptor):
    return {
        "name": descriptor.name,
        "dtype": descriptor.dtype,
        "shape": list(descriptor.shape),
        "nbytes": descriptor.nbytes,
        "order": descriptor.order,
        "unit": str(descriptor.unit),
    }


def array_for_size(nbytes=1024**2):
    return np.arange(nbytes // 8, dtype=np.float64)


def finish_child(child, started):
    if not started:
        return
    if child.is_alive():
        child.terminate()
        child.join(timeout=5.0)
    if child.is_alive():
        child.kill()
        child.join(timeout=5.0)
    alive = child.is_alive()
    exitcode = child.exitcode
    child.close()
    assert not alive, "SHM child leaked"
    assert exitcode is not None


def emergency_unlink(name):
    if not name:
        return
    try:
        handle = shared_memory.SharedMemory(name=name)
    except FileNotFoundError:
        return
    try:
        handle.unlink()
    finally:
        handle.close()


def acknowledgement_worker(connection):
    block = None
    released = False
    try:
        try:
            block = create_block(array_for_size())
        except PrototypeNotImplementedError as error:
            send(connection, {
                "status": "sentinel",
                "code": error.code,
                "owner": error.owner,
            })
            return
        descriptor = descriptor_json(block.descriptor)
        send(connection, {"status": "created", "descriptor": descriptor})
        assert receive(connection) == {"command": "release_without_ack"}
        try:
            release_block(descriptor["name"], acknowledged=False)
        except SharedMemoryError as error:
            assert error.code == "operation_failed"
            send(connection, {"status": "ack_failed", "code": error.code})
        else:
            raise AssertionError("release without acknowledgement unexpectedly passed")
        assert receive(connection) == {"command": "release", "acknowledged": True}
        release_block(descriptor["name"], acknowledged=True)
        released = True
        try:
            shared_memory.SharedMemory(name=descriptor["name"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("acknowledged release did not unlink exact block")
        send(connection, {"status": "unlinked"})
    finally:
        if block is not None:
            block.shm.close()
        connection.close()
        assert released or block is None


def crash_worker(connection):
    block = create_block(array_for_size())
    send(connection, {
        "status": "created",
        "creator_pid": os.getpid(),
        "descriptor": descriptor_json(block.descriptor),
    })
    connection.close()
    os._exit(17)


def main():
    from tests.spikes.test_spike2_shared_memory_transport import _shared_memory_child

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    child = context.Process(
        target=_shared_memory_child,
        args=("ack", 1024**2, child_connection),
    )
    started = False
    child_endpoint_closed = False
    descriptor_name = None
    product_cleanup_asserted = False
    crash_parent = None
    crash_endpoint = None
    crash_child = None
    crash_started = False
    crash_name = None
    crash_product_cleanup_asserted = False
    try:
        child.start()
        started = True
        child_connection.close()
        child_endpoint_closed = True
        first = receive(parent_connection)
        if first["status"] == "sentinel":
            child.join(timeout=5.0)
            alive = child.is_alive()
            exitcode = child.exitcode
            child.close()
            started = False
            assert not alive
            assert exitcode == 0
            print(json.dumps(first, sort_keys=True))
            return
        assert first["status"] == "created"
        descriptor_name = str(first["descriptor"]["name"])
        send(parent_connection, {"command": "release_without_ack"})
        assert receive(parent_connection) == {
            "status": "ack_failed", "code": "operation_failed"
        }
        still_attached = shared_memory.SharedMemory(name=descriptor_name)
        still_attached.close()
        send(parent_connection, {"command": "release", "acknowledged": True})
        assert receive(parent_connection) == {"status": "unlinked"}
        child.join(timeout=10.0)
        alive = child.is_alive()
        exitcode = child.exitcode
        child.close()
        started = False
        assert not alive
        assert exitcode == 0
        with __import__("pytest").raises(FileNotFoundError):
            shared_memory.SharedMemory(name=descriptor_name)
        product_cleanup_asserted = True

        crash_parent, crash_endpoint = context.Pipe(duplex=True)
        crash_child = context.Process(
            target=_shared_memory_child,
            args=("crash", 1024**2, crash_endpoint),
        )
        crash_child.start()
        crash_started = True
        crash_endpoint.close()
        crash_message = receive(crash_parent)
        assert crash_message["status"] == "created"
        assert crash_message["creator_pid"] != os.getpid()
        crash_name = str(crash_message["descriptor"]["name"])
        crash_child.join(timeout=10.0)
        crash_alive = crash_child.is_alive()
        crash_exitcode = crash_child.exitcode
        crash_child.close()
        crash_started = False
        assert not crash_alive
        assert crash_exitcode == 17
        deadline = __import__("time").monotonic() + 5.0
        while __import__("time").monotonic() < deadline:
            try:
                handle = shared_memory.SharedMemory(name=crash_name)
            except FileNotFoundError:
                break
            else:
                handle.close()
                __import__("time").sleep(0.05)
        else:
            raise AssertionError(
                "product cleanup left the exact descriptor-reported SHM name"
            )
        crash_product_cleanup_asserted = True
        print(json.dumps({
            "status": "ok",
            "ack_failure": "operation_failed",
            "crash_descriptor_received": True,
            "crash_product_cleanup": True,
        }, sort_keys=True))
    finally:
        if started:
            finish_child(child, started)
        if not child_endpoint_closed:
            child_connection.close()
        parent_connection.close()
        if crash_started:
            finish_child(crash_child, crash_started)
        if crash_parent is not None:
            crash_parent.close()
        if crash_endpoint is not None:
            crash_endpoint.close()
        if not product_cleanup_asserted:
            emergency_unlink(descriptor_name)
        if not crash_product_cleanup_asserted:
            emergency_unlink(crash_name)


main()
"""
    output = _run_bounded_probe(probe, timeout_s=30.0)
    return json.loads(output.decode("utf-8").splitlines()[-1])


@pytest.mark.parametrize(
    ("case", "nbytes", "preview_stride"),
    [
        pytest.param(
            "one-mib",
            1 * 1024**2,
            3,
            id="one-mib",
            marks=pytest.mark.contract("I-S2-001"),
        ),
        pytest.param(
            "sixteen-mib",
            16 * 1024**2,
            17,
            id="sixteen-mib",
            marks=pytest.mark.contract("I-S2-002"),
        ),
        pytest.param(
            "one-hundred-mib-long",
            100 * 1024**2,
            257,
            id="one-hundred-mib-long",
            marks=[pytest.mark.contract("I-S2-003"), pytest.mark.long],
        ),
    ],
)
def test_shared_memory_copy_out_preserves_values_and_display_preview(
    case: str,
    nbytes: int,
    preview_stride: int,
) -> None:
    """Real boundary sizes preserve metadata, copy-out, and display previews."""
    assert case in {"one-mib", "sixteen-mib", "one-hundred-mib-long"}
    report = _normal_shm_probe(nbytes, preview_stride)
    _raise_reported_sentinel(report)
    assert report["status"] == "ok"
    assert report["nbytes"] == nbytes
    assert report["shape"] == [nbytes // 8]
    assert (
        report["preview_count"] == (nbytes // 8 + preview_stride - 1) // preview_stride
    )


@pytest.mark.contract("I-S2-004")
def test_shared_memory_failure_ack_and_creator_cleanup_are_observable() -> None:
    """Failed acknowledgement and abrupt descriptor-reported death do not orphan SHM."""
    report = _crash_shm_probe()
    _raise_reported_sentinel(report)
    assert report["status"] == "ok"
    assert report["ack_failure"] == "operation_failed"
    assert report["crash_descriptor_received"] is True
    assert report["crash_product_cleanup"] is True
