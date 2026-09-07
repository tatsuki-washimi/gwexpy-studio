"""Tests for tree, metadata, and history UI model projection."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QListWidget, QTreeWidget

from gwexpy_studio.domain.model import (
    DataObjectRef,
    DataSourceRef,
    ExecutionRecord,
    Operation,
)
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import OperationError
from gwexpy_studio.ui import bridge as bridge_module
from gwexpy_studio.ui.bridge import (
    BridgeCommand,
    BridgeResult,
    BridgeState,
    BridgeWorker,
    CommandKind,
    WorkerBridge,
)
from gwexpy_studio.ui.models import (
    populate_history_list,
    populate_metadata_tree,
    populate_source_tree,
)
from gwexpy_studio.ui.window import MainWindow


def _execution(
    execution_id: str,
    op_id: str,
    status: str,
    *,
    started_at: str,
    error: dict[str, Any] | None = None,
) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=execution_id,
        op_id=op_id,
        started_at=started_at,
        duration_s=0.1,
        status=status,
        error=error,
    )


class _WindowBridge(QObject):
    """Signal-only bridge used to deliver a result without starting a thread."""

    result_received = Signal(object)
    safe_to_destroy = Signal()
    state_changed = Signal(str)
    state = BridgeState.IDLE


class _FailingOperationController:
    """Append the record that a failed operation leaves on its project."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.session = SimpleNamespace(client=None)

    def _fail(self, op_id: str, code: str, message: str) -> None:
        self.project.executions = (
            *self.project.executions,
            _execution(
                f"exec-{len(self.project.executions) + 1}",
                op_id,
                "failed",
                started_at="2026-09-02T00:00:00Z",
                error={"code": code, "message": message},
            ),
        )
        raise OperationError(message, code=code)

    def load_source(self, inspection: object) -> None:
        del inspection
        self._fail("op-read", "read_failed", "read failed")

    def apply(self, op_name: str, input_id: str, params: dict[str, Any] | None) -> None:
        del op_name, input_id, params
        self._fail("op-crop", "crop_failed", "crop failed")


class _ProjectAccessController:
    """Count every project read across success, timeout, and cleanup paths."""

    def __init__(
        self,
        project: Project,
        *,
        load_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._project = project
        self._load_error = load_error
        self._close_error = close_error
        self.project_accesses = 0
        self.session = SimpleNamespace(client=None)
        self.source = object()
        self.loaded_object = object()
        self.applied_object = object()

    @property
    def project(self) -> Project:
        self.project_accesses += 1
        return self._project

    def start(self) -> None:
        pass

    def load_source(self, inspection: object) -> tuple[object, object]:
        del inspection
        if self._load_error is not None:
            raise self._load_error
        return self.source, self.loaded_object

    def apply(
        self, op_name: str, input_id: str, params: dict[str, Any] | None
    ) -> object:
        del op_name, input_id, params
        return self.applied_object

    def close(self) -> None:
        if self._close_error is not None:
            raise self._close_error


@pytest.fixture
def sample_project() -> Project:
    """Fixture providing a sample project with source, read op, and crop op."""
    project = Project(
        schema_version=1,
        project_id="proj-test",
        created="2026-08-31T00:00:00Z",
        modified="2026-08-31T00:00:00Z",
    )
    src = DataSourceRef(
        source_id="src-1",
        uri="/tmp/strain.csv",
        format="csv_enhanced",
        size_bytes=1024,
        mtime=123456.0,
    )
    project.sources = (src,)

    read_op = Operation(
        op_id="op-read",
        operation_id="timeseries.read",
        operation_schema=1,
        inputs={},
        params={"source": {"source_id": "src-1"}, "format": "csv_enhanced"},
        outputs=("obj-1",),
    )
    project.graph.add(read_op)

    obj1 = DataObjectRef(
        object_id="obj-1",
        kind="TimeSeries",
        shape=(1000,),
        dtype="float64",
        unit="m",
        name="H1:STRAIN",
        channel="H1:STRAIN",
        axes={
            "t0": {"value": 1126259462.0, "unit": "s"},
            "dt": {"value": 0.000244140625, "unit": "s"},
        },
        produced_by="op-read",
    )

    crop_op = Operation(
        op_id="op-crop",
        operation_id="timeseries.crop",
        operation_schema=1,
        inputs={"self": "obj-1"},
        params={"start": 0.0, "end": 0.5},
        outputs=("obj-2",),
    )
    project.graph.add(crop_op)

    obj2 = DataObjectRef(
        object_id="obj-2",
        kind="TimeSeries",
        shape=(500,),
        dtype="float64",
        unit="m",
        name="H1:STRAIN_cropped",
        channel="H1:STRAIN",
        axes={
            "t0": {"value": 1126259462.0, "unit": "s"},
            "dt": {"value": 0.000244140625, "unit": "s"},
        },
        produced_by="op-crop",
    )
    project.objects = (obj1, obj2)
    return project


def _assert_history_rows(project: Project) -> None:
    """Check row text, tuple authority, insertion order, and item identity."""
    list_widget = QListWidget()
    populate_history_list(list_widget, project)

    assert list_widget.count() == 2
    item0 = list_widget.item(0)
    assert item0 is not None
    assert item0.text() == "timeseries.read [not-run] (id=op-read)"
    assert item0.data(Qt.ItemDataRole.UserRole) == "op-read"

    item1 = list_widget.item(1)
    assert item1 is not None
    assert item1.text() == "timeseries.crop [not-run] (id=op-crop)"
    assert item1.data(Qt.ItemDataRole.UserRole) == "op-crop"

    project.executions = (
        _execution(
            "exec-read-failure",
            "op-read",
            "failed",
            started_at="2026-09-02T02:00:00Z",
            error={"code": "read_failed", "message": "stale failure"},
        ),
        _execution(
            "exec-read-success",
            "op-read",
            "succeeded",
            started_at="2026-09-02T01:00:00Z",
        ),
        _execution(
            "exec-crop-success",
            "op-crop",
            "succeeded",
            started_at="2026-09-02T04:00:00Z",
        ),
        _execution(
            "exec-crop-failure",
            "op-crop",
            "failed",
            started_at="2026-09-02T03:00:00Z",
            error={"code": "crop_failed", "message": "latest failure"},
        ),
    )
    populate_history_list(list_widget, project)

    latest_read = list_widget.item(0)
    latest_crop = list_widget.item(1)
    assert latest_read is not None
    assert latest_crop is not None
    assert latest_read.text() == "timeseries.read [succeeded] (id=op-read)"
    assert "stale failure" not in latest_read.text()
    assert latest_crop.text() == (
        "timeseries.crop [failed] (id=op-crop) code=crop_failed message=latest failure"
    )

    project.executions = (
        _execution(
            "exec-missing-error-fields",
            "op-read",
            "failed",
            started_at="2026-09-02T05:00:00Z",
            error={},
        ),
    )
    populate_history_list(list_widget, project)

    missing_error = list_widget.item(0)
    not_run = list_widget.item(1)
    assert missing_error is not None
    assert not_run is not None
    assert missing_error.text() == (
        "timeseries.read [failed] (id=op-read) code=<unknown> message=<none>"
    )
    assert not_run.text() == "timeseries.crop [not-run] (id=op-crop)"


def _assert_operation_failure_payloads(project: Project) -> None:
    """Check coded and generic operation failures carry the current project."""
    controller = _FailingOperationController(project)
    worker = BridgeWorker(controller=controller)  # type: ignore[arg-type]
    worker_results: list[BridgeResult] = []
    worker.result_ready.connect(worker_results.append)
    operation_commands: tuple[tuple[CommandKind, dict[str, Any]], ...] = (
        ("load", {"inspection": object()}),
        (
            "apply",
            {"op_name": "timeseries.crop", "input_id": "obj-1", "params": {}},
        ),
    )
    for kind, payload in operation_commands:
        worker.handle_command(
            BridgeCommand(
                command_id=str(uuid.uuid4()),
                kind=kind,
                payload=payload,
                deadline_monotonic=time.monotonic() + 1.0,
            )
        )
        assert not worker_results[-1].success
        assert worker_results[-1].payload == {"project": project}

    generic_controller = _ProjectAccessController(
        project, load_error=RuntimeError("generic load failed")
    )
    generic_worker = BridgeWorker(
        controller=generic_controller  # type: ignore[arg-type]
    )
    generic_results: list[BridgeResult] = []
    generic_worker.result_ready.connect(generic_results.append)
    generic_worker.handle_command(
        BridgeCommand(
            command_id=str(uuid.uuid4()),
            kind="load",
            payload={"inspection": object()},
            deadline_monotonic=time.monotonic() + 1.0,
        )
    )
    assert generic_results[-1].error_code == "operation_failed"
    assert generic_results[-1].error_message == "generic load failed"
    assert generic_results[-1].payload == {"project": project}
    assert generic_controller.project_accesses == 1


def _assert_timeout_cleanup_exclusions(
    qapp: QApplication,
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check excluded timeout and cleanup paths never read project state."""
    expired_controller = _ProjectAccessController(project)
    expired_worker = BridgeWorker(
        controller=expired_controller  # type: ignore[arg-type]
    )
    expired_results: list[BridgeResult] = []
    expired_worker.result_ready.connect(expired_results.append)
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(bridge_module.time, "monotonic", lambda: 2.0)
        expired_worker.handle_command(
            BridgeCommand(
                command_id=str(uuid.uuid4()),
                kind="load",
                payload={"inspection": object()},
                deadline_monotonic=1.0,
            )
        )
    assert expired_results[-1].error_code == "timeout"
    assert expired_results[-1].payload is None
    assert expired_controller.project_accesses == 0

    late_controller = _ProjectAccessController(project)
    late_worker = BridgeWorker(controller=late_controller)  # type: ignore[arg-type]
    late_results: list[BridgeResult] = []
    late_worker.result_ready.connect(late_results.append)
    clock_values = iter((1.0, 3.0))
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(bridge_module.time, "monotonic", lambda: next(clock_values))
        late_worker.handle_command(
            BridgeCommand(
                command_id=str(uuid.uuid4()),
                kind="load",
                payload={"inspection": object()},
                deadline_monotonic=2.0,
            )
        )
    assert late_results[-1].error_code == "timeout"
    assert late_results[-1].payload is None
    assert late_controller.project_accesses == 0

    timed_out_failure_controller = _ProjectAccessController(
        project,
        load_error=OperationError("late failure", code="operation_failed"),
    )
    timed_out_failure_worker = BridgeWorker(
        controller=timed_out_failure_controller  # type: ignore[arg-type]
    )
    timed_out_failure_results: list[BridgeResult] = []
    timed_out_failure_worker.result_ready.connect(timed_out_failure_results.append)
    clock_values = iter((1.0, 3.0))
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(bridge_module.time, "monotonic", lambda: next(clock_values))
        timed_out_failure_worker.handle_command(
            BridgeCommand(
                command_id=str(uuid.uuid4()),
                kind="load",
                payload={"inspection": object()},
                deadline_monotonic=2.0,
            )
        )
    assert timed_out_failure_results[-1].error_code == "timeout"
    assert timed_out_failure_results[-1].payload is None
    assert timed_out_failure_controller.project_accesses == 0

    cleanup_controller = _ProjectAccessController(
        project, close_error=RuntimeError("cleanup failed")
    )
    cleanup_worker = BridgeWorker(
        controller=cleanup_controller  # type: ignore[arg-type]
    )
    cleanup_outcomes: list[Any] = []
    cleanup_worker.close_finished.connect(cleanup_outcomes.append)
    cleanup_worker.close_owned(time.monotonic() + 1.0)
    assert cleanup_outcomes
    assert not cleanup_outcomes[-1].success
    assert cleanup_controller.project_accesses == 0

    facade_controller = _ProjectAccessController(project)
    facade = WorkerBridge(controller=facade_controller)  # type: ignore[arg-type]
    facade_results: list[BridgeResult] = []
    facade.result_received.connect(facade_results.append)
    # Exercise the facade-owned timer deterministically without racing a worker call.
    facade._state = BridgeState.RUNNING
    facade._current_command_id = str(uuid.uuid4())
    facade._current_deadline = time.monotonic() - 1.0
    facade._on_command_timeout()
    assert facade_results[-1].error_code == "timeout"
    assert facade_results[-1].payload is None
    assert facade_controller.project_accesses == 0
    facade_stop_deadline = time.monotonic() + 1.0
    while facade.worker_thread.isRunning() and time.monotonic() < facade_stop_deadline:
        qapp.processEvents()
        time.sleep(0.001)
    assert not facade.worker_thread.isRunning()


def _assert_success_payload_compatibility(project: Project) -> None:
    """Check successful project-bearing commands keep exact payload shapes."""
    controller = _ProjectAccessController(project)
    worker = BridgeWorker(controller=controller)  # type: ignore[arg-type]
    results: list[BridgeResult] = []
    worker.result_ready.connect(results.append)
    success_commands: tuple[tuple[CommandKind, dict[str, Any], dict[str, Any]], ...] = (
        ("start", {}, {"ready": True, "project": project}),
        (
            "load",
            {"inspection": object()},
            {
                "source": controller.source,
                "object": controller.loaded_object,
                "project": project,
            },
        ),
        (
            "apply",
            {"op_name": "timeseries.crop", "input_id": "obj-1", "params": {}},
            {"object": controller.applied_object, "project": project},
        ),
    )
    for kind, payload, expected_payload in success_commands:
        worker.handle_command(
            BridgeCommand(
                command_id=str(uuid.uuid4()),
                kind=kind,
                payload=payload,
                deadline_monotonic=time.monotonic() + 1.0,
            )
        )
        assert results[-1].success
        assert results[-1].payload == expected_payload
    assert controller.project_accesses == 3


def _assert_window_refresh_type_guard(qapp: QApplication, project: Project) -> None:
    """Check malformed failures stay visible and valid projects refresh views."""
    project.executions = (
        _execution(
            "exec-visible-failure",
            "op-read",
            "failed",
            started_at="2026-09-02T06:00:00Z",
            error={"code": "read_failed", "message": "visible failure"},
        ),
    )
    bridge = _WindowBridge()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        initial_project = window.project
        window._on_bridge_result(
            BridgeResult(
                command_id=str(uuid.uuid4()),
                success=False,
                payload=object(),
                error_code="malformed_payload",
                error_message="non-mapping payload",
            )
        )
        assert window.project is initial_project
        assert window.source_tree.topLevelItemCount() == 0
        assert window.history_list.count() == 0
        assert window.statusBar().currentMessage() == (
            "Error: [malformed_payload] non-mapping payload"
        )

        window._on_bridge_result(
            BridgeResult(
                command_id=str(uuid.uuid4()),
                success=False,
                payload={"project": object()},
                error_code="invalid_project",
                error_message="invalid project",
            )
        )
        assert window.project is initial_project
        assert window.source_tree.topLevelItemCount() == 0
        assert window.history_list.count() == 0
        assert window.statusBar().currentMessage() == (
            "Error: [invalid_project] invalid project"
        )

        bridge.result_received.emit(
            BridgeResult(
                command_id=str(uuid.uuid4()),
                success=False,
                payload={"project": project},
                error_code="read_failed",
                error_message="visible failure",
            )
        )

        assert window.project is project
        assert window.source_tree.topLevelItemCount() == 1
        assert window.history_list.count() == 2
        failure_item = window.history_list.item(0)
        assert failure_item is not None
        assert failure_item.text() == (
            "timeseries.read [failed] (id=op-read) "
            "code=read_failed message=visible failure"
        )
        assert window.statusBar().currentMessage() == (
            "Error: [read_failed] visible failure"
        )
    finally:
        window.deleteLater()
        qapp.processEvents()


class TestTreeMetadataHistoryProjection:
    """Validate data projections into Qt tree and list widgets."""

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-MOD-001")
    def test_source_tree_projection(
        self, qapp: QApplication, sample_project: Project
    ) -> None:
        """Sources appear as root items with child derived data objects."""
        tree = QTreeWidget()
        populate_source_tree(tree, sample_project)

        assert tree.topLevelItemCount() == 1
        src_item = tree.topLevelItem(0)
        assert src_item is not None
        assert src_item.text(0) == "/tmp/strain.csv"
        assert src_item.text(1) == "csv_enhanced"

        # Child objects (obj-1 and obj-2)
        assert src_item.childCount() == 2
        child0 = src_item.child(0)
        assert child0 is not None
        assert child0.text(0) == "obj-1"
        assert child0.text(1) == "TimeSeries"

        child1 = src_item.child(1)
        assert child1 is not None
        assert child1.text(0) == "obj-2"
        assert child1.text(1) == "TimeSeries"

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-MOD-002")
    def test_metadata_tree_projection(
        self, qapp: QApplication, sample_project: Project
    ) -> None:
        """Metadata widget displays all scalar properties and axes correctly."""
        tree = QTreeWidget()
        obj = sample_project.objects[0]
        populate_metadata_tree(tree, obj)

        props: dict[str, str] = {}
        for i in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(i)
            if item is not None:
                props[item.text(0)] = item.text(1)

        assert props["Object ID"] == "obj-1"
        assert props["Kind"] == "TimeSeries"
        assert props["Shape"] == "(1000,)"
        assert props["Dtype"] == "float64"
        assert props["Unit"] == "m"
        assert props["Name"] == "H1:STRAIN"
        assert props["Channel"] == "H1:STRAIN"
        assert props["Produced By"] == "op-read"
        assert "Axis: dt" in props
        assert "Axis: t0" in props

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-MOD-003")
    def test_history_list_projection(
        self,
        qapp: QApplication,
        sample_project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Latest tuple-ordered executions and failure projects reach history."""
        _assert_history_rows(sample_project)

        _assert_operation_failure_payloads(sample_project)

        _assert_timeout_cleanup_exclusions(qapp, sample_project, monkeypatch)

        _assert_success_payload_compatibility(sample_project)

        _assert_window_refresh_type_guard(qapp, sample_project)
