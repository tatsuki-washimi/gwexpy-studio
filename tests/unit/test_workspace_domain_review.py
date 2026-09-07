"""Independent workspace review regressions for external data writes."""

from __future__ import annotations

import pytest

from gwexpy_studio.application.workspace_controller import WorkspaceController


def _read(controller, path, datatype="TimeSeries"):
    inspection = controller.inspect_io(
        {"paths": [str(path)], "datatype": datatype, "format": "hdf5"}
    )
    return controller.read_io(inspection)[0]


@pytest.mark.contract("WSP-0019")
def test_workspace_write_interrupt_reaches_the_owned_client(
    tmp_path, hdf5_source, monkeypatch
):
    controller = WorkspaceController(recovery=False)
    try:
        raw = _read(controller, hdf5_source)
        cancel_calls = []
        monkeypatch.setattr(
            controller.session.client, "cancel", lambda: cancel_calls.append("cancel")
        )
        original_request = controller._signal_request

        def request(kind, payload):
            if kind == "write_data":
                controller.request_cancel()
                return {"details": {}}
            return original_request(kind, payload)

        monkeypatch.setattr(controller, "_signal_request", request)
        controller.write_data(
            raw.object_id,
            {
                "datatype": "TimeSeries",
                "paths": [str(tmp_path / "interrupt.h5")],
                "format": "hdf5",
            },
        )
        assert cancel_calls == ["cancel"]
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0020")
def test_read_undo_restores_selection_captured_before_the_read(hdf5_source):
    controller = WorkspaceController(recovery=False)
    try:
        first = _read(controller, hdf5_source)
        selection = {"object_id": first.object_id, "selector": None}
        controller.set_ui_state({"selection": selection})
        _read(controller, hdf5_source)
        controller.undo_analysis()
        assert controller.workspace_status()["selected_handles"] == [selection]
    finally:
        controller.finalize_workspace(clean=True)
