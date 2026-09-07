"""Guard application operations before their inputs can change provenance."""

from unittest.mock import MagicMock

import pytest

from gwexpy_studio.application.alpha import AlphaController
from gwexpy_studio.application.project_factory import create_alpha_project
from gwexpy_studio.domain.model import DataObjectRef
from gwexpy_studio.errors import OperationError
from gwexpy_studio.session import StudioSession


@pytest.mark.contract("C-APP-029")
def test_timeseries_operations_reject_other_kinds_without_mutation() -> None:
    for kind in ("FrequencySeries", "Spectrogram"):
        for operation, params in (
            ("timeseries.crop", {"start": 0.0, "end": 0.5}),
            ("timeseries.detrend", {"detrend": "linear"}),
            ("timeseries.asd", {"fftlength": 0.125}),
            ("timeseries.spectrogram", {"stride": 0.25}),
        ):
            project = create_alpha_project()
            obj = DataObjectRef(
                object_id="obj-spectrum",
                kind=kind,
                shape=(4,) if kind == "FrequencySeries" else (4, 4),
                dtype="float64",
                unit="m",
                axes={},
            )
            project.objects = (obj,)
            session = MagicMock(spec=StudioSession)
            controller = AlphaController(project=project, session=session)
            original_graph = project.graph

            with pytest.raises(OperationError) as caught:
                controller.apply(operation, obj.object_id, params)

            assert caught.value.code == "invalid_input_kind"
            assert project.graph is original_graph
            assert project.graph.operations == ()
            assert project.objects == (obj,)
            assert project.executions == ()
            session.start.assert_not_called()
            session.replay.assert_not_called()
