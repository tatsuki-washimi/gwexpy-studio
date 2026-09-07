"""Ownership failures retain the original document and its recovery protection."""

from __future__ import annotations

import pytest

from gwexpy_studio.application.workspace_controller import WorkspaceController
from gwexpy_studio.persistence.locking import ProjectLock


@pytest.mark.contract("WSP-0044")
def test_failed_worker_close_does_not_unlock_live_recovery(tmp_path, monkeypatch):
    c = WorkspaceController(recovery_root=tmp_path / "recovery")
    try:
        path = tmp_path / "original.gwxproj"
        c.save_workspace(path)
        original = c.project
        close = c.session.close
        monkeypatch.setattr(
            c.session,
            "close",
            lambda: (_ for _ in ()).throw(OSError("worker cleanup failed")),
        )
        try:
            with pytest.raises(OSError, match="worker cleanup failed"):
                c.new_workspace()
        finally:
            monkeypatch.setattr(c.session, "close", close)
        assert c.project is original
        c.set_ui_state({"still": "editable"})
        assert c.workspace_status()["recovery_error"] is None
        assert c.list_recoveries() == []
        lock = ProjectLock(path)
        try:
            assert not lock.acquire()
        finally:
            lock.close()
            monkeypatch.setattr(c.session, "close", close)
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0108")
def test_invalid_unicode_project_does_not_replace_current_document(tmp_path):
    import json

    c = WorkspaceController(recovery_root=tmp_path / "recovery")
    try:
        c.set_ui_state({"draft": "keep"})
        before = c.project.to_dict()
        incoming = c.project.to_dict()
        incoming["ui_state"] = {"draft": "\ud800"}
        path = tmp_path / "invalid.gwxproj"
        path.write_text(json.dumps(incoming), encoding="utf-8")
        with pytest.raises((UnicodeError, ValueError)):
            c.open_workspace(path)
        assert c.project.to_dict() == before
        c.set_ui_state({"draft": "still editable"})
    finally:
        c.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0112")
def test_view_settings_are_editable_before_data_restoration():
    from gwexpy_studio.domain import DataObjectRef, PlotSpec
    from gwexpy_studio.errors import OperationError

    c = WorkspaceController(recovery=False)
    try:
        c.project.objects = (
            DataObjectRef(
                object_id="obj-1",
                kind="TimeSeries",
                shape=(3,),
                dtype="float64",
                unit="m",
            ),
        )
        spec = PlotSpec(
            plot_id="plot-1", kind="line", object_ids=("obj-1",), xlim=(1.0, 2.0)
        )
        c.set_plot_spec(spec)
        assert c.project.plots == (spec,)
        with pytest.raises(OperationError):
            c.fetch_preview("obj-1")
        assert c.session.client.state.value == "NEW"
    finally:
        c.finalize_workspace(clean=True)
