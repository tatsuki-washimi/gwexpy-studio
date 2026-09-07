"""Independent regressions for view state after data reconstruction."""

import numpy as np
import pytest

from gui_tests.test_signal_native_workflow import _idle
from gui_tests.test_workspace_window import WorkspaceBridge
from gwexpy_studio.application.preview import PreviewData
from gwexpy_studio.application.project_factory import create_alpha_project
from gwexpy_studio.application.workspace_controller import WorkspaceController
from gwexpy_studio.domain.model import DataObjectRef, PlotSpec
from gwexpy_studio.domain.project import Project
from gwexpy_studio.persistence.project_io import load_project
from gwexpy_studio.ui.bridge import BridgeResult, WorkerBridge
from gwexpy_studio.ui.window import MainWindow


@pytest.mark.contract("GUI-WSP-0028")
@pytest.mark.gui
def test_view_undo_after_reconstruction_uses_current_preview_values(qapp):
    """Undo view settings must keep the replacement execution's array values."""
    window = MainWindow(bridge=WorkspaceBridge())
    ref = DataObjectRef(
        object_id="a",
        kind="FrequencySeries",
        shape=(2,),
        dtype="complex128",
        unit="",
    )
    spec = PlotSpec(plot_id="a-view", object_ids=("a",), kind="line")
    window.project.objects = (ref,)
    old_preview = PreviewData(
        ref=ref,
        values=np.array([1 + 2j, 3 + 4j]),
        unit="",
        x_coordinates=np.array([1.0, 2.0]),
    )
    current_preview = PreviewData(
        ref=ref,
        values=np.array([10 + 20j, 30 + 40j]),
        unit="",
        x_coordinates=np.array([1.0, 2.0]),
    )
    try:
        window.plot_canvas.set_preview(old_preview, spec)
        window.plot_canvas.component_combo.setCurrentText("imag")
        assert window.view_history.stack.count() == 1
        window._clear_pending_command()
        # Restore preserves the logical object ID while replacing its preview.
        window.plot_canvas.set_preview(current_preview, window.plot_canvas._spec)
        window.view_history.stack.undo()
        np.testing.assert_array_equal(
            window.plot_canvas.figure.axes[0].lines[0].get_ydata(), [10, 30]
        )
        window._on_bridge_result(BridgeResult(window._pending_command_id, True, {}))
        window.view_history.stack.redo()
        window._on_bridge_result(BridgeResult(window._pending_command_id, True, {}))
        # A different selection or missing current preview needs a fresh request.
        window.view_history.invalidate_data()
        window.plot_canvas.clear()
        window.view_history.stack.undo()
        assert window.plot_canvas._preview is None
        assert window.bridge.commands[-1][0] == "set_plot"
        window._on_bridge_result(BridgeResult(window._pending_command_id, True, {}))
        assert window.bridge.commands[-1] == ("preview", {"object_id": "a"})
        assert window.plot_canvas._preview is None
        # Deferred declarations and Save belong to the old document generation.
        window._queued_plot_spec = spec
        window._after_view_save = ("old-document.gwxproj", False)
        window._pending_action = "open_project"
        window._pending_command_id = "open-new-generation"
        window._on_bridge_result(
            BridgeResult(
                "open-new-generation",
                True,
                {
                    "project": create_alpha_project(),
                    "workspace_status": {"generation": "new", "revision": 0},
                },
            )
        )
        assert window._queued_plot_spec is None
        assert window._after_view_save is None
    finally:
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0029")
@pytest.mark.gui
def test_save_immediately_after_zoom_persists_visible_axis_limits(qapp, tmp_path):
    """Save must include limits still waiting for the canvas debounce timer."""
    controller = WorkspaceController(recovery=False)
    ref = DataObjectRef(
        object_id="obj-1",
        kind="FrequencySeries",
        shape=(2,),
        dtype="complex128",
        unit="",
    )
    spec = PlotSpec(plot_id="plot-1", object_ids=(ref.object_id,), kind="bode")
    controller.project.objects = (ref,)
    controller.project.plots = (spec,)
    window = MainWindow(bridge=WorkerBridge(controller=controller))
    try:
        window.initialize_workspace()
        _idle(window, qapp)
        assert window._io_capability_ready is True
        window._project = Project.from_dict(controller.project.to_dict())
        window._workspace_status = controller.workspace_status()
        preview = PreviewData(
            ref=ref,
            values=np.array([1 + 2j, 3 + 4j]),
            unit="",
            x_coordinates=np.array([1.0, 2.0]),
        )
        window.plot_canvas.set_preview(preview, spec)
        magnitude, phase = window.plot_canvas.figure.axes
        magnitude.set_xlim(1.1, 1.9)
        magnitude.set_ylim(-4, 12)
        phase.set_ylim(-45, 45)
        assert window.plot_canvas._view_timer.isActive()
        path = tmp_path / "zoom.gwxproj"
        window.save_project_to(str(path))
        _idle(window, qapp)
        saved = load_project(path)
        assert saved.plots[0].xlim == (1.1, 1.9)
        assert saved.plots[0].ylim == (-4, 12)
        assert saved.plots[0].phase_ylim == (-45, 45)
    finally:
        window._workspace_close_authorized = True
        window.close()
        qapp.processEvents()


@pytest.mark.contract("GUI-WSP-0030")
@pytest.mark.gui
def test_sources_tree_preserves_duplicate_read_parents_and_hides_undone_source(qapp):
    """Objects with identical input paths stay under their own active source row."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTreeWidget

    from gwexpy_studio.domain.history import HistoryState
    from gwexpy_studio.domain.model import DataSourceRef, Operation
    from gwexpy_studio.ui.models import populate_source_tree

    project = create_alpha_project()
    for index in (1, 2):
        source_id, op_id, object_id = f"src-{index}", f"op-{index}", f"obj-{index}"
        project.sources += (
            DataSourceRef(source_id=source_id, uri="/same", format="hdf5"),
        )
        project.graph.add(
            Operation(
                op_id=op_id,
                operation_id="data.read",
                operation_schema=1,
                inputs={},
                params={"source": "/same", "format": "hdf5"},
                outputs=(object_id,),
            )
        )
        project.objects += (
            DataObjectRef(
                object_id=object_id,
                kind="TimeSeries",
                shape=(2,),
                dtype="float64",
                unit="m",
                produced_by=op_id,
            ),
        )
    project.source_bindings = {"op-1": ("src-1",), "op-2": ("src-2",)}
    tree = QTreeWidget()
    try:
        project.history = HistoryState(
            initialized=True, baseline_heads=("obj-1", "obj-2")
        )
        populate_source_tree(tree, project)
        assert tree.topLevelItemCount() == 2
        for index in (0, 1):
            row = tree.topLevelItem(index)
            assert row.data(0, Qt.ItemDataRole.UserRole) == f"src-{index + 1}"
            assert row.childCount() == 1
            assert row.child(0).text(0) == f"obj-{index + 1}"
        project.history = HistoryState(initialized=True, baseline_heads=("obj-1",))
        populate_source_tree(tree, project)
        assert tree.topLevelItemCount() == 1
        assert tree.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole) == "src-1"
        assert tree.topLevelItem(0).child(0).text(0) == "obj-1"
    finally:
        tree.deleteLater()
        qapp.processEvents()
