"""Save, reopen, restore, and analysis history through a real Qt worker bridge."""

import subprocess
import sys

import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from gui_tests.test_signal_native_workflow import _idle
from gwexpy_studio.application.workspace_controller import WorkspaceController
from gwexpy_studio.ui.bridge import WorkerBridge
from gwexpy_studio.ui.window import MainWindow
from gwexpy_studio.ui.workspace_window import WorkspaceTools


@pytest.mark.contract("GUI-WSP-0004")
@pytest.mark.gui
def test_native_gui_save_open_restore_and_persistent_analysis_undo(
    qapp, tmp_path, monkeypatch
):
    """Native gui save open restore and persistent analysis undo."""
    source = tmp_path / "入力.h5"
    script = """
import sys
import numpy as np
from gwexpy.timeseries import TimeSeries
t = np.arange(1024)/128
TimeSeries(np.sin(2*np.pi*10*t),dt=1/128,t0=0,unit='m',name='input').write(sys.argv[1],format='hdf5')
"""
    subprocess.run(
        [sys.executable, "-c", script, str(source)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    controller = WorkspaceController(recovery=False)
    window = MainWindow(bridge=WorkerBridge(controller=controller))
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    try:
        window.show()
        window.initialize_workspace()
        _idle(window, qapp)
        assert window._io_capability_ready is True
        window.show_open_data()
        _idle(window, qapp)
        panel = window.open_data_panel
        panel.paths_edit.setPlainText(str(source))
        panel.format_combo.setEditText("hdf5")
        panel.inspect_button.click()
        _idle(window, qapp)
        panel.confirm_button.click()
        _idle(window, qapp)
        raw_id = window.project.objects[0].object_id
        window.show_parameter_panel("timeseries.lowpass")
        window.parameter_panel.fields["frequency"].setText("20")
        window.parameter_panel.apply_button.click()
        _idle(window, qapp)
        assert window.plot_canvas._preview is not None, (
            window.statusBar().currentMessage()
        )
        original = window.plot_canvas._preview
        filtered_id = original.ref.object_id
        values = original.values.copy()
        coordinates = original.x_coordinates.copy()
        project_path = tmp_path / "解析.gwxproj"
        window.save_project_to(str(project_path))
        _idle(window, qapp)
        assert project_path.is_file(), window.statusBar().currentMessage()
        assert not window._workspace_status["dirty"]
        window._request_document_change("open_project", {"path": str(project_path)})
        _idle(window, qapp)
        assert window.plot_canvas._preview is None
        assert window._workspace_status["needs_restore"]
        assert window._workspace_status["resident_object_ids"] == []
        assert window.parameter_panel.fields["frequency"].text() == "20"

        def approve(review):
            def capture_and_confirm():
                dialog = QApplication.activeModalWidget()
                assert dialog.grab().save(
                    str(tmp_path / "workspace-restore-review.png")
                )
                dialog.button(QMessageBox.StandardButton.Ok).click()

            QTimer.singleShot(0, capture_and_confirm)
            WorkspaceTools._review_restore_response(window, review)

        monkeypatch.setattr(window, "_review_restore_response", approve)
        window.review_restore()
        _idle(window, qapp)
        restored = window.plot_canvas._preview
        assert restored is not None, window.statusBar().currentMessage()
        assert restored.ref.object_id == filtered_id
        np.testing.assert_array_equal(restored.values, values)
        np.testing.assert_array_equal(restored.x_coordinates, coordinates)
        assert restored.unit == original.unit
        assert window.undo_analysis_action.isEnabled()
        window.setFocus()
        window.undo_analysis_action.trigger()
        _idle(window, qapp)
        assert any(
            "inactive" in window.history_list.item(i).text()
            for i in range(window.history_list.count())
        )
        assert filtered_id not in window._workspace_status["active_object_ids"]
        assert raw_id in window._workspace_status["active_object_ids"]
        assert window.redo_analysis_action.isEnabled()
        assert all(
            handle[1]["object_id"] != filtered_id for handle in window._object_handles()
        )
        window.redo_analysis_action.trigger()
        _idle(window, qapp)
        assert filtered_id in window._workspace_status["active_object_ids"]
        exported = tmp_path / "restored.py"
        window.export_to_file(str(exported))
        _idle(window, qapp)
        np.save(tmp_path / "expected-values.npy", values)
        np.save(tmp_path / "expected-times.npy", coordinates)
        replay = """
import runpy, sys
import numpy as np
namespace = runpy.run_path(sys.argv[1])
result = namespace[sys.argv[2]]
np.testing.assert_array_equal(result.value, np.load(sys.argv[3]))
np.testing.assert_array_equal(result.times.value, np.load(sys.argv[4]))
assert str(result.unit) == sys.argv[5]
"""
        checked = subprocess.run(
            [
                sys.executable,
                "-c",
                replay,
                str(exported),
                filtered_id.replace("-", "_"),
                str(tmp_path / "expected-values.npy"),
                str(tmp_path / "expected-times.npy"),
                original.unit,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert checked.returncode == 0, checked.stderr
        assert any(
            "Restore data" in window.history_list.item(i).text()
            for i in range(window.history_list.count())
        )
        for visible_panel in (window.parameter_panel, window.open_data_panel):
            if visible_panel is not None:
                visible_panel.hide()
        window.resize(1100, 750)
        window._flush_ui_checkpoint()
        _idle(window, qapp)
        assert window.grab().save(str(tmp_path / "workspace-restored.png"))
        menu = window.menuBar().actions()[0].menu()
        menu.popup(window.menuBar().mapToGlobal(window.menuBar().rect().bottomLeft()))
        qapp.processEvents()
        assert menu.grab().save(str(tmp_path / "workspace-file-menu.png"))
        menu.close()
        assert "gwexpy" not in sys.modules
    finally:
        window._workspace_close_authorized = True
        window.close()
        qapp.processEvents()
