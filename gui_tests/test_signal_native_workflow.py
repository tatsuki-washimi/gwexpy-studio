"""Native worker integration through real modeless I/O and signal controls."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from PySide6.QtTest import QTest

from gwexpy_studio.ui.bridge import BridgeState
from gwexpy_studio.ui.window import MainWindow


def _idle(window, qapp):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        qapp.processEvents()
        if window.bridge.state is BridgeState.IDLE and not window._command_reserved:
            return
        if window.bridge.state is BridgeState.FAILED:
            pytest.fail(window.statusBar().currentMessage())
        QTest.qWait(10)
    pytest.fail(f"Native command did not finish: {window.statusBar().currentMessage()}")


@pytest.mark.contract(id="GUI-SIG-E2E-001")
@pytest.mark.gui
def test_native_open_filter_csd_bode_and_data_export(qapp, tmp_path):
    """Run real widget requests while scientific imports stay in child processes."""
    source = tmp_path / "source.h5"
    script = """
import sys
import numpy as np
import gwexpy
from gwexpy.timeseries import TimeSeries
gwexpy.register_all()
t = np.arange(2048) / 256
TimeSeries(np.sin(2*np.pi*10*t) + .1*np.sin(2*np.pi*60*t), dt=1/256,
           t0=0, unit='m', name='Input A').write(sys.argv[1], format='hdf5')
"""
    subprocess.run(
        [sys.executable, "-c", script, str(source)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    window = MainWindow()
    try:
        window.initialize_workspace()
        _idle(window, qapp)
        assert window._io_capability_ready is True
        window.show_open_data()
        _idle(window, qapp)
        panel = window.open_data_panel
        assert panel is not None
        assert panel.format_combo.count() > 1, panel.error_label.text()
        panel.paths_edit.setPlainText(str(source))
        panel.format_combo.setEditText("hdf5")
        panel.inspect_button.click()
        _idle(window, qapp)
        assert panel.confirm_button.isEnabled(), panel.error_label.text()
        panel.confirm_button.click()
        _idle(window, qapp)
        assert len(window.project.objects) == 1, window.statusBar().currentMessage()
        assert window.plot_canvas._preview is not None
        original = window.project.objects[0].object_id

        window.show_parameter_panel("timeseries.lowpass")
        operation = window.parameter_panel
        operation.fields["frequency"].setText("0.02 kHz")
        assert operation.request()["params"]["frequency"] == {
            "value": 0.02,
            "unit": "kHz",
        }
        history = len(window.project.graph.operations)
        operation.preview_button.click()
        _idle(window, qapp)
        assert not operation.error_label.text(), operation.error_label.text()
        assert len(operation.response_canvas.figure.axes) == 2
        assert len(window.project.graph.operations) == history
        operation.apply_button.click()
        _idle(window, qapp)
        assert len(window.project.objects) == 2, window.statusBar().currentMessage()
        filtered = window.project.objects[-1].object_id
        window._show_applied_response()
        _idle(window, qapp)
        assert not operation.error_label.text(), operation.error_label.text()
        assert len(window.project.graph.operations) == history + 1
        recorded = window.project.graph.operations[-1].params["frequency"]
        assert recorded == 20.0  # Native recipe stores the converted cutoff in Hz.
        assert operation.request()["params"]["frequency"] == {
            "value": recorded,
            "unit": "Hz",
        }

        window.show_parameter_panel("timeseries.csd")
        operation.self_combo.setCurrentIndex(
            operation.self_combo.findData({"object_id": original})
        )
        operation.other_combo.setCurrentIndex(
            operation.other_combo.findData({"object_id": filtered})
        )
        operation.fields["fftlength"].setText("1")
        operation.apply_button.click()
        _idle(window, qapp)
        assert len(window.project.objects) == 3, window.statusBar().currentMessage()
        assert window.plot_canvas._spec.kind == "bode"
        assert len(window.plot_canvas.figure.axes) == 2
        window.plot_canvas.magnitude_combo.setCurrentText("linear")
        _idle(window, qapp)
        assert window.project.plots[-1].magnitude_scale == "linear"

        target = tmp_path / "csd.h5"
        window._show_export_data()
        _idle(window, qapp)
        output = window.export_data_panel
        output.paths_edit.setPlainText(str(target))
        output.format_combo.setEditText("hdf5")
        output.inspect_button.click()
        output.confirm_button.click()
        _idle(window, qapp)
        assert target.exists(), window.statusBar().currentMessage()
        assert window.project.activities[-1].status == "succeeded"
        assert (
            "Data write:"
            in window.history_list.item(window.history_list.count() - 1).text()
        )
        assert not window.include_writes_action.isChecked()
        python_file = tmp_path / "export.py"
        window.export_to_file(str(python_file))
        _idle(window, qapp)
        assert python_file.exists(), window.statusBar().currentMessage()
        assert str(target) not in python_file.read_text()
        replay = """
import runpy
import sys
import matplotlib
matplotlib.use('Agg')
import numpy as np
from gwexpy.timeseries import TimeSeries
from gwexpy.frequencyseries import FrequencySeries
namespace = runpy.run_path(sys.argv[1])
a = TimeSeries.read(sys.argv[2], format='hdf5')
oracle = a.csd(a.lowpass(20, filtfilt=True), fftlength=1)
written = FrequencySeries.read(sys.argv[3], format='hdf5')
replayed = namespace[sys.argv[4]]
for result in (written, replayed):
    np.testing.assert_allclose(result.value, oracle.value, rtol=1e-10, atol=1e-14)
    np.testing.assert_array_equal(result.frequencies.value, oracle.frequencies.value)
    assert result.unit == oracle.unit
figure = namespace['figures'][sys.argv[5]]
assert len(figure.axes) == 2
assert 'dB' not in figure.axes[0].get_ylabel()
"""
        checked = subprocess.run(
            [
                sys.executable,
                "-c",
                replay,
                str(python_file),
                str(source),
                str(target),
                window.project.objects[-1].object_id.replace("-", "_"),
                window.project.plots[-1].plot_id,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert checked.returncode == 0, checked.stderr
        window.include_writes_action.setChecked(True)
        window.export_to_file(str(python_file))
        _idle(window, qapp)
        assert str(target) in python_file.read_text()
    finally:
        window._workspace_close_authorized = True
        window.close()
        qapp.processEvents()
    assert "gwexpy" not in sys.modules
    assert "gwpy" not in sys.modules
