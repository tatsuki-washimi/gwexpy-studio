"""Unit tests for AlphaController application logic."""

from __future__ import annotations

import gc
import sys
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

import gwexpy_studio.application.alpha as alpha_module
from gwexpy_studio.application.alpha import AlphaController
from gwexpy_studio.domain.model import DataObjectRef, DataSourceRef, Operation
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import OperationError, SharedMemoryError
from gwexpy_studio.ops.source import SourceInspection
from gwexpy_studio.session import StudioSession
from gwexpy_studio.worker.client import WorkerClient, WorkerLifecycle
from gwexpy_studio.worker.shm import (
    FetchedArray,
    SharedMemoryBlock,
    SharedMemoryDescriptor,
    SharedMemoryPreview,
    attach_block,
    create_block,
    release_block,
)


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    p = tmp_path / "data.csv"
    p.write_text("time,value\n0.0,1.0\n0.1,2.0\n0.2,3.0\n", encoding="utf-8")
    return p


@pytest.fixture
def hdf5_file(tmp_path: Path) -> Path:
    p = tmp_path / "data.h5"
    p.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\x00" * 32)
    return p


class TestAlphaControllerInspection:
    """Inspection contracts."""

    @pytest.mark.contract("C-APP-001")
    def test_inspect_source_valid_csv(self, csv_file: Path) -> None:
        controller = AlphaController()
        insp = controller.inspect_source(str(csv_file))
        assert insp.exists is True
        assert insp.format_guess == "csv_enhanced"
        assert insp.resolved_uri == str(csv_file.resolve())
        assert insp.size_bytes == csv_file.stat().st_size
        assert len(controller.project.sources) == 0
        assert len(controller.project.graph.operations) == 0

    @pytest.mark.contract("C-APP-002")
    def test_inspect_source_rejects_directory(self, tmp_path: Path) -> None:
        controller = AlphaController()
        with pytest.raises(IsADirectoryError):
            controller.inspect_source(str(tmp_path))

    @pytest.mark.contract("C-APP-003")
    def test_inspect_source_rejects_empty_uri(self) -> None:
        controller = AlphaController()
        with pytest.raises(ValueError):
            controller.inspect_source("")


class TestAlphaControllerLoadSource:
    """Load source contracts and security guards."""

    @pytest.mark.contract("C-APP-004")
    def test_load_source_rejects_hdf5_with_guard_code(self, hdf5_file: Path) -> None:
        controller = AlphaController()
        insp = controller.inspect_source(str(hdf5_file))
        with pytest.raises(OperationError) as exc_info:
            controller.load_source(insp)
        assert exc_info.value.code == "hdf5_schema_guard_unavailable"

    @pytest.mark.contract("C-APP-005")
    def test_load_source_rejects_script_file(self, tmp_path: Path) -> None:
        script = tmp_path / "test.py"
        script.write_text("print('hello')", encoding="utf-8")
        insp = SourceInspection(
            exists=True,
            size_bytes=script.stat().st_size,
            mtime=script.stat().st_mtime,
            format_guess="csv_enhanced",
            resolved_uri=str(script.resolve()),
            device=script.stat().st_dev,
            inode=script.stat().st_ino,
            mtime_ns=script.stat().st_mtime_ns,
        )
        controller = AlphaController()
        with pytest.raises(OperationError) as exc_info:
            controller.load_source(insp)
        assert exc_info.value.code == "invalid_source_format"

    @pytest.mark.contract("C-APP-006")
    def test_load_source_rejects_missing_identity(self, csv_file: Path) -> None:
        insp = SourceInspection(
            exists=True,
            size_bytes=csv_file.stat().st_size,
            mtime=csv_file.stat().st_mtime,
            format_guess="csv_enhanced",
            resolved_uri=str(csv_file.resolve()),
            device=None,
            inode=None,
            mtime_ns=None,
        )
        controller = AlphaController()
        with pytest.raises(OperationError) as exc_info:
            controller.load_source(insp)
        assert exc_info.value.code == "inspection_identity_unavailable"

    @pytest.mark.contract("C-APP-007")
    def test_load_source_rejects_modified_file(self, csv_file: Path) -> None:
        controller = AlphaController()
        insp = controller.inspect_source(str(csv_file))
        # Modify file after inspection
        csv_file.write_text("time,value\n0,1\n1,2\n2,3\n3,4\n", encoding="utf-8")
        with pytest.raises(OperationError) as exc_info:
            controller.load_source(insp)
        assert exc_info.value.code == "source_changed"

    @pytest.mark.contract("C-APP-008")
    def test_load_source_success(self, csv_file: Path) -> None:
        session_mock = MagicMock(spec=StudioSession)
        project = Project(
            schema_version=1,
            project_id="proj-1",
            created="now",
            modified="now",
        )
        controller = AlphaController(project=project, session=session_mock)
        insp = controller.inspect_source(str(csv_file))

        def fake_replay(targets: tuple[str, ...]) -> dict[str, Any]:
            target_id = targets[0]
            obj = DataObjectRef(
                object_id=target_id,
                kind="TimeSeries",
                shape=(3,),
                dtype="float64",
                unit="m",
                axes={
                    "t0": {"value": 0.0, "unit": "s"},
                    "dt": {"value": 0.1, "unit": "s"},
                },
                produced_by=project.graph.operations[0].op_id,
            )
            project.objects = (*project.objects, obj)
            return {"object_ids": (target_id,)}

        session_mock.replay.side_effect = fake_replay

        src_ref, obj_ref = controller.load_source(insp)
        assert src_ref.uri == str(csv_file.resolve())
        assert src_ref.format == "csv_enhanced"
        assert obj_ref.produced_by == project.graph.operations[0].op_id
        assert len(project.sources) == 1
        assert len(project.graph.operations) == 1
        assert project.graph.operations[0].operation_id == "timeseries.read"


class TestAlphaControllerOperations:
    """Operations and export contracts."""

    @pytest.mark.contract("C-APP-009")
    def test_apply_operation_success(self) -> None:
        project = Project(
            schema_version=1,
            project_id="proj-1",
            created="now",
            modified="now",
        )
        input_obj = DataObjectRef(
            object_id="obj-in",
            kind="TimeSeries",
            shape=(100,),
            dtype="float64",
            unit="m",
            axes={
                "t0": {"value": 0.0, "unit": "s"},
                "dt": {"value": 0.01, "unit": "s"},
            },
        )
        project.objects = (input_obj,)

        session_mock = MagicMock(spec=StudioSession)

        def fake_replay(targets: tuple[str, ...]) -> dict[str, Any]:
            target_id = targets[0]
            out_obj = DataObjectRef(
                object_id=target_id,
                kind="TimeSeries",
                shape=(50,),
                dtype="float64",
                unit="m",
                axes={
                    "t0": {"value": 0.0, "unit": "s"},
                    "dt": {"value": 0.01, "unit": "s"},
                },
                produced_by=project.graph.operations[-1].op_id,
            )
            project.objects = (*project.objects, out_obj)
            return {"object_ids": (target_id,)}

        session_mock.replay.side_effect = fake_replay
        controller = AlphaController(project=project, session=session_mock)

        result_ref = controller.apply(
            "timeseries.crop", "obj-in", {"start": 0.0, "end": 0.5}
        )
        assert result_ref.object_id.startswith("obj-")
        assert len(project.graph.operations) == 1
        assert project.graph.operations[0].operation_id == "timeseries.crop"
        assert project.graph.operations[0].inputs == {"self": "obj-in"}

    @pytest.mark.contract("C-APP-010")
    def test_apply_unknown_operation_rejected(self) -> None:
        controller = AlphaController()
        with pytest.raises(OperationError) as exc_info:
            controller.apply("timeseries.unknown_op", "obj-1")
        assert exc_info.value.code == "invalid_operation"

    @pytest.mark.contract("C-APP-011")
    def test_apply_missing_input_rejected(self) -> None:
        controller = AlphaController()
        with pytest.raises(OperationError) as exc_info:
            controller.apply("timeseries.crop", "obj-nonexistent")
        assert exc_info.value.code == "object_not_found"

    @pytest.mark.contract("C-APP-012")
    def test_export_script(self, tmp_path: Path) -> None:
        project = Project(
            schema_version=1,
            project_id="proj-1",
            created="now",
            modified="now",
        )
        src = DataSourceRef(
            source_id="src-1",
            uri="/tmp/test.csv",
            format="csv_enhanced",
            size_bytes=100,
            mtime=123.0,
        )
        project.sources = (src,)
        op = Operation(
            op_id="op-1",
            operation_id="timeseries.read",
            operation_schema=1,
            inputs={},
            params={"source": {"source_id": "src-1"}, "format": "csv_enhanced"},
            outputs=("obj-1",),
        )
        project.graph.add(op)
        obj = DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(10,),
            dtype="float64",
            unit="m",
            axes={
                "t0": {"value": 0.0, "unit": "s"},
                "dt": {"value": 0.1, "unit": "s"},
            },
            produced_by="op-1",
        )
        project.objects = (obj,)
        controller = AlphaController(project=project)
        out_file = tmp_path / "export.py"
        controller.export_script(out_file)
        assert out_file.is_file()
        content = out_file.read_text(encoding="utf-8")
        assert "TimeSeries.read" in content
        assert "/tmp/test.csv" in content


class _CopyFailure(RuntimeError):
    pass


class _HandleCloseFailure(RuntimeError):
    pass


class _ReleaseFailure(RuntimeError):
    pass


class _SessionCloseFailure(RuntimeError):
    pass


class _DiagnosticConstructionFailure(RuntimeError):
    pass


class _HostileStringFailure(RuntimeError):
    def __str__(self) -> str:
        raise _DiagnosticConstructionFailure("diagnostic __str__ exploded")


class _HostileNoteFailure(RuntimeError):
    def add_note(self, note: str) -> None:
        del note
        raise _DiagnosticConstructionFailure("diagnostic add_note exploded")


class _PreviewHarness:
    """Build focused preview scenarios without adding collected test nodes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.project = Project(
            schema_version=1,
            project_id="proj-1",
            created="now",
            modified="now",
        )
        self.obj = DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(10,),
            dtype="float64",
            unit="m",
            axes={
                "t0": {"value": 0.0, "unit": "s"},
                "dt": {"value": 0.1, "unit": "s"},
            },
        )
        self.project.objects = (self.obj,)
        self.values = np.arange(10, dtype=np.float64)
        self.original_copy = alpha_module.np.ascontiguousarray

    def fetched(
        self,
        handle: Any,
        *,
        name: str = "shm-preview-test",
        descriptor: SharedMemoryDescriptor | None = None,
    ) -> FetchedArray:
        actual_descriptor = descriptor or SharedMemoryDescriptor(
            name=name,
            dtype=self.values.dtype.name,
            shape=self.values.shape,
            nbytes=self.values.nbytes,
            unit="m",
        )
        return FetchedArray(
            descriptor=actual_descriptor,
            preview=SharedMemoryPreview(
                values=self.values,
                coordinates=np.arange(self.values.size),
                handle=handle,
            ),
            unit="m",
        )

    def controller(
        self, fetched: FetchedArray
    ) -> tuple[AlphaController, MagicMock, MagicMock]:
        client = MagicMock()
        client.state.value = "running"
        client.fetch_array.return_value = fetched
        session = MagicMock(spec=StudioSession)
        session.client = client
        controller = AlphaController(project=self.project, session=session)
        return controller, client, session

    def real_session_controller(
        self, fetched: FetchedArray
    ) -> tuple[AlphaController, MagicMock]:
        client = MagicMock(spec=WorkerClient)
        client.state = WorkerLifecycle.RUNNING
        client.fetch_array.return_value = fetched
        session = StudioSession(
            project=self.project,
            graph=self.project.graph,
            client=client,
        )
        return AlphaController(project=self.project, session=session), client

    def fetch(
        self, controller: AlphaController, copy_function: Callable[[Any], Any]
    ) -> Any:
        with self.monkeypatch.context() as patch:
            patch.setattr(alpha_module.np, "ascontiguousarray", copy_function)
            return controller.fetch_preview("obj-1")


class _PreviewScenario:
    """Arrange one synthetic cleanup sequence for the contract assertions."""

    def __init__(
        self,
        harness: _PreviewHarness,
        *,
        name: str,
        copy_failure: BaseException | None = None,
        close_failure: BaseException | None = None,
        release_failure: BaseException | None = None,
        shutdown_failure: BaseException | None = None,
    ) -> None:
        self.harness = harness
        self.copy_failure = copy_failure
        self.close_failure = close_failure
        self.release_failure = release_failure
        self.shutdown_failure = shutdown_failure
        self.events: list[str] = []
        handle = MagicMock()
        handle.close.side_effect = self._close
        self.controller, self.client, self.session = harness.controller(
            harness.fetched(handle, name=name)
        )
        self.client.release_array.side_effect = self._release
        self.session.close.side_effect = self._shutdown

    def _copy(self, values: Any) -> Any:
        self.events.append("copy")
        if self.copy_failure is not None:
            raise self.copy_failure
        return self.harness.original_copy(values)

    def _close(self) -> None:
        self.events.append("close")
        if self.close_failure is not None:
            raise self.close_failure

    def _release(self, _name: str) -> None:
        self.events.append("release")
        if self.release_failure is not None:
            raise self.release_failure

    def _shutdown(self) -> None:
        self.events.append("session_close")
        if self.shutdown_failure is not None:
            raise self.shutdown_failure

    def run(self) -> Any:
        return self.harness.fetch(self.controller, self._copy)


@contextmanager
def _owned_preview_block(values: Any) -> Iterator[SharedMemoryBlock]:
    """Own a real SHM block from creation through unlink and local close."""
    block: SharedMemoryBlock | None = None
    try:
        block = create_block(values)
        yield block
    finally:
        if block is not None:
            try:
                release_block(block.descriptor.name)
            finally:
                block.shm.close()


def _assert_owned_preview_block_acquisition_interrupt(harness: _PreviewHarness) -> None:
    acquired: list[SharedMemoryBlock] = []
    interrupt = KeyboardInterrupt("post-acquisition interrupt")
    target_code = getattr(_owned_preview_block, "__wrapped__").__code__

    def trace(frame: Any, event: str, _argument: Any) -> Any:
        block = frame.f_locals.get("block")
        if (
            frame.f_code is target_code
            and event == "line"
            and isinstance(block, SharedMemoryBlock)
        ):
            acquired.append(block)
            raise interrupt
        return trace

    previous_trace = sys.gettrace()
    sys.settrace(trace)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            with _owned_preview_block(harness.values):
                pytest.fail("post-acquisition interrupt was not injected")
    finally:
        sys.settrace(previous_trace)
    assert caught.value is interrupt

    block = acquired[0]
    owner_closed = getattr(block.shm, "_fd", None) == -1
    try:
        with pytest.raises(SharedMemoryError) as reattach_error:
            attached = attach_block(block.descriptor)
            attached.handle.close()
    finally:
        try:
            release_block(block.descriptor.name)
        finally:
            block.shm.close()
    assert reattach_error.value.code == "shm_not_found" and owner_closed


def _note_count(error: BaseException, text: str) -> int:
    return sum(text in note for note in getattr(error, "__notes__", ()))


def _assert_successful_preview_release(harness: _PreviewHarness) -> None:
    events: list[str] = []
    with _owned_preview_block(harness.values) as block:
        handle = MagicMock()
        handle.close.side_effect = lambda: events.append("close")
        fetched = harness.fetched(handle, descriptor=block.descriptor)
        controller, client, session = harness.controller(fetched)

        def copy(values: Any) -> Any:
            events.append("copy")
            return harness.original_copy(values)

        def release(name: str) -> None:
            events.append("release")
            release_block(name)

        client.release_array.side_effect = release
        preview = harness.fetch(controller, copy)

        assert events == ["copy", "close", "release"]
        assert np.array_equal(preview.values, harness.values)
        assert preview.values.flags.writeable is False
        client.release_array.assert_called_once_with(block.descriptor.name)
        session.close.assert_not_called()
        with pytest.raises(SharedMemoryError) as reattach_error:
            attach_block(block.descriptor)
        assert reattach_error.value.code == "shm_not_found"


def _assert_ordinary_pre_release_failures(harness: _PreviewHarness) -> None:
    copy_primary = _CopyFailure("copy failed")
    combined_primary = _CopyFailure("combined copy failed")
    combined_close = _HandleCloseFailure("combined close failed")
    close_primary = _HandleCloseFailure("close failed")
    cases = (
        ("shm-copy-failure", copy_primary, None, copy_primary),
        ("shm-combined-failure", combined_primary, combined_close, combined_primary),
        ("shm-close-failure", None, close_primary, close_primary),
    )
    for name, copy_failure, close_failure, expected in cases:
        scenario = _PreviewScenario(
            harness,
            name=name,
            copy_failure=copy_failure,
            close_failure=close_failure,
        )
        with pytest.raises((_CopyFailure, _HandleCloseFailure)) as error:
            scenario.run()

        assert error.value is expected
        assert scenario.events == ["copy", "close", "release"]
        scenario.client.release_array.assert_called_once_with(name)
        scenario.session.close.assert_not_called()

    assert _note_count(combined_primary, "combined close failed") == 1


def _assert_release_failure_closes_real_session(harness: _PreviewHarness) -> None:
    events: list[str] = []
    release_failure = _ReleaseFailure("release RPC failed")
    with _owned_preview_block(harness.values) as block:
        handle = MagicMock()
        handle.close.side_effect = lambda: events.append("close")
        fetched = harness.fetched(handle, descriptor=block.descriptor)
        controller, client = harness.real_session_controller(fetched)
        tracked_copies: list[Any] = []

        class TrackedContiguous:
            def copy(self) -> Any:
                copied = harness.values.copy()
                tracked_copies.append(weakref.ref(copied))
                return copied

        def copy(_values: Any) -> TrackedContiguous:
            events.append("copy")
            return TrackedContiguous()

        def release(_name: str) -> None:
            events.append("release")
            raise release_failure

        def worker_shutdown() -> None:
            events.append("session_close")
            release_block(block.descriptor.name)

        client.release_array.side_effect = release
        # This is a real StudioSession.close path. The client remains a unit
        # seam whose shutdown side effect models worker-owned final unlink.
        client.shutdown.side_effect = worker_shutdown
        with pytest.raises(OperationError) as error:
            harness.fetch(controller, copy)

        assert error.value.code == "preview_release_failed"
        assert error.value.__cause__ is release_failure
        assert events == ["copy", "close", "release", "session_close"]
        client.release_array.assert_called_once_with(block.descriptor.name)
        client.shutdown.assert_called_once_with()
        gc.collect()
        assert len(tracked_copies) == 1
        assert tracked_copies[0]() is None
        with pytest.raises(SharedMemoryError) as reattach_error:
            attach_block(block.descriptor)
        assert reattach_error.value.code == "shm_not_found"


def _assert_ordinary_failure_precedence(harness: _PreviewHarness) -> None:
    for fail_during_copy in (True, False):
        earlier_failure: BaseException
        if fail_during_copy:
            earlier_failure = _CopyFailure("copy before release failed")
        else:
            earlier_failure = _HandleCloseFailure("close before release failed")
        cleanup_close = _HandleCloseFailure("priority cleanup close failed")
        release_failure = _ReleaseFailure("priority release failed")
        shutdown_failure = _SessionCloseFailure("priority session close failed")
        scenario = _PreviewScenario(
            harness,
            name="shm-priority-failure",
            copy_failure=earlier_failure if fail_during_copy else None,
            close_failure=cleanup_close if fail_during_copy else earlier_failure,
            release_failure=release_failure,
            shutdown_failure=shutdown_failure,
        )
        with pytest.raises((_CopyFailure, _HandleCloseFailure)) as error:
            scenario.run()

        assert error.value is earlier_failure
        assert _note_count(earlier_failure, "priority cleanup close failed") == int(
            fail_during_copy
        )
        assert _note_count(earlier_failure, "priority release failed") == 1
        assert _note_count(earlier_failure, "priority session close failed") == 1
        assert scenario.events == ["copy", "close", "release", "session_close"]
        scenario.client.release_array.assert_called_once_with("shm-priority-failure")
        scenario.session.close.assert_called_once_with()


def _assert_control_flow_precedence(harness: _PreviewHarness) -> None:
    for stage in ("copy", "close", "release", "session_close"):
        for failure_type in (KeyboardInterrupt, SystemExit):
            failure = failure_type(f"{stage} control flow")
            release_failure: BaseException | None = None
            if stage == "release":
                release_failure = failure
            elif stage == "session_close":
                release_failure = RuntimeError("release before control-flow shutdown")
            scenario = _PreviewScenario(
                harness,
                name=f"shm-control-{stage}-{failure_type.__name__}",
                copy_failure=failure if stage == "copy" else None,
                close_failure=failure if stage == "close" else None,
                release_failure=release_failure,
                shutdown_failure=failure if stage == "session_close" else None,
            )
            with pytest.raises((KeyboardInterrupt, SystemExit)) as error:
                scenario.run()

            assert error.value is failure
            expected_events = ["copy", "close", "release"]
            if stage in ("release", "session_close"):
                expected_events.append("session_close")
                scenario.session.close.assert_called_once_with()
            else:
                scenario.session.close.assert_not_called()
            assert scenario.events == expected_events
            scenario.client.release_array.assert_called_once()

    ordinary_copy = RuntimeError("ordinary copy before control flow")
    earliest_control = KeyboardInterrupt("earliest cleanup control flow")
    later_control = SystemExit("later release control flow")
    ordinary_shutdown = RuntimeError("ordinary shutdown cleanup")
    mixed = _PreviewScenario(
        harness,
        name="shm-mixed-control-flow",
        copy_failure=ordinary_copy,
        close_failure=earliest_control,
        release_failure=later_control,
        shutdown_failure=ordinary_shutdown,
    )
    with pytest.raises(KeyboardInterrupt) as error:
        mixed.run()

    assert error.value is earliest_control
    assert _note_count(earliest_control, "ordinary copy before control flow") == 1
    assert _note_count(earliest_control, "later release control flow") == 1
    assert _note_count(earliest_control, "ordinary shutdown cleanup") == 1
    assert mixed.events == ["copy", "close", "release", "session_close"]
    mixed.client.release_array.assert_called_once_with("shm-mixed-control-flow")
    mixed.session.close.assert_called_once_with()


def _assert_hostile_diagnostics(harness: _PreviewHarness) -> None:
    primary = RuntimeError("primary before hostile string")
    scenario = _PreviewScenario(
        harness,
        name="shm-hostile-string",
        copy_failure=primary,
        close_failure=_HostileStringFailure(),
        release_failure=RuntimeError("release after hostile string"),
    )
    with pytest.raises(RuntimeError) as error:
        scenario.run()

    assert error.value is primary
    assert scenario.events == ["copy", "close", "release", "session_close"]
    assert _note_count(primary, "_HostileStringFailure") == 1
    assert _note_count(primary, "release after hostile string") == 1
    scenario.client.release_array.assert_called_once_with("shm-hostile-string")
    scenario.session.close.assert_called_once_with()

    hostile_note = _HostileNoteFailure("hostile note primary")
    note_scenario = _PreviewScenario(
        harness,
        name="shm-hostile-note",
        copy_failure=hostile_note,
        release_failure=RuntimeError("release after hostile add_note"),
    )
    with pytest.raises(_HostileNoteFailure) as note_error:
        note_scenario.run()

    assert note_error.value is hostile_note
    assert note_scenario.events == ["copy", "close", "release", "session_close"]
    note_scenario.client.release_array.assert_called_once_with("shm-hostile-note")
    note_scenario.session.close.assert_called_once_with()


class TestAlphaControllerPreview:
    """Preview fetch and SHM lifecycle contracts."""

    @pytest.mark.contract("C-APP-013")
    def test_fetch_preview_lifecycle(self) -> None:
        project = Project(
            schema_version=1,
            project_id="proj-1",
            created="now",
            modified="now",
        )
        obj = DataObjectRef(
            object_id="obj-1",
            kind="TimeSeries",
            shape=(10,),
            dtype="float64",
            unit="m",
            axes={
                "t0": {"value": 0.0, "unit": "s"},
                "dt": {"value": 0.1, "unit": "s"},
            },
        )
        project.objects = (obj,)

        client_mock = MagicMock()
        session_mock = MagicMock(spec=StudioSession)
        session_mock.client = client_mock

        mock_handle = MagicMock()
        raw_arr = np.arange(10, dtype=np.float64)
        mock_preview = MagicMock(values=raw_arr, handle=mock_handle)
        mock_descriptor = MagicMock()
        mock_descriptor.name = "shm-block-1"
        mock_fetched = MagicMock(
            preview=mock_preview,
            descriptor=mock_descriptor,
            unit="m",
        )
        client_mock.fetch_array.return_value = mock_fetched

        controller = AlphaController(project=project, session=session_mock)
        preview_data = controller.fetch_preview("obj-1")

        assert preview_data.ref == obj
        assert np.array_equal(preview_data.values, raw_arr)
        assert preview_data.values.flags.writeable is False
        mock_handle.close.assert_called_once()
        client_mock.release_array.assert_called_once_with("shm-block-1")

    @pytest.mark.contract("C-APP-025")
    def test_fetch_preview_release_once_and_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _PreviewHarness(monkeypatch)
        _assert_owned_preview_block_acquisition_interrupt(harness)
        _assert_successful_preview_release(harness)
        _assert_ordinary_pre_release_failures(harness)
        _assert_release_failure_closes_real_session(harness)
        _assert_ordinary_failure_precedence(harness)
        _assert_control_flow_precedence(harness)
        _assert_hostile_diagnostics(harness)
