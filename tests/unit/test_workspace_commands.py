"""Reviewed workspace commands, interruption records and restore generations."""

from __future__ import annotations

from dataclasses import replace

import pytest

from gwexpy_studio.application.workspace_bridge import execute_workspace_command
from gwexpy_studio.application.workspace_controller import WorkspaceController
from gwexpy_studio.errors import OperationError


def _read(controller, path):
    inspection = controller.inspect_io(
        {"paths": [str(path)], "datatype": "TimeSeries", "format": "hdf5"}
    )
    return controller.read_io(inspection)[0]


@pytest.mark.contract("WSP-0001")
def test_workspace_commands_save_open_new_unknown_and_no_undo(tmp_path):
    c = WorkspaceController(recovery_root=tmp_path / "recovery")
    try:
        result = execute_workspace_command(
            c, "set_ui_state", {"ui_state": {"draft": "20 Hz"}}
        )
        assert result["workspace_status"]["dirty"]
        with pytest.raises(OperationError, match="Choose"):
            execute_workspace_command(c, "save_project", {})
        path = tmp_path / "project.gwxproj"
        execute_workspace_command(c, "save_project", {"path": str(path)})
        execute_workspace_command(c, "new_project", {})
        execute_workspace_command(c, "open_project", {"path": str(path)})
        assert c.project.ui_state["draft"] == "20 Hz"
        for command in ("undo_analysis", "redo_analysis", "unknown"):
            with pytest.raises(OperationError):
                execute_workspace_command(c, command, {})
        execute_workspace_command(c, "close_project", {})
        assert not c.project.ui_state
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0002")
def test_workspace_recovery_after_interrupted_write_never_repeats_output(tmp_path):
    root = tmp_path / "recovery"
    first = WorkspaceController(recovery_root=root)
    first.set_ui_state({"draft": "pending"})
    output = tmp_path / "output.dat"
    output.write_text("partially written")
    first._intent("write_data", {"target": str(output), "object_id": "obj-9"})
    run_id = first._recovery.run_id
    first.close()
    second = WorkspaceController(recovery_root=root)
    try:
        candidates = execute_workspace_command(second, "list_recoveries", {})[
            "recoveries"
        ]
        assert [entry["run_id"] for entry in candidates] == [run_id]
        assert candidates[0]["project_id"] == first.project.project_id
        execute_workspace_command(second, "recover_project", {"run_id": run_id})
        assert second.project.ui_state == {"draft": "pending"}
        assert second.workspace_status()["dirty"]
        assert second.project.activities[-1].status == "unknown"
        assert second.project.activities[-1].error["code"] == "output_unconfirmed"
        assert output.read_text() == "partially written"
        assert not second.list_recoveries()
    finally:
        second.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0003")
def test_workspace_recovery_save_failure_keeps_dirty_and_last_checkpoint(
    tmp_path, monkeypatch
):
    c = WorkspaceController(recovery_root=tmp_path / "recovery")
    try:
        c.set_ui_state({"draft": "old"})
        last = c.workspace_status()["last_checkpoint_at"]
        monkeypatch.setattr(
            c._recovery,
            "checkpoint",
            lambda *a, **k: (_ for _ in ()).throw(OSError("full")),
        )
        c.set_ui_state({"draft": "new"})
        state = c.workspace_status()
        assert state["dirty"] and "full" in state["recovery_error"]
        assert state["last_checkpoint_at"] == last
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0004")
def test_workspace_restore_review_is_bound_to_document_and_explicit_consent(
    tmp_path, hdf5_source
):
    c = WorkspaceController(recovery=False)
    try:
        _read(c, hdf5_source)
        path = tmp_path / "saved.gwxproj"
        c.save_workspace(path)
        c.open_workspace(path)
        review = c.review_restore()
        for altered, confirmed in ((review, False), ({**review, "targets": []}, True)):
            with pytest.raises(OperationError) as error:
                c.restore_workspace(altered, confirmed=confirmed)
            assert error.value.code == "restore_confirmation_required"
        c.set_ui_state({"changed": True})
        with pytest.raises(OperationError):
            c.restore_workspace(review, confirmed=True)
        assert c.workspace_status()["needs_restore"]
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0005")
def test_workspace_version_change_recomputes_and_keeps_previous_execution(
    tmp_path, hdf5_source
):
    c = WorkspaceController(recovery=False)
    try:
        original = _read(c, hdf5_source)
        old = c.project.executions[0]
        c.project.executions = (
            replace(old, environment={**old.environment, "gwexpy": "0.1.0"}),
        )
        path = tmp_path / "version.gwxproj"
        c.save_workspace(path)
        c.open_workspace(path)
        before = c.project.executions[0]
        review = execute_workspace_command(c, "review_restore", {})["review"]
        assert review["environment_differences"]["gwexpy"]["recorded"] == "0.1.0"
        result = execute_workspace_command(
            c, "restore_project", {"review": review, "confirmed": True}
        )
        assert result["workspace_status"]["environment_changed"]
        assert result["workspace_status"]["dirty"]
        assert c.project.executions[0] == before
        assert (
            c.project.executions[-1].details["previous_object_ref"]["object_id"]
            == original.object_id
        )
        assert c.project.executions[-1].environment["gwexpy"] != "0.1.0"
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0006")
def test_workspace_inactive_and_unrestored_data_cannot_be_used_directly(
    tmp_path, hdf5_source
):
    c = WorkspaceController(recovery=False)
    try:
        original = _read(c, hdf5_source)
        c.undo_analysis()
        with pytest.raises(OperationError) as error:
            c.fetch_preview(original.object_id)
        assert error.value.code == "object_not_found"
        c.redo_analysis()
        path = tmp_path / "cold.gwxproj"
        c.save_workspace(path)
        c.open_workspace(path)
        with pytest.raises(OperationError) as error:
            c.fetch_member_preview_payload(original.object_id)
        assert error.value.code == "restore_required"
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0007")
def test_workspace_write_only_member_does_not_join_analysis_history(tmp_path):
    import numpy as np
    from gwexpy.timeseries import TimeSeries, TimeSeriesDict

    from gwexpy_studio.domain.history import active_object_ids

    source = tmp_path / "container.h5"
    TimeSeriesDict(
        a=TimeSeries(np.arange(128.0), sample_rate=128, unit="m", name="signal")
    ).write(source, format="hdf5")
    c = WorkspaceController(recovery=False)
    try:
        inspection = c.inspect_io(
            {"paths": [str(source)], "datatype": "TimeSeriesDict", "format": "hdf5"}
        )
        parent = c.read_io(inspection)[0]
        before = c.project.history
        output = tmp_path / "member.h5"
        record = c.write_data(
            parent.object_id,
            {"paths": [str(output)], "datatype": "TimeSeries", "format": "hdf5"},
            {"key": "a"},
        )
        assert record.status == "succeeded"
        assert c.project.history == before
        assert active_object_ids(c.project) == frozenset({parent.object_id})
        assert output.exists()
        c.undo_analysis()
        assert output.exists()
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0008")
def test_workspace_unacknowledged_write_is_unknown_and_requires_restore(
    tmp_path, hdf5_source, monkeypatch
):
    c = WorkspaceController(recovery=False)
    try:
        source = _read(c, hdf5_source)
        original_request = c._signal_request

        def crash(kind, payload):
            if kind == "write_data":
                raise OperationError("worker vanished", code="worker_crashed")
            return original_request(kind, payload)

        monkeypatch.setattr(c, "_signal_request", crash)
        with pytest.raises(OperationError) as error:
            c.write_data(
                source.object_id,
                {
                    "paths": [str(tmp_path / "out.h5")],
                    "datatype": "TimeSeries",
                    "format": "hdf5",
                },
            )
        assert error.value.code == "worker_crashed"
        assert c.project.activities[-1].status == "unknown"
        assert c.project.activities[-1].error["code"] == "output_unconfirmed"
        assert c.workspace_status()["needs_restore"]
        assert not c.workspace_status()["resident_object_ids"]
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0009")
def test_workspace_restore_never_claims_unmaterialized_metadata_is_resident(
    tmp_path, hdf5_source, monkeypatch
):
    from gwexpy_studio.session import StudioSession

    c = WorkspaceController(recovery=False)
    try:
        _read(c, hdf5_source)
        path = tmp_path / "project.gwxproj"
        c.save_workspace(path)
        c.open_workspace(path)
        review = c.review_restore()
        original = c.project.to_dict()
        monkeypatch.setattr(StudioSession, "replay", lambda *a, **k: None)
        with pytest.raises(OperationError, match="materialized|missing|Missing"):
            c.restore_workspace(review, confirmed=True)
        assert c.project.to_dict() == original
        assert c.workspace_status()["needs_restore"]
    finally:
        c.finalize_workspace(clean=True)
