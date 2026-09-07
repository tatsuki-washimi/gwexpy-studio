"""Interactive Matplotlib canvas widget for Qt."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal, cast

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..application.preview import PreviewData
from ..domain.model import PlotSpec
from ..plotting.preview_renderer import render_preview


class _ReferenceSpinBox(QDoubleSpinBox):
    """Show dB reference values without filling the toolbar with trailing zeros."""

    def textFromValue(self, value: float) -> str:
        return f"{value:.12g}"


class PlotCanvas(QWidget):
    """Interactive Matplotlib canvas widget with navigation and cursor tracking."""

    cursor_moved = Signal(str)
    spec_changed = Signal(object)
    view_changed = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize canvas layout, toolbar, and event tracking."""
        super().__init__(parent)
        self._fig = Figure(figsize=(8, 5))
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._toolbar = NavigationToolbar2QT(self._canvas, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._toolbar)
        self._controls = QWidget(self)
        controls = QHBoxLayout(self._controls)
        controls.setContentsMargins(6, 0, 6, 0)
        self.component_combo = QComboBox(self)
        self.component_combo.addItems(["real", "imag", "abs", "phase"])
        self.magnitude_combo = QComboBox(self)
        self.magnitude_combo.addItems(["db", "linear"])
        self.db_reference = _ReferenceSpinBox(self)
        self.db_reference.setDecimals(12)
        self.db_reference.setRange(1e-12, 1e100)
        self.db_reference.setValue(1.0)
        self.db_reference.setMaximumWidth(120)
        self.display_unit = QLineEdit(self)
        self.display_unit.setPlaceholderText("native unit")
        self.display_unit.setMaximumWidth(100)
        self.phase_unwrap = QCheckBox("Unwrap phase", self)
        self.phase_unwrap.setChecked(True)
        self._control_labels: dict[str, QLabel] = {}
        for label, control in (
            ("Component", self.component_combo),
            ("Magnitude", self.magnitude_combo),
            ("Reference", self.db_reference),
            ("Unit", self.display_unit),
        ):
            self._control_labels[label] = QLabel(label, self)
            controls.addWidget(self._control_labels[label])
            controls.addWidget(control)
        controls.addWidget(self.phase_unwrap)
        controls.addStretch()
        self.display_error = QLabel(self)
        self.display_error.setStyleSheet("color: #bd3a30")
        layout.addWidget(self._controls)
        layout.addWidget(self.display_error)
        layout.addWidget(self._canvas)

        self._canvas.mpl_connect("motion_notify_event", self._on_motion_notify)
        self._preview: PreviewData | None = None
        self._spec: PlotSpec | None = None
        self._view_timer = QTimer(self)
        self._view_timer.setSingleShot(True)
        self._view_timer.setInterval(100)
        self._view_timer.timeout.connect(self._capture_limits)
        self.component_combo.currentTextChanged.connect(self._change_spec)
        self.magnitude_combo.currentTextChanged.connect(self._change_spec)
        self.db_reference.valueChanged.connect(self._change_spec)
        self.display_unit.editingFinished.connect(self._change_spec)
        self.phase_unwrap.toggled.connect(self._change_spec)
        self._controls.hide()

    @property
    def canvas(self) -> FigureCanvasQTAgg:
        """Access FigureCanvas instance."""
        return self._canvas

    @property
    def toolbar(self) -> NavigationToolbar2QT:
        """Access NavigationToolbar instance."""
        return self._toolbar

    @property
    def figure(self) -> Figure:
        """Access current figure."""
        return self._fig

    def set_preview(
        self,
        preview: PreviewData,
        spec: PlotSpec,
    ) -> None:
        """Render new preview onto canvas."""
        render_preview(preview, spec, fig=self._fig)
        self._preview = preview
        self._spec = spec
        self._sync_controls()
        self._toolbar.update()
        self._canvas.draw_idle()
        self._connect_axes()

    def _connect_axes(self) -> None:
        self._view_timer.stop()
        self._canvas.draw()
        for axis in self._fig.axes:
            axis.callbacks.connect(
                "xlim_changed", lambda _axis: self._view_timer.start()
            )
            axis.callbacks.connect(
                "ylim_changed", lambda _axis: self._view_timer.start()
            )

    def flush_view_changes(self) -> None:
        """Capture pending pan/zoom limits before a document save boundary."""
        if self._view_timer.isActive():
            self._view_timer.stop()
            self._capture_limits()

    def _capture_limits(self) -> None:
        if self._spec is None or not self._fig.axes:
            return
        first = self._fig.axes[0]
        updated = replace(
            self._spec,
            xlim=cast(tuple[float, float], tuple(first.get_xlim())),
            ylim=cast(tuple[float, float], tuple(first.get_ylim())),
            phase_ylim=cast(tuple[float, float], tuple(self._fig.axes[1].get_ylim()))
            if self._spec.kind == "bode"
            else self._spec.phase_ylim,
        )
        if updated != self._spec:
            previous, self._spec = self._spec, updated
            self.view_changed.emit(previous, updated)
            self.spec_changed.emit(updated)

    def apply_spec(self, spec: PlotSpec) -> None:
        """Render a view-history entry while retaining the detached source array."""
        if self._preview is not None:
            self.set_preview(self._preview, spec)
            self.spec_changed.emit(spec)

    def _sync_controls(self) -> None:
        if self._spec is None or self._preview is None:
            return
        controls = (
            self.component_combo,
            self.magnitude_combo,
            self.db_reference,
            self.display_unit,
            self.phase_unwrap,
        )
        blockers = [QSignalBlocker(control) for control in controls]
        self.component_combo.setCurrentText(self._spec.component)
        self.magnitude_combo.setCurrentText(self._spec.magnitude_scale)
        self.db_reference.setValue(self._spec.db_reference)
        self.display_unit.setText(self._spec.display_unit or "")
        self.phase_unwrap.setChecked(self._spec.phase_unwrap)
        bode = self._spec.kind == "bode"
        self.component_combo.setVisible(not bode)
        self._control_labels["Component"].setVisible(not bode)
        self.magnitude_combo.setVisible(bode)
        self._control_labels["Magnitude"].setVisible(bode)
        self.db_reference.setVisible(bode)
        self._control_labels["Reference"].setVisible(bode)
        self.db_reference.setEnabled(bode and self._spec.magnitude_scale == "db")
        self._controls.setVisible(
            np.iscomplexobj(self._preview.values) or self._spec.kind == "bode"
        )
        del blockers

    def _change_spec(self, *_args: Any) -> None:
        if self._spec is None or self._preview is None:
            return
        spec = replace(
            self._spec,
            component=cast(
                Literal["real", "imag", "abs", "phase"],
                self.component_combo.currentText(),
            ),
            magnitude_scale=cast(
                Literal["db", "linear"], self.magnitude_combo.currentText()
            ),
            db_reference=self.db_reference.value(),
            display_unit=self.display_unit.text().strip() or None,
            phase_unwrap=self.phase_unwrap.isChecked(),
        )
        try:
            render_preview(self._preview, spec, fig=self._fig)
        except (ValueError, TypeError) as exc:
            self.display_error.setText(str(exc))
            return
        self.display_error.clear()
        previous, self._spec = self._spec, spec
        self._sync_controls()
        self._toolbar.update()
        self._canvas.draw_idle()
        self._connect_axes()
        self.view_changed.emit(previous, spec)
        self.spec_changed.emit(spec)

    def clear(self) -> None:
        """Clear canvas."""
        self._preview = None
        self._spec = None
        self._view_timer.stop()
        self._controls.hide()
        self.display_error.clear()
        for ax in self._fig.axes:
            ax.set_xscale("linear")
        self._fig.clear()
        self._canvas.draw_idle()

    def _on_motion_notify(self, event: Any) -> None:
        """Handle mouse motion over canvas axes to emit formatted coordinate text."""
        if event.inaxes and event.xdata is not None and event.ydata is not None:
            text = f"x={event.xdata:.4f}, y={event.ydata:.4f}"
            self.cursor_moved.emit(text)
        else:
            self.cursor_moved.emit("")
