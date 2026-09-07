"""Stopped-thread ownership transfer and detached document error responses."""

import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PySide6.QtTest import QTest

from gwexpy_studio.application.project_factory import create_alpha_project
from gwexpy_studio.errors import OperationError
from gwexpy_studio.ui.bridge import BridgeState, WorkerBridge
from gwexpy_studio.ui.window import MainWindow


class FailingWorkspace:
    """Minimal document controller with an observable worker failure."""

    def __init__(self):
        """Initialize independent test state."""
        self.project = create_alpha_project()
        self.session = SimpleNamespace(
            client=SimpleNamespace(
                capability_snapshot=None,
                deadline_scope=lambda _deadline: nullcontext(),
            )
        )
        self.resumed = 0
        self.closed = 0
        self.started = 0

    def workspace_status(self):
        """Return a deterministic detached workspace status."""
        return {
            "generation": "document",
            "revision": 1,
            "dirty": False,
            "needs_restore": True,
            "resident_object_ids": [],
        }

    def resume_workspace(self):
        """Record a new bridge ownership period."""
        self.resumed += 1

    def start(self):
        """Fail once, then supply the replacement worker's valid capability stamp."""
        self.started += 1
        if self.started == 1:
            raise OperationError("Worker crashed", code="worker_crashed")
        from gwexpy_studio.ops.io_capabilities import (
            CapabilityManifest,
            unprobed_effective_capabilities,
        )

        self.session.client.capability_snapshot = unprobed_effective_capabilities(
            CapabilityManifest(mode="developer")
        )

    def close(self):
        """Record cleanup without creating a data worker."""
        self.closed += 1

    def review_restore(self, **kwargs):
        """Check the immutable deadline and emit restore progress."""
        assert kwargs["deadline_monotonic"] > time.monotonic()
        kwargs["progress"]({"completed": 1, "total": 1, "label": "Reviewed"})
        return {"message": "Reviewed"}


def wait_until(qapp, predicate):
    """Process Qt events until a bounded transition completes."""
    until = time.monotonic() + 5
    while time.monotonic() < until:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    pytest.fail("Qt bridge transition did not finish")


@pytest.mark.contract("GUI-WSP-0001")
@pytest.mark.gui
def test_worker_failure_keeps_detached_document_then_transfers_stopped_owner(
    qapp, monkeypatch
):
    """Worker failure keeps detached document then transfers stopped owner."""
    controller = FailingWorkspace()
    bridge = WorkerBridge(controller=controller)
    window = MainWindow(bridge=bridge)
    reviewed = []
    monkeypatch.setattr(window, "_review_restore_response", reviewed.append)
    try:
        with pytest.raises(OperationError, match="previous|old"):
            bridge.take_controller()
        window._dispatch_command("start", {}, pending_action="start")
        wait_until(qapp, lambda: not bridge.worker_thread.isRunning())
        assert controller.closed == 1
        assert window.project is not controller.project
        assert window.project.project_id == controller.project.project_id
        assert window._workspace_status["needs_restore"]
        assert window.restore_project_action.isEnabled()
        window.restore_project_action.trigger()
        wait_until(qapp, lambda: bool(reviewed))
        assert window.bridge is not bridge
        assert controller.started == 2
        assert controller.resumed == 2
        assert window.bridge.state is BridgeState.IDLE
        assert reviewed == [{"message": "Reviewed"}]
    finally:
        window._workspace_close_authorized = True
        window.close()
        qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0002")
@pytest.mark.gui
def test_bridge_worker_snapshot_and_restore_deadline_are_detached(qapp):
    """Bridge worker snapshot and restore deadline are detached."""
    import uuid

    from gwexpy_studio.ui.bridge import BridgeCommand, BridgeWorker

    controller = FailingWorkspace()
    worker = BridgeWorker(controller=controller)
    results, progress = [], []
    worker.result_ready.connect(results.append)
    worker.progress_ready.connect(progress.append)
    for kind in ("start", "review_restore"):
        command = BridgeCommand(
            command_id=str(uuid.uuid4()),
            kind=kind,
            payload={},
            deadline_monotonic=time.monotonic() + 5,
        )
        worker.handle_command(command)
    assert not results[0].success
    assert results[1].success
    assert results[1].payload["project"] is not controller.project
    assert results[0].payload["project"].project_id == controller.project.project_id
    assert progress[0]["command_id"] == results[1].command_id
    controller.project.ui_state = {"new": True}
    assert results[1].payload["project"].ui_state == {}
    worker.clean_workspace = True
    worker.close_owned(time.monotonic() + 5)
    worker.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0003")
@pytest.mark.gui
def test_bridge_flushes_ui_state_before_scientific_gesture(qapp):
    """The gesture sees the selection and drafts from its own submitted frame."""
    import uuid

    from gwexpy_studio.ui.bridge import BridgeCommand, BridgeWorker

    controller = FailingWorkspace()
    order = []
    controller.set_ui_state = lambda value: order.append(("ui", value))
    controller.apply = lambda *args: order.append(("apply", args))
    worker = BridgeWorker(controller=controller)
    worker.handle_command(
        BridgeCommand(
            command_id=str(uuid.uuid4()),
            kind="apply",
            payload={
                "op_name": "timeseries.lowpass",
                "input_id": "a",
                "ui_state": {"selection": {"object_id": "a"}},
            },
            deadline_monotonic=time.monotonic() + 5,
        )
    )
    assert [item[0] for item in order] == ["ui", "apply"]
    assert order[0][1]["selection"]["object_id"] == "a"
    worker.deleteLater()
    qapp.processEvents()
