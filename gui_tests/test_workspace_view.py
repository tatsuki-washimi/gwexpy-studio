"""Independent view undo on a real Qt Matplotlib canvas."""

import numpy as np
import pytest
from PySide6.QtTest import QTest

from gui_tests.test_workspace_window import WorkspaceBridge
from gwexpy_studio.application.preview import PreviewData
from gwexpy_studio.domain.model import DataObjectRef, PlotSpec
from gwexpy_studio.ui.window import MainWindow


@pytest.mark.contract("GUI-WSP-0005")
@pytest.mark.gui
def test_view_undo_tracks_both_bode_axes_without_analysis_undo(qapp):
    """View undo tracks both bode axes without analysis undo."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    values = np.array([1 + 1j, 2 + 1j, 3 + 1j])
    ref = DataObjectRef(
        object_id="a",
        kind="FrequencySeries",
        shape=values.shape,
        dtype=str(values.dtype),
        unit="",
    )
    preview = PreviewData(
        ref=ref, values=values, unit="", x_coordinates=np.array([1.0, 2.0, 3.0])
    )
    spec = PlotSpec(plot_id="bode", object_ids=("a",), kind="bode")
    window.plot_canvas.set_preview(preview, spec)
    magnitude, phase = window.plot_canvas.figure.axes
    magnitude.set_xlim(1.2, 2.8)
    magnitude.set_ylim(-3, 12)
    phase.set_ylim(-20, 80)
    QTest.qWait(160)
    assert window.view_history.stack.count() == 1
    assert window.plot_canvas._spec.phase_ylim == (-20, 80)
    assert not window.view_history.undo_action.isEnabled()
    window._clear_pending_command()
    window._update_command_state()
    assert window.view_history.undo_action.isEnabled()
    window.view_history.stack.undo()
    assert window.plot_canvas._spec.phase_ylim is None
    window.view_history.stack.redo()
    assert window.plot_canvas.figure.axes[1].get_ylim() == (-20, 80)
    from gwexpy_studio.application.project_factory import create_alpha_project
    from gwexpy_studio.domain.project import Project

    saved = create_alpha_project()
    saved.objects, saved.plots = (ref,), (window.plot_canvas._spec,)
    reopened = Project.from_dict(saved.to_dict())
    window.plot_canvas.set_preview(preview, reopened.plots[0])
    assert window.plot_canvas.figure.axes[0].get_xlim() == (1.2, 2.8)
    assert window.plot_canvas.figure.axes[0].get_ylim() == (-3, 12)
    assert window.plot_canvas.figure.axes[1].get_ylim() == (-20, 80)
    assert all(kind != "undo_analysis" for kind, _ in bridge.commands)
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0006")
@pytest.mark.gui
def test_view_undo_targets_the_original_plot_after_selection_changes(qapp):
    """View undo targets the original plot after selection changes."""
    bridge = WorkspaceBridge()
    window = MainWindow(bridge=bridge)
    previews = []
    for object_id, values in (("a", [1 + 2j, 3 + 4j]), ("b", [5 + 6j, 7 + 8j])):
        array = np.array(values)
        ref = DataObjectRef(
            object_id=object_id,
            kind="FrequencySeries",
            shape=array.shape,
            dtype=str(array.dtype),
            unit="",
        )
        previews.append(
            PreviewData(
                ref=ref, values=array, unit="", x_coordinates=np.array([1.0, 2.0])
            )
        )
    window.project.objects = tuple(preview.ref for preview in previews)
    first = PlotSpec(plot_id="a-view", object_ids=("a",), kind="line")
    second = PlotSpec(plot_id="b-view", object_ids=("b",), kind="line")
    window.plot_canvas.set_preview(previews[0], first)
    window.plot_canvas.component_combo.setCurrentText("imag")
    window._clear_pending_command()
    window.plot_canvas.set_preview(previews[1], second)
    window._current_object_id = "b"
    window.view_history.stack.undo()
    assert window.plot_canvas._preview.ref.object_id == "a"
    assert window._current_object_id == "a"
    np.testing.assert_array_equal(
        window.plot_canvas.figure.axes[0].lines[0].get_ydata(), [1, 3]
    )
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0007")
@pytest.mark.gui
def test_view_undo_restores_dock_placement_and_only_current_layout_is_saved(qapp):
    """View undo restores dock placement and only current layout is saved."""
    from PySide6.QtCore import Qt

    from gwexpy_studio.ui.workspace_state import capture_ui_state

    window = MainWindow(bridge=WorkspaceBridge())
    window.show()
    qapp.processEvents()
    window.view_history.capture_layout()
    before = capture_ui_state(window)["layout"]
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, window.source_dock)
    QTest.qWait(150)
    assert window.view_history.stack.count() == 1
    assert (
        window.dockWidgetArea(window.source_dock)
        == Qt.DockWidgetArea.RightDockWidgetArea
    )
    window.view_history.stack.undo()
    assert (
        window.dockWidgetArea(window.source_dock)
        == Qt.DockWidgetArea.LeftDockWidgetArea
    )
    assert capture_ui_state(window)["layout"] == before
    window.view_history.stack.redo()
    assert (
        window.dockWidgetArea(window.source_dock)
        == Qt.DockWidgetArea.RightDockWidgetArea
    )
    assert "undo" not in capture_ui_state(window)
    window.deleteLater()
    qapp.processEvents()
