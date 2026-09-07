"""Review regressions for recorded filter execution and persisted plot members."""

from __future__ import annotations

import numpy as np
import pytest

from gwexpy_studio.domain.model import DataObjectRef, Operation, PlotSpec
from gwexpy_studio.domain.project import Project
from gwexpy_studio.errors import OperationError, ProjectFormatError
from gwexpy_studio.runtime.executor import execute_operation_detailed
from gwexpy_studio.runtime.store import ObjectStore


@pytest.mark.parametrize("operation_id", ["timeseries.psd", "timeseries.highpass"])
def test_recorded_filter_recipe_cannot_execute_a_different_operation(operation_id):
    from gwexpy.timeseries import TimeSeries

    store = ObjectStore()
    store.put(TimeSeries(np.arange(128.0), dt=1 / 128, name="source"))
    operation = Operation(
        op_id="op-1",
        operation_id=operation_id,
        operation_schema=1,
        inputs={"self": "obj-1"},
        params={"frequency": 10} if operation_id.endswith("highpass") else {},
        outputs=("obj-2",),
    )
    details = {
        "filter_recipes": [
            {
                "format": "sos",
                "coefficients": [[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]],
                "sample_rate": 128.0,
                "filtfilt": False,
                "operation": "timeseries.lowpass",
                "label": "source",
            }
        ]
    }
    with pytest.raises(OperationError, match="recorded|recipe"):
        execute_operation_detailed(operation, store=store, recorded_details=details)
    assert len(store) == 1


def _project():
    return Project(
        project_id="plots",
        compatibility={"studio": "0.1", "gwexpy": "0.2"},
        objects=(
            DataObjectRef(
                object_id="obj-1",
                kind="TimeSeriesDict",
                shape=(1,),
                dtype="float64",
                unit="m",
            ),
        ),
    )


@pytest.mark.contract("SIG-0075")
def test_plot_member_selectors_roundtrip_with_native_key_type():
    project = _project()
    project.plots = (
        PlotSpec(
            plot_id="plot-1-key=7",
            kind="line",
            object_ids=("obj-1",),
            selectors={"obj-1": {"key": 7}},
        ),
    )
    loaded = Project.from_dict(project.to_dict())
    assert loaded.plots[0].selectors == {"obj-1": {"key": 7}}
    assert loaded.plots == project.plots


@pytest.mark.parametrize(
    "selectors",
    [
        {"obj-2": {"key": "missing"}},
        {"obj-1": {"index": True}},
        {"obj-1": {"row": -1, "col": 0}},
        {"obj-1": {"key": "x", "index": 0}},
    ],
)
def test_invalid_persisted_plot_member_selectors_rejected(selectors):
    project = _project()
    project.plots = (PlotSpec(plot_id="plot-1", kind="line", object_ids=("obj-1",)),)
    document = project.to_dict()
    document["plots"][0]["selectors"] = selectors
    with pytest.raises(ProjectFormatError, match="selector"):
        Project.from_dict(document)
