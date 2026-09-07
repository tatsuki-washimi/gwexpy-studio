"""Tests for interactive Qt PlotCanvas widget."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import gwexpy_studio.application as application
from gwexpy_studio.application import AlphaController
from gwexpy_studio.application.preview import PreviewData
from gwexpy_studio.domain.model import DataObjectRef, PlotSpec
from gwexpy_studio.ui.bridge import BridgeResult, BridgeState, BridgeWorker
from gwexpy_studio.ui.plot_canvas import PlotCanvas
from gwexpy_studio.ui.window import MainWindow

if TYPE_CHECKING:
    from gwexpy_studio.application import PreviewPayload


class _SignalBridge(QObject):
    """Signal-only bridge for synchronous MainWindow payload assertions."""

    result_received = Signal(object)
    safe_to_destroy = Signal()
    state_changed = Signal(str)
    state = BridgeState.IDLE

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def send_command(self, kind: str, payload: dict[str, Any]) -> str:
        """Allow the window to reserve the synthetic preview request."""
        self.commands.append((kind, payload))
        return "preview-command"


@pytest.fixture
def timeseries_preview() -> PreviewData:
    """Fixture providing sample TimeSeries PreviewData."""
    ref = DataObjectRef(
        object_id="obj-1",
        kind="TimeSeries",
        shape=(100,),
        dtype="float64",
        unit="m",
        name="H1:STRAIN",
        axes={
            "t0": {"value": 0.0, "unit": "s"},
            "dt": {"value": 0.01, "unit": "s"},
        },
    )
    values = np.sin(np.linspace(0, 10, 100))
    return PreviewData(ref=ref, values=values, unit="m")


@pytest.fixture
def timeseries_spec(timeseries_preview: PreviewData) -> PlotSpec:
    """Complete alpha-default PlotSpec matching ``timeseries_preview``."""
    return _line_spec(timeseries_preview)


def _line_spec(
    preview: PreviewData, *, object_ids: tuple[str, ...] | None = None
) -> PlotSpec:
    label = preview.ref.name or preview.ref.object_id
    return PlotSpec(
        plot_id="plot-1",
        kind="line",
        object_ids=(preview.ref.object_id,) if object_ids is None else object_ids,
        xscale="linear",
        yscale="linear",
        title=label,
        xlabel="Time [s]",
        ylabel="Amplitude [m]",
        legend=True,
        styles={"label": label},
    )


def _spectrogram_preview() -> PreviewData:
    ref = DataObjectRef(
        object_id="obj-2",
        kind="Spectrogram",
        shape=(3, 4),
        dtype="float64",
        unit="m",
        axes={
            "t0": {"value": 0.0, "unit": "s"},
            "dt": {"value": 0.1, "unit": "s"},
            "f0": {"value": 10.0, "unit": "Hz"},
            "df": {"value": 1.0, "unit": "Hz"},
        },
    )
    return PreviewData(
        ref=ref,
        values=np.arange(12, dtype=np.float64).reshape(3, 4),
        unit="m",
    )


def _frequency_preview() -> PreviewData:
    ref = DataObjectRef(
        object_id="obj-frequency",
        kind="FrequencySeries",
        shape=(4,),
        dtype="float64",
        unit="m",
        axes={
            "f0": {"value": 1.0, "unit": "Hz"},
            "df": {"value": 0.5, "unit": "Hz"},
        },
    )
    return PreviewData(ref=ref, values=np.ones(4, dtype=np.float64), unit="m")


def _unsupported_preview() -> PreviewData:
    ref = DataObjectRef(
        object_id="obj-unknown",
        kind="TimeSeries",
        shape=(2,),
        dtype="float64",
        unit="m",
        axes={
            "t0": {"value": 0.0, "unit": "s"},
            "dt": {"value": 1.0, "unit": "s"},
        },
    )
    object.__setattr__(ref, "kind", "Unknown")
    preview = PreviewData.__new__(PreviewData)
    object.__setattr__(preview, "ref", ref)
    object.__setattr__(preview, "values", np.ones(2, dtype=np.float64))
    object.__setattr__(preview, "unit", "m")
    return preview


def _canvas_rejection_cases(
    preview: PreviewData, valid_spec: PlotSpec
) -> tuple[tuple[object, object, type[BaseException]], ...]:
    frequency = _frequency_preview()
    spectrogram = _spectrogram_preview()
    return (
        ("not preview data", valid_spec, TypeError),
        (preview, None, TypeError),
        (preview, object(), TypeError),
        (preview, _line_spec(preview, object_ids=()), ValueError),
        (preview, _line_spec(preview, object_ids=("obj-other",)), ValueError),
        (
            preview,
            _line_spec(preview, object_ids=(preview.ref.object_id, "obj-other")),
            ValueError,
        ),
        (
            preview,
            PlotSpec(
                plot_id="plot-1",
                kind="spectrogram",
                object_ids=(preview.ref.object_id,),
            ),
            ValueError,
        ),
        (
            frequency,
            PlotSpec(
                plot_id="plot-frequency",
                kind="spectrogram",
                object_ids=(frequency.ref.object_id,),
            ),
            ValueError,
        ),
        (
            spectrogram,
            PlotSpec(
                plot_id="plot-2",
                kind="line",
                object_ids=(spectrogram.ref.object_id,),
            ),
            ValueError,
        ),
        (
            _unsupported_preview(),
            PlotSpec(
                plot_id="plot-unknown",
                kind="line",
                object_ids=("obj-unknown",),
            ),
            ValueError,
        ),
    )


def _assert_canvas_structural_rejections(
    canvas: PlotCanvas, preview: PreviewData, spec: PlotSpec
) -> None:
    for candidate_preview, candidate_spec, expected_error in _canvas_rejection_cases(
        preview, spec
    ):
        axes_before = tuple(canvas.figure.axes)
        ax = axes_before[0]
        line = ax.lines[0]
        xdata_before = np.asarray(line.get_xdata()).copy()
        title_before = ax.get_title()
        preview_before = canvas._preview
        spec_before = canvas._spec

        with pytest.raises(expected_error):
            canvas.set_preview(  # type: ignore[arg-type]
                candidate_preview, candidate_spec
            )

        assert tuple(canvas.figure.axes) == axes_before
        assert canvas.figure.axes[0] is ax
        assert ax.lines[0] is line
        assert np.array_equal(line.get_xdata(), xdata_before)
        assert ax.get_title() == title_before
        assert canvas._preview is preview_before
        assert canvas._spec is spec_before


def _assert_bridge_and_window_propagation(
    qapp: QApplication, payload: PreviewPayload
) -> None:
    controller = MagicMock(spec=AlphaController)
    controller.fetch_preview_payload.return_value = payload
    worker = BridgeWorker(controller=controller)

    response = worker._execute(
        controller,
        "preview",
        {"object_id": payload.preview.ref.object_id, "preview_stride": 4},
    )

    assert set(response) == {"preview_payload"}
    assert response == {"preview_payload": payload}
    controller.fetch_preview_payload.assert_called_once_with(
        payload.preview.ref.object_id, preview_stride=4
    )
    controller.fetch_preview.assert_not_called()

    bridge = _SignalBridge()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    try:
        window.project.objects = (payload.preview.ref,)
        window._current_object_id = payload.preview.ref.object_id
        initial_widget = window.central_stack.currentWidget()
        with patch.object(window.plot_canvas, "set_preview") as forwarded:
            window._dispatch_command(
                "preview",
                {"object_id": payload.preview.ref.object_id},
                pending_action="preview",
            )
            window._on_bridge_result(
                BridgeResult(
                    command_id="preview-command",
                    success=True,
                    payload={"preview_payload": object()},
                )
            )

            forwarded.assert_not_called()
            assert window.central_stack.currentWidget() is initial_widget
            assert window.statusBar().currentMessage() == (
                "Error: [invalid_response] Invalid preview payload"
            )
            assert window.open_action.isEnabled()
            assert window.export_action.isEnabled()
            assert window.source_tree.isEnabled()
            assert window.acceptDrops()

            window._dispatch_command(
                "preview",
                {"object_id": payload.preview.ref.object_id},
                pending_action="preview",
            )
            window._on_bridge_result(
                BridgeResult(
                    command_id="preview-command",
                    success=True,
                    payload=response,
                )
            )

        forwarded.assert_called_once_with(payload.preview, payload.spec)
        assert window.central_stack.currentWidget() is window.plot_canvas
        assert window.statusBar().currentMessage() == "Preview loaded"
    finally:
        window.deleteLater()
        qapp.processEvents()


class TestPlotCanvas:
    """Validate PlotCanvas widget rendering and toolbar interaction."""

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-PLT-001")
    def test_plot_canvas_set_preview(
        self,
        qapp: QApplication,
        timeseries_preview: PreviewData,
        timeseries_spec: PlotSpec,
    ) -> None:
        """Canvas and window accept only one coherent propagated preview spec."""
        canvas = PlotCanvas()
        canvas.set_preview(timeseries_preview, timeseries_spec)
        qapp.processEvents()

        assert canvas.figure is not None
        assert len(canvas.figure.axes) >= 1
        ax = canvas.figure.axes[0]
        assert ax.get_xlabel() == "Time [s]"
        assert "Amplitude [m]" in ax.get_ylabel()
        assert canvas._preview is timeseries_preview
        assert canvas._spec is timeseries_spec

        _assert_canvas_structural_rejections(
            canvas, timeseries_preview, timeseries_spec
        )

        canvas.clear()
        qapp.processEvents()
        assert len(canvas.figure.axes) == 0
        assert canvas._preview is None
        assert canvas._spec is None

        payload_type: Any = getattr(application, "PreviewPayload", None)
        assert payload_type is not None, "application must export PreviewPayload"
        payload = payload_type(preview=timeseries_preview, spec=timeseries_spec)
        _assert_bridge_and_window_propagation(qapp, payload)

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-PLT-002")
    def test_plot_canvas_navigation_toolbar(
        self,
        qapp: QApplication,
        timeseries_preview: PreviewData,
        timeseries_spec: PlotSpec,
    ) -> None:
        """Navigation toolbar exists and zoom/pan actions are accessible."""
        canvas = PlotCanvas()
        canvas.set_preview(timeseries_preview, timeseries_spec)
        qapp.processEvents()

        assert canvas.toolbar is not None
        # Verify zoom / pan toolbar modes do not error
        canvas.toolbar.zoom()
        canvas.toolbar.pan()
        qapp.processEvents()

    @pytest.mark.gui
    @pytest.mark.contract(id="GUI-PLT-003")
    def test_plot_canvas_motion_notify(
        self,
        qapp: QApplication,
        timeseries_preview: PreviewData,
        timeseries_spec: PlotSpec,
    ) -> None:
        """Motion notify triggers cursor_moved signal."""
        canvas = PlotCanvas()
        canvas.set_preview(timeseries_preview, timeseries_spec)
        qapp.processEvents()

        coords: list[str] = []
        canvas.cursor_moved.connect(coords.append)

        # Simulate motion event
        ax = canvas.figure.axes[0]
        from matplotlib.backend_bases import MouseEvent

        event = MouseEvent(
            "motion_notify_event",
            canvas.canvas,
            x=100,
            y=100,
            guiEvent=None,
        )
        event.inaxes = ax
        event.xdata = 0.5
        event.ydata = 1.2
        canvas._on_motion_notify(event)

        assert len(coords) == 1
        assert "x=0.5000" in coords[0]
        assert "y=1.2000" in coords[0]
