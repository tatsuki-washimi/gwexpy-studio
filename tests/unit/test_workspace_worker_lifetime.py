"""Actual cancellation and owner-death cleanup under an isolated subreaper."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import threading
import time
from functools import partial

import pytest

from gwexpy_studio.errors import WorkerCrashedError
from gwexpy_studio.worker.client import WorkerClient, WorkerLifecycle


def _message():
    return {
        "protocol": 2,
        "request_id": "00000000-0000-4000-8000-000000000123",
        "type": "execute",
        "payload": {},
    }


def _capability_document():
    """Return a valid, probe-free capability handshake for test workers."""
    from gwexpy_studio.ops.io_capabilities import (
        load_capability_manifest,
        unprobed_effective_capabilities,
    )

    return unprobed_effective_capabilities(
        load_capability_manifest()
    ).document()


def _blocked_worker(connection, ready):
    from gwexpy_studio.worker.protocol import decode_message, encode_message

    message = decode_message(connection.recv_bytes())
    connection.send_bytes(
        encode_message(
            {
                **message,
                "type": "result",
                "payload": {
                    "ready": True,
                    "io_capabilities": _capability_document(),
                },
            }
        )
    )
    connection.recv_bytes()
    ready.set()
    # Block in native code: cancellation cannot depend on Python cooperation.
    import ctypes

    ctypes.CDLL(None).sleep(60)


@pytest.mark.contract("WSP-0094")
def test_cancel_interrupts_real_request_without_cross_thread_cleanup():
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    client = WorkerClient(
        context=context,
        worker_target=partial(_blocked_worker, ready=ready),
        startup_timeout_s=5,
        request_timeout_s=30,
        join_timeout_s=0.2,
    )
    assert callable(getattr(client, "cancel", None)), "WorkerClient needs cancellation"
    assert client.cancel() is False
    errors = []
    cleanup_threads = []
    original_cleanup = client._force_cleanup_process

    def cleanup():
        cleanup_threads.append(threading.get_ident())
        original_cleanup()

    client._force_cleanup_process = cleanup
    client.start()
    child_pid = client._process.pid

    def request():
        try:
            client.request(_message())
        except Exception as error:
            errors.append(error)

    requester = threading.Thread(target=request)
    requester.start()
    try:
        assert ready.wait(5), "Worker did not receive the native computation request"
        started = time.monotonic()
        assert client.cancel()
        assert time.monotonic() - started < 0.5
        requester.join(5)
        assert not requester.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], WorkerCrashedError)
        assert client.state == WorkerLifecycle.CRASHED
        assert cleanup_threads == [requester.ident]
        assert client.cancel() is False
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        client.shutdown()
        requester.join(5)


_OWNER_DEATH_SCRIPT = r"""
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def worker(connection, evidence):
    import numpy as np
    from gwexpy_studio.worker import service
    from gwexpy_studio.worker.shm import create_block
    transfer = service.DefaultStore()
    block = create_block(np.arange(32, dtype=np.float64))
    transfer.pending.add(block.name)
    block.shm.close()
    class Registry(service.DefaultRegistry):
        def execute(self, payload):
            Path(evidence).write_text(json.dumps({
                'pid': os.getpid(), 'shm': block.name,
            }))
            ctypes.CDLL(None).sleep(60)
            return {}
    service.create_registry = Registry
    service.create_store = lambda: transfer
    service.worker_main(connection)


def gui(evidence):
    from functools import partial
    import multiprocessing
    import threading
    from gwexpy_studio.worker.client import WorkerClient
    mode = sys.argv[3]
    method = 'fork' if mode.endswith('fork') else 'spawn'
    client = WorkerClient(
        context=multiprocessing.get_context(method),
        worker_target=partial(worker, evidence=evidence), join_timeout_s=.2,
    )
    client.start()
    request = {
        'protocol': 2, 'request_id': '00000000-0000-4000-8000-000000000123',
        'type': 'execute', 'payload': {},
    }
    if mode == 'owner-death':
        client.request(request)
        return
    errors = []
    def run():
        try:
            client.request(request)
        except Exception as error:
            errors.append(type(error).__name__)
    requester = threading.Thread(target=run)
    requester.start()
    deadline = time.monotonic() + 10
    while not Path(evidence).exists() and time.monotonic() < deadline:
        time.sleep(.01)
    info = json.loads(Path(evidence).read_text())
    if mode.startswith('cancel'):
        client.cancel()
    else:
        os.kill(info['pid'], signal.SIGKILL)
    requester.join(5)
    client.shutdown()
    deadline = time.monotonic() + 2
    while Path('/dev/shm', info['shm']).exists() and time.monotonic() < deadline:
        time.sleep(.01)
    Path(evidence + '.done').write_text(json.dumps({
        'errors': errors, 'done': not requester.is_alive(),
        'remaining': Path('/dev/shm', info['shm']).exists(),
    }))
    sys.stdin.read(1)  # Keep GUI/tracker alive until supervisor verifies reclamation.


def supervisor(evidence):
    # This disposable test process adopts/reaps only its own orphan descendants.
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    mode = sys.argv[3] if len(sys.argv) > 3 else 'owner-death'
    parent = subprocess.Popen(
        [sys.executable, __file__, 'gui', evidence, mode], stdin=subprocess.PIPE,
    )
    info = None
    statuses = {}
    def reap():
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            if pid == 0:
                return False
            statuses[pid] = status
    try:
        deadline = time.monotonic() + 20
        while not Path(evidence).exists() and time.monotonic() < deadline:
            assert parent.poll() is None, 'GUI exited before compute began'
            time.sleep(.02)
        assert Path(evidence).exists(), 'worker did not begin native compute'
        info = json.loads(Path(evidence).read_text())
        if mode == 'owner-death':
            assert Path('/dev/shm', info['shm']).exists()
            parent.kill()
            parent.wait(5)
        else:
            deadline = time.monotonic() + 10
            completed = Path(evidence + '.done')
            while not completed.exists() and time.monotonic() < deadline:
                assert parent.poll() is None
                time.sleep(.01)
            report = json.loads(completed.read_text())
            assert parent.poll() is None, 'GUI must survive cancellation/crash'
            assert report['done'] and report['errors'] == ['WorkerCrashedError']
            assert not report['remaining'], 'unacknowledged SHM survived live GUI'
            assert not Path('/proc', str(info['pid'])).exists(), 'worker not reaped'
            parent.stdin.write(b'x')
            parent.stdin.flush()
            assert parent.wait(5) == 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if reap() and not Path('/dev/shm', info['shm']).exists():
                break
            time.sleep(.02)
        if mode == 'owner-death':
            assert info['pid'] in statuses, 'busy worker survived GUI SIGKILL'
            assert os.waitstatus_to_exitcode(statuses[info['pid']]) == -signal.SIGKILL
        assert not Path('/dev/shm', info['shm']).exists(), 'pending SHM survived'
        assert reap(), 'owned descendants remained'
        print('worker, trackers and pending shared memory: 0 remaining')
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(5)
        parent.stdin.close()
        if mode == 'owner-death' and info and info['pid'] not in statuses:
            try:
                os.kill(info['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not reap():
            time.sleep(.02)


if __name__ == '__main__':
    (gui if sys.argv[1] == 'gui' else supervisor)(sys.argv[2])
"""


@pytest.mark.contract("WSP-0095")
def test_gui_sigkill_stops_busy_worker_and_reclaims_pending_shared_memory(tmp_path):
    assert sys.platform == "linux", "The ownership acceptance suite requires Linux"
    script = tmp_path / "owned_lifetime.py"
    script.write_text(_OWNER_DEATH_SCRIPT)
    result = subprocess.run(
        [sys.executable, str(script), "supervisor", str(tmp_path / "evidence.json")],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 remaining" in result.stdout


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(
            "cancel-fork", marks=pytest.mark.contract("WSP-0096"), id="cancel-fork"
        ),
        pytest.param(
            "crash-fork", marks=pytest.mark.contract("WSP-0097"), id="crash-fork"
        ),
        pytest.param(
            "cancel-spawn", marks=pytest.mark.contract("WSP-0098"), id="cancel-spawn"
        ),
        pytest.param(
            "crash-spawn", marks=pytest.mark.contract("WSP-0099"), id="crash-spawn"
        ),
    ],
)
def test_cancel_or_crash_reclaims_unacknowledged_shm_while_gui_is_alive(tmp_path, mode):
    assert sys.platform == "linux", "The ownership acceptance suite requires Linux"
    script = tmp_path / "owned_lifetime.py"
    script.write_text(_OWNER_DEATH_SCRIPT)
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "supervisor",
            str(tmp_path / "evidence.json"),
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 remaining" in result.stdout


@pytest.mark.contract("WSP-0100")
def test_parent_guard_leaves_in_process_worker_seam_unchanged(monkeypatch):
    from gwexpy_studio.worker import service

    assert callable(getattr(service, "guard_parent_lifetime", None))
    monkeypatch.setattr(multiprocessing, "parent_process", lambda: None)
    assert service.guard_parent_lifetime() is False


@pytest.mark.contract("WSP-0101")
def test_parent_guard_closes_parent_death_before_installation_race(monkeypatch):
    import signal
    from types import SimpleNamespace

    from gwexpy_studio.worker import parent_lifetime

    calls = []
    monkeypatch.setattr(parent_lifetime.sys, "platform", "linux")
    monkeypatch.setattr(
        multiprocessing, "parent_process", lambda: SimpleNamespace(pid=10)
    )
    monkeypatch.setattr(
        parent_lifetime.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(prctl=lambda *args: 0),
    )
    monkeypatch.setattr(os, "getppid", lambda: 11)
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert parent_lifetime.guard_parent_lifetime()
    assert calls == [(os.getpid(), signal.SIGKILL)]


@pytest.mark.contract("WSP-0102")
def test_parent_guard_reports_kernel_rejection(monkeypatch):
    import errno
    from types import SimpleNamespace

    from gwexpy_studio.worker import parent_lifetime

    monkeypatch.setattr(parent_lifetime.sys, "platform", "linux")
    monkeypatch.setattr(
        multiprocessing, "parent_process", lambda: SimpleNamespace(pid=10)
    )
    monkeypatch.setattr(
        parent_lifetime.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(prctl=lambda *args: -1),
    )
    monkeypatch.setattr(parent_lifetime.ctypes, "get_errno", lambda: errno.EPERM)
    with pytest.raises(OSError) as error:
        parent_lifetime.guard_parent_lifetime()
    assert error.value.errno == errno.EPERM


@pytest.mark.contract("WSP-0102A")
def test_parent_guard_exits_darwin_worker_after_parent_death(monkeypatch):
    from types import SimpleNamespace

    from gwexpy_studio.worker import parent_lifetime

    parent_released = threading.Event()
    watchdog_exited = threading.Event()
    exit_codes = []

    def join_parent() -> None:
        assert parent_released.wait(1)

    monkeypatch.setattr(parent_lifetime.sys, "platform", "darwin")
    monkeypatch.setattr(
        multiprocessing,
        "parent_process",
        lambda: SimpleNamespace(pid=os.getppid(), join=join_parent),
    )
    monkeypatch.setattr(
        os,
        "_exit",
        lambda code: (exit_codes.append(code), watchdog_exited.set()),
    )

    assert parent_lifetime.guard_parent_lifetime()
    parent_released.set()
    assert watchdog_exited.wait(1)
    assert exit_codes == [1]


def _replying_worker(connection):
    from gwexpy_studio.worker.protocol import decode_message, encode_message

    while True:
        message = decode_message(connection.recv_bytes())
        payload = {}
        if message["type"] == "ping":
            payload["io_capabilities"] = _capability_document()
        connection.send_bytes(
            encode_message({**message, "type": "result", "payload": payload})
        )
        if message["type"] == "shutdown":
            return


@pytest.mark.contract("WSP-0103")
def test_cancelled_response_is_rejected_and_new_generation_can_restart():
    client = WorkerClient(
        context=multiprocessing.get_context("fork"),
        worker_target=_replying_worker,
        startup_timeout_s=5,
        request_timeout_s=5,
        join_timeout_s=0.2,
    )
    client.start()
    original_receive = client._recv_frame_within

    def cancel_after_response(connection, timeout_s):
        data = original_receive(connection, timeout_s)
        client.cancel()
        return data

    client._recv_frame_within = cancel_after_response
    try:
        with pytest.raises(WorkerCrashedError, match="cancelled"):
            client.request(_message())
        assert client.state == WorkerLifecycle.CRASHED
        client._recv_frame_within = original_receive
        client.start()
        assert client.request(_message())["type"] == "result"
    finally:
        client.shutdown()
