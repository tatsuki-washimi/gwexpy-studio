"""Lifecycle and shell layout tests for GWexpy Studio UI."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from gwexpy_studio.ops.source import SourceInspection
from gwexpy_studio.ui.app import create_app
from gwexpy_studio.ui.bridge import BridgeState, WorkerBridge
from gwexpy_studio.ui.window import MainWindow


def _spin_until(
    qapp: QApplication,
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.001)
    qapp.processEvents()
    assert predicate(), "condition did not become true before the test deadline"


class _SlowCloseController:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.session = type("Session", (), {"client": None})()
        self._entered = entered
        self._release = release
        self.close_threads: list[QThread] = []

    def close(self) -> None:
        self.close_threads.append(QThread.currentThread())
        self._entered.set()
        self._release.wait(1.0)


class _ShortCloseBridge(WorkerBridge):
    def close(self, timeout_s: float = 0.02) -> None:
        super().close(timeout_s=timeout_s)


class _BlockingLoadController:
    """Controller probe that keeps one load command active until released."""

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self.session = type("Session", (), {"client": None})()
        self._entered = entered
        self._release = release
        self.close_threads: list[QThread] = []

    def load_source(self, inspection: SourceInspection) -> tuple[None, None]:
        del inspection
        self._entered.set()
        self._release.wait(1.0)
        return None, None

    def close(self) -> None:
        self.close_threads.append(QThread.currentThread())


class _CountingWindow(MainWindow):
    def __init__(self, **kwargs: Any) -> None:
        self.close_events = 0
        super().__init__(**kwargs)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.close_events += 1
        super().closeEvent(event)


class TestUiLifecycle:
    """Validate QApplication startup, main window composition, and explicit close."""

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-LIFE-001")
    def test_application_and_main_window_startup(self, qapp: QApplication) -> None:
        """Create application and main window instance under offscreen session."""
        window = MainWindow()
        window.show()
        qapp.processEvents()

        assert window.isVisible()
        assert window.windowTitle() == "GWexpy Studio"

        # Verify docked widgets and central placeholder
        assert window.central_placeholder is not None
        assert window.source_dock is not None
        assert window.metadata_dock is not None
        assert window.history_dock is not None
        assert window.statusBar() is not None

        # Verify actions
        assert window.open_action is not None
        assert window.export_action is not None
        assert window.close_action is not None

        # Clean close
        window.close()
        window.deleteLater()
        qapp.processEvents()
        assert not window.isVisible()

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-LIFE-002")
    def test_create_app_factory(self, qapp: QApplication) -> None:
        """create_app returns existing session qapp and configured MainWindow."""
        app, window = create_app([])
        assert app is qapp
        assert isinstance(window, MainWindow)

        window.close()
        window.deleteLater()
        qapp.processEvents()

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-LIFE-003")
    def test_close_waits_for_safe_to_destroy(self, qapp: QApplication) -> None:
        """A normal close waits for worker-thread stop, then retries once."""
        assert hasattr(WorkerBridge, "safe_to_destroy")

        quick_release = threading.Event()
        quick_release.set()
        quick_bridge = _ShortCloseBridge(
            controller=_SlowCloseController(threading.Event(), quick_release)
        )
        quick_window = _CountingWindow(bridge=quick_bridge)
        quick_window.show()
        qapp.processEvents()
        quick_event = QCloseEvent()
        quick_window.closeEvent(quick_event)
        assert not quick_event.isAccepted()
        _spin_until(qapp, lambda: not quick_window.isVisible())
        assert quick_window.close_events == 2
        assert not quick_bridge.worker_thread.isRunning()
        quick_window.deleteLater()
        qapp.processEvents()

        entered = threading.Event()
        release = threading.Event()
        controller = _SlowCloseController(entered, release)
        bridge = _ShortCloseBridge(controller=controller)  # type: ignore[arg-type]
        window = _CountingWindow(bridge=bridge)
        safe_states: list[bool] = []
        bridge.safe_to_destroy.connect(
            lambda: safe_states.append(bridge.worker_thread.isRunning())
        )
        window.show()
        qapp.processEvents()

        window.close()
        assert entered.wait(0.5)
        _spin_until(qapp, lambda: bridge.state is BridgeState.FAILED)
        assert window.isVisible()
        assert bridge.worker_thread.isRunning()
        assert window.close_events == 1
        assert safe_states == []

        release.set()
        _spin_until(qapp, lambda: not window.isVisible())
        assert safe_states == [False]
        assert not bridge.worker_thread.isRunning()
        assert controller.close_threads == [bridge.worker_thread]
        assert window.close_events == 2
        window.deleteLater()
        qapp.processEvents()

        load_entered = threading.Event()
        load_release = threading.Event()
        load_controller = _BlockingLoadController(load_entered, load_release)
        load_bridge = _ShortCloseBridge(controller=load_controller)  # type: ignore[arg-type]
        load_window = _CountingWindow(bridge=load_bridge)
        load_window.show()
        qapp.processEvents()
        load_bridge.send_command(
            "load",
            {
                "inspection": SourceInspection(
                    exists=True,
                    size_bytes=1,
                    mtime=1.0,
                    format_guess="csv_enhanced",
                    resolved_uri="/tmp/blocked.csv",
                    device=1,
                    inode=2,
                    mtime_ns=3,
                )
            },
        )
        assert load_entered.wait(0.5)
        load_window.close()
        assert load_window.isVisible()
        load_release.set()
        _spin_until(qapp, lambda: not load_window.isVisible())
        assert load_controller.close_threads == [load_bridge.worker_thread]
        assert not load_bridge.worker_thread.isRunning()
        load_window.deleteLater()
        qapp.processEvents()
