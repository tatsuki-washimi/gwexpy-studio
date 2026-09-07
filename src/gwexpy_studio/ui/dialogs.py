"""Fixed dialogs for source confirmation and curated alpha operations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..ops.source import SourceInspection


class LoadConfirmationDialog(QDialog):
    """Show immutable inspection metadata before a source is loaded."""

    def __init__(
        self, inspection: SourceInspection, parent: QWidget | None = None
    ) -> None:
        """Initialize the dialog with one worker-produced inspection token."""
        super().__init__(parent)
        self.setWindowTitle("Confirm Data Load")
        self.inspection = inspection

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Resolved path", QLabel(inspection.resolved_uri or "<unavailable>"))
        form.addRow("Format", QLabel(inspection.format_guess or "<unknown>"))
        form.addRow(
            "Size",
            QLabel(
                "<unavailable>"
                if inspection.size_bytes is None
                else f"{inspection.size_bytes} bytes"
            ),
        )
        form.addRow(
            "Modified",
            QLabel(
                "<unavailable>" if inspection.mtime is None else str(inspection.mtime)
            ),
        )
        layout.addLayout(form)

        self.can_load = (
            inspection.exists
            and inspection.resolved_uri is not None
            and inspection.format_guess == "csv_enhanced"
        )
        if not self.can_load:
            layout.addWidget(
                QLabel("This source cannot be loaded by the alpha CSV reader.")
            )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok,
            parent=self,
        )
        self.load_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.load_button.setText("Load")
        self.load_button.setEnabled(self.can_load)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class _OperationDialog(QDialog):
    """Base class that returns only a raw operation parameter mapping."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._layout = QVBoxLayout(self)
        self._form = QFormLayout()
        self._layout.addLayout(self._form)
        self._params: dict[str, Any] | None = None
        self.validation_label = QLabel(self)
        self.validation_label.setObjectName("validationError")
        self.validation_label.setStyleSheet("color: #b00020;")
        self.validation_label.setWordWrap(True)
        self.validation_label.hide()
        self._layout.addWidget(self.validation_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok,
            parent=self,
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        self._layout.addWidget(buttons)

    @property
    def params(self) -> Mapping[str, Any]:
        """Return the selected raw mapping after an accepted dialog."""
        if self._params is None:
            raise RuntimeError("Operation dialog has not been accepted")
        return self._params

    @staticmethod
    def _optional_finite(value: str, *, label: str) -> float | None:
        value = value.strip()
        if not value:
            return None
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be a number") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{label} must be finite")
        return parsed

    def _collect_params(self) -> dict[str, Any]:
        raise NotImplementedError

    def _accept_if_valid(self) -> None:
        self._params = None
        try:
            params = self._collect_params()
        except ValueError as exc:
            self.validation_label.setText(str(exc))
            self.validation_label.show()
            return
        self.validation_label.clear()
        self.validation_label.hide()
        self._params = params
        self.accept()


class CropDialog(_OperationDialog):
    """Collect optional crop boundaries in canonical seconds."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize optional crop-boundary inputs."""
        super().__init__("Crop Time Series", parent)
        self.start_edit = QLineEdit(self)
        self.end_edit = QLineEdit(self)
        self._form.addRow("Start [s] (optional)", self.start_edit)
        self._form.addRow("End [s] (optional)", self.end_edit)

    def _collect_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        start = self._optional_finite(self.start_edit.text(), label="Start")
        end = self._optional_finite(self.end_edit.text(), label="End")
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if start is not None and end is not None and start >= end:
            raise ValueError("Start must be before end")
        return params


class DetrendDialog(_OperationDialog):
    """Collect one supported detrend mode."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize the detrend-mode selector."""
        super().__init__("Detrend Time Series", parent)
        self.detrend_combo = QComboBox(self)
        self.detrend_combo.addItems(["constant", "linear"])
        self._form.addRow("Method", self.detrend_combo)

    def _collect_params(self) -> dict[str, Any]:
        return {"detrend": self.detrend_combo.currentText()}


class _SpectrumDialog(_OperationDialog):
    """Shared optional spectral parameter widgets."""

    def _add_optional_spectrum_fields(self) -> None:
        self.fftlength_edit = QLineEdit(self)
        self.overlap_edit = QLineEdit(self)
        self.window_edit = QLineEdit(self)
        self.method_combo = QComboBox(self)
        self.method_combo.addItems(["", "welch", "bartlett", "median", "median-mean"])
        self._form.addRow("FFT length [s] (optional)", self.fftlength_edit)
        self._form.addRow("Overlap [s] (optional)", self.overlap_edit)
        self._form.addRow("Window (optional)", self.window_edit)
        self._form.addRow("Method (optional)", self.method_combo)

    def _optional_spectrum_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {}
        fftlength = self._optional_finite(
            self.fftlength_edit.text(), label="FFT length"
        )
        overlap = self._optional_finite(self.overlap_edit.text(), label="Overlap")
        window = self.window_edit.text().strip()
        method = self.method_combo.currentText()
        if fftlength is not None and fftlength <= 0:
            raise ValueError("FFT length must be positive")
        if overlap is not None and overlap < 0:
            raise ValueError("Overlap must be non-negative")
        if overlap is not None and fftlength is not None and overlap >= fftlength:
            raise ValueError("Overlap must be shorter than FFT length")
        if fftlength is not None:
            params["fftlength"] = fftlength
        if overlap is not None:
            params["overlap"] = overlap
        if window:
            params["window"] = window
        if method:
            params["method"] = method
        return params


class AsdDialog(_SpectrumDialog):
    """Collect ASD parameters without performing scientific normalization."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize the ASD parameter inputs."""
        super().__init__("Compute ASD", parent)
        self._add_optional_spectrum_fields()
        self.fftlength_edit.setPlaceholderText("Required")

    def _collect_params(self) -> dict[str, Any]:
        params = self._optional_spectrum_params()
        fftlength = params.get("fftlength")
        if fftlength is None:
            raise ValueError("FFT length is required")
        if fftlength <= 0:
            raise ValueError("FFT length must be positive")
        return params


class SpectrogramDialog(_SpectrumDialog):
    """Collect spectrogram parameters without performing normalization."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize the spectrogram parameter inputs."""
        super().__init__("Compute Spectrogram", parent)
        self.stride_edit = QLineEdit(self)
        self._form.addRow("Stride [s]", self.stride_edit)
        self._add_optional_spectrum_fields()

    def _collect_params(self) -> dict[str, Any]:
        stride = self._optional_finite(self.stride_edit.text(), label="Stride")
        if stride is None:
            raise ValueError("Stride is required")
        if stride <= 0:
            raise ValueError("Stride must be positive")
        return {"stride": stride, **self._optional_spectrum_params()}
