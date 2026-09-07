"""Window-level signal tools, lazy member selection, and dispatch guards."""

import pytest
from PySide6.QtCore import QObject, Signal

from gwexpy_studio.domain.model import DataObjectRef, MemberRef
from gwexpy_studio.ui.bridge import BridgeResult, BridgeState
from gwexpy_studio.ui.window import MainWindow


class _Bridge(QObject):
    result_received = Signal(object)
    safe_to_destroy = Signal()
    state_changed = Signal(str)
    state = BridgeState.IDLE

    def __init__(self):
        super().__init__()
        self.commands = []

    def send_command(self, kind, payload):
        self.commands.append((kind, payload))
        return f"command-{len(self.commands)}"


@pytest.mark.contract(id="GUI-SIG-WIN-001")
@pytest.mark.gui
def test_parent_expansion_requests_members_and_leaf_preview_without_dag(qapp):
    """Browse native collection members without materializing graph nodes."""
    bridge = _Bridge()
    window = MainWindow(bridge=bridge)
    parent = DataObjectRef(
        object_id="parent",
        kind="TimeSeriesDict",
        shape=(1,),
        dtype=None,
        unit=None,
        metadata={"member_count": 1},
    )
    window.project.objects = (parent,)
    window._current_object_id = "parent"
    window._refresh_project_views(selected_object_id="parent")
    item = window._find_object_item(window.source_tree, "parent")
    assert item is not None
    window.source_tree.setCurrentItem(item)
    item.setExpanded(True)
    qapp.processEvents()
    assert bridge.commands[-1] == ("list_members", {"object_id": "parent"})
    member = MemberRef(
        selector={"key": "X"},
        label="X",
        kind="TimeSeries",
        shape=(4,),
        dtype="float64",
        unit="m",
        axes={"t0": {"value": 0, "unit": "s"}, "dt": {"value": 1, "unit": "s"}},
    )
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=True,
            payload={"object_id": "parent", "members": (member,)},
        )
    )
    assert item.childCount() == 1
    window.source_tree.setCurrentItem(item.child(0))
    assert bridge.commands[-1] == (
        "member_preview",
        {"object_id": "parent", "selector": {"key": "X"}},
    )
    assert len(window.project.graph.operations) == 0
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract(id="GUI-SIG-WIN-002")
@pytest.mark.gui
def test_signal_panel_dispatch_retains_failed_values_and_global_busy_guard(qapp):
    """Preserve retry fields and disable global commands during preview."""
    bridge = _Bridge()
    window = MainWindow(bridge=bridge)
    window.project.objects = (
        DataObjectRef(
            object_id="a", kind="TimeSeries", shape=(4,), dtype="float64", unit="m"
        ),
    )
    window._current_object_id = "a"
    window.show_parameter_panel("timeseries.lowpass")
    panel = window.parameter_panel
    panel.fields["frequency"].setText("20")
    panel.preview_button.click()
    assert bridge.commands[-1][0] == "filter_preview"
    assert not window.open_action.isEnabled()
    assert not window.source_tree.isEnabled()
    assert not window.acceptDrops()
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=False,
            error_code="axes_mismatch",
            error_message="different times",
        )
    )
    assert panel.fields["frequency"].text() == "20"
    assert panel.preview_button.isEnabled()
    assert "axes_mismatch" in panel.error_label.text()
    assert window.open_action.isEnabled()
    panel.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract(id="GUI-SIG-WIN-003")
@pytest.mark.gui
def test_project_refresh_keeps_selected_member_and_plot_controls_guarded(qapp):
    """Keep visible selection and persisted settings aligned during plot saves."""
    from gwexpy_studio.domain.model import PlotSpec
    from gwexpy_studio.ui.models import MEMBER_ROLE

    bridge = _Bridge()
    window = MainWindow(bridge=bridge)
    member = MemberRef(
        selector={"key": "X"},
        label="X",
        kind="TimeSeries",
        shape=(2,),
        dtype="complex128",
        unit="m",
    )
    parent = DataObjectRef(
        object_id="parent",
        kind="TimeSeriesDict",
        shape=(1,),
        dtype=None,
        unit=None,
        members=(member,),
    )
    window.project.objects = (parent,)
    window._current_object_id = "parent"
    window._current_member = member
    window._refresh_project_views(selected_object_id="parent")
    assert window.source_tree.currentItem().data(0, MEMBER_ROLE) == member
    spec = PlotSpec(plot_id="plot", kind="line", object_ids=("parent",))
    window._save_plot_spec(spec)
    assert not window.plot_canvas._controls.isEnabled()
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1", success=True, payload={"project": window.project}
        )
    )
    assert window.source_tree.currentItem().data(0, MEMBER_ROLE) == member
    assert window._selected_handle() == {
        "object_id": "parent",
        "selector": {"key": "X"},
    }
    assert window.plot_canvas._controls.isEnabled()
    assert window.statusBar().currentMessage() == "Plot settings saved"
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract(id="GUI-SIG-WIN-004")
@pytest.mark.gui
def test_data_overwrite_requires_confirmation_before_dispatch(
    qapp, tmp_path, monkeypatch
):
    """Emit an overwrite authorization only after confirming the concrete target."""
    from PySide6.QtWidgets import QMessageBox

    from gwexpy_studio.ui.io_panel import DataIOPanel

    bridge = _Bridge()
    window = MainWindow(bridge=bridge)
    window.export_data_panel = DataIOPanel(direction="write", parent=window)
    window._write_handle = {"object_id": "a"}
    path = tmp_path / "existing.dat"
    path.write_text("existing")
    request = {"paths": [str(path)]}
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No
    )
    window._write_selected_data(request)
    assert not bridge.commands
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes
    )
    window._write_selected_data(request)
    assert bridge.commands[-1][1]["request"]["overwrite_confirmed"] is True
    assert "overwrite_confirmed" not in request
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract(id="GUI-SIG-WIN-005")
@pytest.mark.gui
def test_collection_members_can_be_loaded_beyond_first_page(qapp):
    """Browse a later native member page without adding graph operations."""
    bridge = _Bridge()
    window = MainWindow(bridge=bridge)
    parent = DataObjectRef(
        object_id="parent", kind="TimeSeriesList", shape=(101,), dtype=None, unit=None
    )
    window.project.objects = (parent,)
    window._current_object_id = "parent"
    window._refresh_project_views(selected_object_id="parent")
    item = window._find_object_item(window.source_tree, "parent")
    item.setExpanded(True)
    members = tuple(
        MemberRef(
            selector={"index": i},
            label=str(i),
            kind="TimeSeries",
            shape=(2,),
            dtype="float64",
            unit="m",
        )
        for i in range(100)
    )
    window._on_bridge_result(
        BridgeResult(
            command_id="command-1",
            success=True,
            payload={"object_id": "parent", "members": members},
        )
    )
    assert item.childCount() == 101
    assert "more" in item.child(100).text(0).lower()
    window.source_tree.setCurrentItem(item.child(100))
    assert bridge.commands[-1] == (
        "list_members",
        {"object_id": "parent", "offset": 100, "limit": 100},
    )
    last = MemberRef(
        selector={"index": 100},
        label="100",
        kind="TimeSeries",
        shape=(2,),
        dtype="float64",
        unit="m",
    )
    window._on_bridge_result(
        BridgeResult(
            command_id="command-2",
            success=True,
            payload={"object_id": "parent", "members": (last,)},
        )
    )
    assert item.childCount() == 101
    assert item.child(100).text(0) == "100"
    assert not window.project.graph.operations
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract(id="GUI-SIG-WIN-006")
@pytest.mark.gui
def test_native_source_paths_parent_objects_and_data_writes_have_history_rows(qapp):
    """Group native reads beneath their source and distinguish external writes."""
    from gwexpy_studio.domain.model import ActivityRecord, DataSourceRef, Operation
    from gwexpy_studio.ui.models import populate_history_list, populate_source_tree

    window = MainWindow(bridge=_Bridge())
    window.project.sources = (
        DataSourceRef(source_id="source", uri="/a.dat", format="custom"),
    )
    window.project.graph.add(
        Operation(
            op_id="read",
            operation_id="data.read",
            operation_schema=1,
            params={"source": ["/a.dat", "/b.dat"]},
            outputs=("parent",),
        )
    )
    window.project.objects = (
        DataObjectRef(
            object_id="parent",
            kind="TimeSeriesDict",
            shape=(1,),
            dtype=None,
            unit=None,
            produced_by="read",
        ),
    )
    window.project.activities = (
        ActivityRecord(
            activity_id="write",
            action="write_data",
            started_at="now",
            status="failed",
            target="/out.dat",
            error={"code": "missing_dependency", "message": "install reader"},
        ),
    )
    populate_source_tree(window.source_tree, window.project)
    assert window.source_tree.topLevelItemCount() == 1
    assert window.source_tree.topLevelItem(0).child(0).text(0) == "parent"
    populate_history_list(window.history_list, window.project)
    assert window.history_list.count() == 2
    assert "Data write:" in window.history_list.item(1).text()
    assert "missing_dependency" in window.history_list.item(1).text()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.contract(id="GUI-SIG-WIN-007")
@pytest.mark.gui
def test_reopening_filter_recipe_retains_explicit_quantity_units(qapp):
    """Keep unit-scaled cutoffs editable without silently changing the filter."""
    from gwexpy_studio.domain.model import Operation

    bridge = _Bridge()
    window = MainWindow(bridge=bridge)
    window.project.objects = (
        DataObjectRef(
            object_id="a", kind="TimeSeries", shape=(2,), dtype="float64", unit="m"
        ),
        DataObjectRef(
            object_id="filtered",
            kind="TimeSeries",
            shape=(2,),
            dtype="float64",
            unit="m",
            produced_by="filter",
        ),
    )
    window.project.graph.add(
        Operation(
            op_id="read",
            operation_id="data.read",
            operation_schema=1,
            outputs=("a",),
        )
    )
    window.project.graph.add(
        Operation(
            op_id="filter",
            operation_id="timeseries.lowpass",
            operation_schema=1,
            inputs={"self": "a"},
            outputs=("filtered",),
            params={"frequency": {"value": 2, "unit": "kHz"}, "filtfilt": True},
        )
    )
    window._current_object_id = "filtered"
    window._show_applied_response()
    assert bridge.commands[-1][0] == "filter_preview"
    assert window.parameter_panel.request()["params"]["frequency"] == {
        "value": 2,
        "unit": "kHz",
    }
    window.parameter_panel.close()
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.gui
@pytest.mark.parametrize(
    "target_kind",
    [
        pytest.param(
            "home",
            id="home-expansion",
            marks=pytest.mark.contract(id="GUI-SIG-WIN-008"),
        ),
        pytest.param(
            "symlink",
            id="dangling-symlink",
            marks=pytest.mark.contract(id="GUI-SIG-WIN-009"),
        ),
    ],
)
def test_data_overwrite_checks_expanded_home_and_dangling_symlink(
    qapp, tmp_path, monkeypatch, target_kind
):
    """Confirm existing filesystem entries before dispatching normalized paths."""
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from PySide6.QtWidgets import QMessageBox

    bridge = _Bridge()
    window = MainWindow(bridge=bridge)
    window._write_handle = {"object_id": "a"}
    prompts = []

    def confirm(*args):
        prompts.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    with TemporaryDirectory(prefix=".studio-test-", dir=Path.home()) as directory:
        if target_kind == "home":
            target = Path(directory) / "existing.dat"
            target.write_text("existing")
            entered = "~/" + str(target.relative_to(Path.home()))
        else:
            target = tmp_path / "dangling.dat"
            target.symlink_to(tmp_path / "missing.dat")
            entered = str(target)
        request = {"paths": [entered]}
        window._write_selected_data(request)
        assert len(prompts) == 1
        sent = bridge.commands[-1][1]["request"]
        assert sent["paths"] == [str(target)]
        assert sent["overwrite_confirmed"] is True
        assert request == {"paths": [entered]}
    window.deleteLater()
    qapp.processEvents()
