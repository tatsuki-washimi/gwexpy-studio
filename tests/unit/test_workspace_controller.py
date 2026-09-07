"""Workspace transitions keep scientific state, disk state and recovery distinct."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest


def _controller(tmp_path: Path):
    from gwexpy_studio.application.workspace_controller import WorkspaceController

    return WorkspaceController(recovery_root=tmp_path / "recovery")


def _read(controller, source):
    inspection = controller.inspect_io(
        {"paths": [str(source)], "datatype": "TimeSeries", "format": "hdf5"}
    )
    return controller.read_io(inspection)[0]


@pytest.mark.contract("WSP-0010")
def test_workspace_save_open_is_data_only_and_snapshots_are_detached(tmp_path):
    from gwexpy_studio.application.workspace_bridge import workspace_snapshot

    controller = _controller(tmp_path)
    try:
        controller.set_ui_state({"workspace": {"selection": None}})
        assert controller.workspace_status()["dirty"]
        path = tmp_path / "日本語.gwxproj"
        controller.save_workspace(path)
        assert not controller.workspace_status()["dirty"]
        snapshot = workspace_snapshot(controller)
        snapshot["project"].ui_state = {"tampered": True}
        assert controller.project.ui_state == {"workspace": {"selection": None}}
        controller.new_workspace()
        controller.open_workspace(path)
        assert controller.session.client.state.value == "NEW"
        assert controller.project.ui_state == {"workspace": {"selection": None}}
        assert not controller.workspace_status()["dirty"]
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0011")
def test_workspace_failed_save_keeps_path_dirty_and_previous_bytes(
    tmp_path, monkeypatch
):
    import gwexpy_studio.application.workspace_document as document

    controller = _controller(tmp_path)
    try:
        path = tmp_path / "saved.gwxproj"
        controller.save_workspace(path)
        old = path.read_bytes()
        controller.set_ui_state({"changed": True})
        monkeypatch.setattr(
            document,
            "save_project",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError, match="disk full"):
            controller.save_workspace(path)
        assert controller.workspace_status()["dirty"]
        assert path.read_bytes() == old
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0012")
def test_workspace_native_read_undo_save_restore_and_redo(tmp_path, hdf5_source):
    from gwexpy_studio.domain.history import active_object_ids

    controller = _controller(tmp_path)
    try:
        raw = _read(controller, hdf5_source)
        result = controller.apply(
            "timeseries.crop",
            raw.object_id,
            {"start": 1000000000.125, "end": 1000000000.875},
        )
        expected = controller.fetch_preview(result.object_id).values.copy()
        controller.undo_analysis()
        assert result.object_id not in active_object_ids(controller.project)
        script = tmp_path / "undo.py"
        controller.export_script(script)
        assert ".crop(" not in script.read_text()
        saved = tmp_path / "undo.gwxproj"
        controller.save_workspace(saved)
        controller.open_workspace(saved)
        assert controller.workspace_status()["can_redo"]
        assert controller.workspace_status()["needs_restore"]
        review = controller.review_restore()
        controller.restore_workspace(review, confirmed=True)
        assert (
            result.object_id not in controller.workspace_status()["resident_object_ids"]
        )
        review = controller.review_restore(redo=True)
        controller.restore_workspace(review, confirmed=True)
        np.testing.assert_array_equal(
            controller.fetch_preview(result.object_id).values, expected
        )
        assert controller.workspace_status()["can_undo"]
        assert not controller.workspace_status()["can_redo"]
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0013")
def test_workspace_failed_group_does_not_publish_partial_individual_reads(
    tmp_path, hdf5_source
):
    from gwexpy_studio.domain.history import active_object_ids

    controller = _controller(tmp_path)
    bad = tmp_path / "bad.h5"
    bad.write_bytes(b"not hdf5")
    try:
        inspection = controller.inspect_io(
            {
                "paths": [str(hdf5_source), str(bad)],
                "datatype": "TimeSeries",
                "format": "hdf5",
                "combine": "individual",
            }
        )
        with pytest.raises(Exception):
            controller.read_io(inspection)
        assert not active_object_ids(controller.project)
        assert not controller.workspace_status()["can_undo"]
        assert controller.project.graph.operations
        assert any(
            record.status == "failed" for record in controller.project.executions
        )
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0014")
def test_workspace_restore_rejects_changed_bytes_even_if_size_and_time_match(
    tmp_path, hdf5_source
):
    import os

    controller = _controller(tmp_path)
    try:
        _read(controller, hdf5_source)
        stat = hdf5_source.stat()
        saved = tmp_path / "saved.gwxproj"
        controller.save_workspace(saved)
        controller.open_workspace(saved)
        content = bytearray(hdf5_source.read_bytes())
        content[-1] ^= 1
        hdf5_source.write_bytes(content)
        os.utime(hdf5_source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with pytest.raises(Exception, match="changed|Changed"):
            controller.review_restore()
        assert controller.workspace_status()["needs_restore"]
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0015")
def test_workspace_restore_cancel_does_not_publish_candidate(tmp_path, hdf5_source):
    controller = _controller(tmp_path)
    try:
        _read(controller, hdf5_source)
        saved = tmp_path / "saved.gwxproj"
        controller.save_workspace(saved)
        controller.open_workspace(saved)
        review = controller.review_restore()
        prior = controller.project.to_dict()
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(Exception, match="cancel"):
            controller.restore_workspace(review, confirmed=True, cancel_event=cancel)
        assert controller.project.to_dict() == prior
        assert controller.workspace_status()["needs_restore"]
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0016")
def test_workspace_file_lock_and_invalid_open_preserve_current_document(tmp_path):
    first = _controller(tmp_path / "one")
    second = _controller(tmp_path / "two")
    try:
        path = tmp_path / "same.gwxproj"
        first.save_workspace(path)
        second.open_workspace(path)
        assert second.workspace_status()["read_only"]
        before = second.project.to_dict()
        invalid = tmp_path / "bad.gwxproj"
        invalid.write_text(json.dumps({"schema_version": 999}))
        with pytest.raises(Exception):
            second.open_workspace(invalid)
        assert second.project.to_dict() == before
        second.save_workspace(tmp_path / "separate.gwxproj")
        assert not second.workspace_status()["read_only"]
    finally:
        first.finalize_workspace(clean=True)
        second.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0017")
def test_workspace_open_cleanup_failure_retains_original_writer_lock(
    tmp_path, monkeypatch
):
    from gwexpy_studio.persistence.locking import ProjectLock

    controller = _controller(tmp_path)
    original = tmp_path / "original.gwxproj"
    target = tmp_path / "target.gwxproj"
    try:
        controller.save_workspace(original)
        target.write_bytes(original.read_bytes())
        owner = controller._recovery
        close = owner.close
        monkeypatch.setattr(
            owner,
            "close",
            lambda **kwargs: (_ for _ in ()).throw(OSError("cleanup failed")),
        )
        with pytest.raises(OSError, match="cleanup failed"):
            controller.open_workspace(target)
        competing = ProjectLock(original)
        try:
            assert not competing.acquire()
            assert controller.workspace_status()["project_path"] == str(original)
        finally:
            competing.close()
            monkeypatch.setattr(owner, "close", close)
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0018")
def test_workspace_confirmed_legacy_restore_establishes_new_source_evidence(
    tmp_path, hdf5_source
):
    controller = _controller(tmp_path)
    try:
        _read(controller, hdf5_source)
        controller.project.source_manifests = {}
        saved = tmp_path / "legacy.gwxproj"
        controller.save_workspace(saved)
        controller.open_workspace(saved)
        review = controller.review_restore()
        assert review["unverified_sources"]
        controller.restore_workspace(review, confirmed=True)
        assert controller.project.source_manifests
        controller.save_workspace(saved)
        controller.open_workspace(saved)
        assert not controller.review_restore()["unverified_sources"]
    finally:
        controller.finalize_workspace(clean=True)
