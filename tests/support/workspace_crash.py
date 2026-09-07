"""Disposable Qt crash scenarios and a supervisor that reaps its own children."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any


def _json_file(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".writing")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _boundary(directory: Path, phase: str) -> None:
    _json_file(directory / "boundary.json", {"phase": phase, "pid": os.getpid()})
    threading.Event().wait(120)
    raise RuntimeError("Supervisor did not terminate the GUI at the crash boundary")


def fault_worker(connection: Any, directory: Path, phase: str) -> None:
    """Use the real worker loop, pausing only the requested external write seam."""
    from gwexpy_studio.worker import service, signal_service

    if phase == "write_reply":

        def partial_write(value: Any, target: str, **kwargs: Any) -> None:
            del value, kwargs
            with Path(target).open("wb") as stream:
                stream.write(b"partial output awaiting confirmation\n")
                stream.flush()
                os.fsync(stream.fileno())
            _boundary(directory, phase)

        signal_service.write_data = partial_write
    service.worker_main(connection)


def _idle(window: Any, app: Any, errors: list[str]) -> None:
    from gwexpy_studio.ui.bridge import BridgeState

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        app.processEvents()
        if errors:
            raise AssertionError(errors)
        if window.bridge.state is BridgeState.IDLE and not window._command_reserved:
            return
        if window.bridge.state is BridgeState.FAILED:
            raise AssertionError(window.statusBar().currentMessage())
        time.sleep(0.01)
    raise AssertionError(
        f"GUI command timed out: {window.statusBar().currentMessage()}"
    )


def gui(directory: Path, phase: str) -> None:
    """Build a real Qt window and killable bridge over native scientific history."""
    from PySide6.QtWidgets import QApplication

    from gwexpy_studio.application.workspace_controller import WorkspaceController
    from gwexpy_studio.ui.bridge import WorkerBridge
    from gwexpy_studio.ui.window import MainWindow
    from gwexpy_studio.ui.workspace_state import capture_ui_state
    from gwexpy_studio.worker.client import WorkerClient

    app = QApplication([])
    controller = WorkspaceController(
        recovery_root=directory / "recovery",
        client=WorkerClient(
            worker_target=partial(fault_worker, directory=directory, phase=phase)
        ),
    )
    window = MainWindow(bridge=WorkerBridge(controller=controller))
    errors: list[str] = []
    window._show_error = lambda code, message: errors.append(f"{code}: {message}")
    window.show()
    window.initialize_workspace()
    _idle(window, app, errors)
    window.show_open_data()
    _idle(window, app, errors)
    panel = window.open_data_panel
    panel.paths_edit.setPlainText(str(directory / "入力.h5"))
    panel.format_combo.setEditText("hdf5")
    panel.inspect_button.click()
    _idle(window, app, errors)
    panel.confirm_button.click()
    _idle(window, app, errors)
    raw_id = window.project.objects[0].object_id
    window.show_parameter_panel("timeseries.lowpass")
    window.parameter_panel.fields["frequency"].setText("20")
    window.parameter_panel.apply_button.click()
    _idle(window, app, errors)
    assert window.project.history.cursor == 2
    window._dispatch_command("undo_analysis", {}, pending_action="undo_analysis")
    _idle(window, app, errors)
    assert window.project.history.cursor == 1
    project_path = directory / "保存済み.gwxproj"
    window.save_project_to(str(project_path))
    _idle(window, app, errors)
    (directory / "original-project.json").write_bytes(project_path.read_bytes())
    window.parameter_panel.fields["frequency"].setText("17.125")
    window._dispatch_command(
        "set_ui_state",
        {"ui_state": capture_ui_state(window)},
        pending_action="set_ui_state",
    )
    _idle(window, app, errors)
    _json_file(directory / "baseline.json", controller.project.to_dict())
    _json_file(
        directory / "owner.json",
        {
            "worker_pid": controller.session.client._process.pid,
            "run_id": controller._recovery.run_id,
            "gui_pid": os.getpid(),
        },
    )
    assert "gwexpy" not in sys.modules
    if phase == "analysis_intent":
        original_intent = controller._intent

        def intent(kind: str, details: Any) -> None:
            original_intent(kind, details)
            if kind == "analysis":
                _boundary(directory, phase)

        controller._intent = intent
        window.parameter_panel.apply_button.click()
    elif phase == "save_replace":
        original_replace = os.replace

        def replace(
            src: Any,
            dst: Any,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            if Path(dst) == project_path:
                _boundary(directory, phase)
            original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        os.replace = replace
        window.save_project_to(str(project_path))
    elif phase == "write_reply":
        window._dispatch_command(
            "write_data",
            {
                "object_id": raw_id,
                "request": {"paths": [str(directory / "partial.h5")], "format": "hdf5"},
            },
            pending_action="write_data",
        )
    else:
        raise AssertionError(phase)
    while True:
        app.processEvents()
        if errors:
            raise AssertionError(errors)
        time.sleep(0.01)


def source(directory: Path) -> None:
    """Create deterministic native input in a separate scientific process."""
    import numpy as np
    from gwexpy.timeseries import TimeSeries

    times = np.arange(1024) / 128
    TimeSeries(
        np.sin(2 * np.pi * 10 * times), dt=1 / 128, t0=0, unit="m", name="input"
    ).write(str(directory / "入力.h5"), format="hdf5")


def supervise(directory: Path, phase: str) -> None:
    """SIGKILL one owned GUI and reap its worker and tracker descendants."""
    assert sys.platform == "linux", "Crash acceptance requires Linux"
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    helper = str(Path(__file__).resolve())
    subprocess.run(
        [sys.executable, helper, "source", str(directory), phase], check=True
    )
    statuses: dict[int, int] = {}

    def reap() -> bool:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            if pid == 0:
                return False
            statuses[pid] = status

    with (directory / "gui.log").open("w") as log:
        parent = subprocess.Popen(
            [sys.executable, helper, "gui", str(directory), phase],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while not (directory / "boundary.json").exists():
                assert parent.poll() is None, (directory / "gui.log").read_text()
                assert time.monotonic() < deadline, (directory / "gui.log").read_text()
                time.sleep(0.02)
            owner = json.loads((directory / "owner.json").read_text())
            assert owner["gui_pid"] == parent.pid
            parent.kill()
            assert parent.wait(5) == -signal.SIGKILL
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not reap():
                time.sleep(0.02)
            assert reap(), "Owned processes remained after GUI SIGKILL"
            assert owner["worker_pid"] in statuses, "Worker was not reaped"
            assert (
                os.waitstatus_to_exitcode(statuses[owner["worker_pid"]])
                == -signal.SIGKILL
            )
            prefix = os.environ["GWEXPY_STUDIO_SHM_PREFIX"]
            assert not list(Path("/dev/shm").glob(prefix + "*"))
            print(
                json.dumps({"owned_processes": 0, "shared_memory": 0, "phase": phase})
            )
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not reap():
                time.sleep(0.02)


if __name__ == "__main__":
    mode, location, phase = sys.argv[1:]
    if mode == "source":
        source(Path(location))
    elif mode == "gui":
        gui(Path(location), phase)
    else:
        supervise(Path(location), phase)
