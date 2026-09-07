"""Read identity survives duplicate paths, failed reads and document replay."""

import pytest

from gwexpy_studio.application.workspace_controller import WorkspaceController
from gwexpy_studio.domain.history import active_source_ids
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import OperationError, ProjectFormatError


@pytest.mark.contract("WSP-0113")
def test_repeated_native_read_keeps_source_identity_after_undo_and_restore(
    tmp_path, hdf5_source
):
    """The retained object belongs to its original source after a second read."""
    controller = WorkspaceController(recovery=False)
    try:
        inspection = controller.inspect_io(
            {"paths": [str(hdf5_source)], "datatype": "TimeSeries", "format": "hdf5"}
        )
        first = controller.read_io(inspection)[0]
        second = controller.read_io(inspection)[0]
        controller.undo_analysis()
        assert active_source_ids(controller.project) == {"src-1"}
        path = tmp_path / "重複入力.gwxproj"
        controller.save_workspace(path)
        controller.open_workspace(path)
        assert active_source_ids(controller.project) == {"src-1"}
        controller.restore_workspace(controller.review_restore(), confirmed=True)
        assert active_source_ids(controller.project) == {"src-1"}
        with pytest.raises(OperationError, match="Restore redo"):
            controller.redo_analysis()
        controller.restore_workspace(
            controller.review_restore(redo=True), confirmed=True
        )
        assert active_source_ids(controller.project) == {"src-1", "src-2"}
        assert controller.project.source_bindings[first.produced_by] == ("src-1",)
        assert controller.project.source_bindings[second.produced_by] == ("src-2",)
        assert set(controller.workspace_status()["resident_object_ids"]) == {
            first.object_id,
            second.object_id,
        }
    finally:
        controller.finalize_workspace(clean=True)


@pytest.mark.contract("WSP-0115")
def test_combined_source_bindings_and_legacy_format_identity():
    """Combined reads retain all roots; legacy duplicates have one canonical row."""
    from gwexpy_studio.application.project_factory import create_alpha_project
    from gwexpy_studio.domain.model import DataSourceRef, Operation
    from gwexpy_studio.domain.sources import source_ids_for_operation

    project = create_alpha_project()
    project.sources = tuple(
        DataSourceRef(source_id=ident, uri=path, format=fmt)
        for ident, path, fmt in (
            ("src-1", "/data", "csv"),
            ("src-2", "/data", "hdf5"),
            ("src-3", "/data", "hdf5"),
            ("src-4", "/other", "hdf5"),
        )
    )
    op = Operation(
        op_id="op-1",
        operation_id="data.read",
        operation_schema=1,
        inputs={},
        params={"source": ["/data", "/other"], "format": "hdf5"},
        outputs=("obj-1",),
    )
    project.graph.add(op)
    project.source_bindings = {op.op_id: ("src-3", "src-4")}
    restored = Project.from_dict(project.to_dict())
    assert source_ids_for_operation(restored, op) == ("src-3", "src-4")
    legacy = project.to_dict()
    legacy.pop("source_bindings")
    assert source_ids_for_operation(Project.from_dict(legacy), op) == ("src-2", "src-4")
    for ids in (["src-4", "src-3"], ["src-1", "src-1"]):
        with pytest.raises(ProjectFormatError, match="source_bindings"):
            Project.from_dict({**legacy, "source_bindings": {op.op_id: ids}})
    duplicate_ids = {**legacy, "sources": [*legacy["sources"], legacy["sources"][0]]}
    with pytest.raises(ProjectFormatError, match="duplicate source ID"):
        Project.from_dict(duplicate_ids)


@pytest.mark.contract("WSP-0114")
def test_source_bindings_keep_individual_order_after_failed_read(
    tmp_path, hdf5_source, monkeypatch
):
    """Failed source rows and duplicate URIs cannot steal later read results."""
    controller = WorkspaceController(recovery=False)
    try:
        second_path = tmp_path / "second.h5"
        second_path.write_bytes(hdf5_source.read_bytes())
        inspection = controller.inspect_io(
            {
                "paths": [str(hdf5_source), str(second_path)],
                "datatype": "TimeSeries",
                "format": "hdf5",
                "combine": "individual",
            }
        )
        original = controller._execute_signal
        with monkeypatch.context() as patch:
            patch.setattr(
                controller,
                "_execute_signal",
                lambda *a, **k: (_ for _ in ()).throw(
                    OperationError("reader failed", code="read_failed")
                ),
            )
            with pytest.raises(OperationError, match="reader failed"):
                controller.read_io(inspection)
        assert controller._execute_signal == original
        results = controller.read_io(inspection)
        assert active_source_ids(controller.project) == {"src-3", "src-4"}
        assert [
            controller.project.source_bindings[obj.produced_by] for obj in results
        ] == [("src-3",), ("src-4",)]
        saved = controller.project.to_dict()
        for invalid in (
            {"missing": ["src-3"]},
            {results[0].produced_by: ["missing"]},
            {results[0].produced_by: "src-3"},
            {results[0].produced_by: []},
        ):
            with pytest.raises(ProjectFormatError, match="source_bindings"):
                Project.from_dict({**saved, "source_bindings": invalid})
    finally:
        controller.finalize_workspace(clean=True)
