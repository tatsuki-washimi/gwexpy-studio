"""Shared modeless parameter panel for non-destructive scientific operations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..application.preview import PreviewPayload
from .parameter_fields import (
    ARITHMETIC_IDS,
    BINARY_OPERATIONS,
    FILTER_OPERATIONS,
    OP_FIELDS,
    field_value,
    make_field,
)
from .plot_canvas import PlotCanvas


class ParameterPanel(QDialog):
    """Retain inputs and response previews until the user applies a recipe."""

    apply_requested = Signal(object)
    preview_requested = Signal(object)
    draft_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build object role selectors and the reusable typed parameter form."""
        super().__init__(parent)
        self.setWindowTitle("Signal operation")
        self.setModal(False)
        self.resize(640, 640)
        self.generation = 0
        self._busy = False
        self._preview_generation: int | None = None
        self._recorded_handle: dict[str, Any] | None = None
        self._saved: dict[str, dict[str, Any]] = {}
        self.fields: dict[str, QWidget] = {}
        self.operation = ""
        layout = QVBoxLayout(self)
        self.heading = QLabel(self)
        self.heading.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(self.heading)
        roles = QFormLayout()
        self.self_combo = QComboBox(self)
        self.other_combo = QComboBox(self)
        self.self_label = QLabel("Input A", self)
        self.other_label = QLabel("Output B / other", self)
        roles.addRow(self.self_label, self.self_combo)
        roles.addRow(self.other_label, self.other_combo)
        self.swap_button = QPushButton("Swap A ↔ B", self)
        self.swap_button.clicked.connect(self._swap_roles)
        roles.addRow("", self.swap_button)
        layout.addLayout(roles)
        self.form_widget = QWidget(self)
        self.form = QFormLayout(self.form_widget)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_widget)
        layout.addWidget(scroll)
        self.note = QLabel(
            "Operations create new data. Existing data stays available.", self
        )
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #bd3a30")
        layout.addWidget(self.error_label)
        self.response_member_combo = QComboBox(self)
        self.response_member_combo.currentIndexChanged.connect(self._select_response)
        layout.addWidget(self.response_member_combo)
        self.response_canvas = PlotCanvas(self)
        self.response_canvas.setMinimumHeight(460)
        self.response_canvas.hide()
        self.response_member_combo.hide()
        self._responses: tuple[PreviewPayload, ...] = ()
        layout.addWidget(self.response_canvas)
        buttons = QHBoxLayout()
        self.preview_button = QPushButton("Preview Response", self)
        self.apply_button = QPushButton("Apply", self)
        self.apply_button.setDefault(True)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.close)
        self.preview_button.clicked.connect(self._request_preview)
        self.apply_button.clicked.connect(self._request_apply)
        buttons.addWidget(self.preview_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        self.self_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.other_combo.currentIndexChanged.connect(self._invalidate_preview)
        self.set_operation("arithmetic")

    def set_objects(self, objects: Sequence[tuple[str, dict[str, Any]]]) -> None:
        """Provide parent/member handles without materializing graph nodes."""
        for combo in (self.self_combo, self.other_combo):
            previous = combo.currentData()
            combo.clear()
            for label, handle in objects:
                combo.addItem(label, handle)
            selected = combo.findData(previous)
            if selected >= 0:
                combo.setCurrentIndex(selected)

    def set_operation(self, operation: str) -> None:
        """Switch operation while retaining each form's previously entered values."""
        if operation in ARITHMETIC_IDS.values():
            operation = "arithmetic"
        if operation not in OP_FIELDS:
            raise ValueError(f"Unknown operation: {operation}")
        if self.operation:
            self._saved[self.operation] = self._raw_values()
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields = {}
        self.operation = operation
        for field in OP_FIELDS[operation]:
            widget = make_field(field, self.form_widget)
            self.fields[field[0]] = widget
            self.form.addRow(field[1], widget)
            self._restore_field(widget, self._saved.get(operation, {}).get(field[0]))
            for signal_name in ("textChanged", "currentTextChanged", "toggled"):
                signal = getattr(widget, signal_name, None)
                if signal is not None:
                    signal.connect(self._invalidate_preview)
        title = operation.removeprefix("timeseries.").replace("_", " ").title()
        self.heading.setText(title)
        self.setWindowTitle(title)
        self.error_label.clear()
        self._invalidate_preview()
        self._update_controls()

    def _raw_values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, widget in self.fields.items():
            if isinstance(widget, QCheckBox):
                result[name] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                result[name] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                result[name] = widget.text()
        return result

    @staticmethod
    def _restore_field(widget: QWidget, value: Any) -> None:
        if value is None:
            return
        if isinstance(widget, QCheckBox):
            widget.setChecked(value)
        elif isinstance(widget, QComboBox):
            widget.setCurrentText(value)
        elif isinstance(widget, QLineEdit):
            widget.setText(value)

    def _swap_roles(self) -> None:
        first, second = self.self_combo.currentIndex(), self.other_combo.currentIndex()
        self.self_combo.setCurrentIndex(second)
        self.other_combo.setCurrentIndex(first)

    def _invalidate_preview(self, *_args: Any) -> None:
        self.generation += 1
        self._recorded_handle = None
        self.response_canvas.hide()
        self.response_member_combo.hide()
        self._update_controls()
        self.draft_changed.emit()

    def draft_state(self) -> dict[str, Any]:
        """Return editable text and role choices without response approvals."""
        return {
            "operation": self.operation,
            "saved": {**self._saved, self.operation: self._raw_values()},
            "self": self.self_combo.currentData(),
            "other": self.other_combo.currentData(),
        }

    def restore_draft(self, state: dict[str, Any]) -> None:
        """Restore a draft while invalidating every former response request."""
        operation = state.get("operation", "arithmetic")
        self.operation = ""
        self._saved = dict(state.get("saved", {}))
        self.set_operation(operation)
        for role, combo in (("self", self.self_combo), ("other", self.other_combo)):
            index = combo.findData(state.get(role))
            if index >= 0:
                combo.setCurrentIndex(index)
        self._recorded_handle = None
        self._preview_generation = None

    def _update_controls(self) -> None:
        scalar = (
            self.operation == "arithmetic"
            and cast(QComboBox, self.fields["operand_mode"]).currentText() == "scalar"
        )
        binary = self.operation in BINARY_OPERATIONS and not scalar
        self.other_combo.setVisible(binary)
        self.other_label.setVisible(binary)
        self.swap_button.setVisible(binary)
        for name in (
            ("scalar", "unit", "reverse") if self.operation == "arithmetic" else ()
        ):
            self.fields[name].setEnabled(scalar)
        self.preview_button.setVisible(self.operation in FILTER_OPERATIONS)
        self.preview_button.setEnabled(not self._busy)
        self.apply_button.setEnabled(not self._busy)
        if self.operation in FILTER_OPERATIONS:
            self.note.setText(
                "Steady response; edge transients are excluded. "
                "Preview does not add History."
            )
        else:
            self.note.setText(
                "Operations create new data. "
                "Batch members pair by keys, indexes, or labels."
            )

    def request(self) -> dict[str, Any]:
        """Return an operation request with explicit object roles and typed params."""
        first = self.self_combo.currentData()
        if first is None:
            raise ValueError("Select input A")
        params = {}
        scalar_mode = (
            self.operation == "arithmetic"
            and cast(QComboBox, self.fields["operand_mode"]).currentText() == "scalar"
        )
        for field in OP_FIELDS[self.operation]:
            if (
                self.operation == "arithmetic"
                and not scalar_mode
                and field[0] in {"scalar", "unit", "reverse"}
            ):
                continue
            value = field_value(self.fields[field[0]], field)
            if value is not None:
                params[field[0]] = value
        inputs = {"self": first}
        if self.operation in BINARY_OPERATIONS and not scalar_mode:
            second = self.other_combo.currentData()
            if second is None:
                raise ValueError("Select output B / other")
            inputs["other"] = second
        op_name = self.operation
        if op_name == "arithmetic":
            op_name = ARITHMETIC_IDS[params.pop("operator")]
            if scalar_mode:
                if "scalar" not in params:
                    raise ValueError("Enter a finite constant")
                params["scalar"] = {
                    "value": params["scalar"],
                    "unit": params.pop("unit", ""),
                }
        return {"op_name": op_name, "inputs": inputs, "params": params}

    def _emit_request(self, *, preview: bool) -> None:
        if self._busy:
            return
        try:
            request = self.request()
        except (ValueError, TypeError) as exc:
            self.show_error("invalid_params", str(exc))
            return
        self.error_label.clear()
        self.set_busy(True)
        if preview:
            self._preview_generation = self.generation
            request["generation"] = self.generation
            if self._recorded_handle is not None:
                request.update(self._recorded_handle)
            self.preview_requested.emit(request)
        else:
            self.apply_requested.emit(request)

    def _request_preview(self) -> None:
        self._emit_request(preview=True)

    def preview_recorded(
        self, object_id: str, selector: dict[str, Any] | None = None
    ) -> None:
        """Reopen an applied filter response from its recorded native coefficients."""
        self._recorded_handle = {"recorded_object_id": object_id}
        if selector is not None:
            self._recorded_handle["recorded_selector"] = selector
        self._emit_request(preview=True)

    def _request_apply(self) -> None:
        self._emit_request(preview=False)

    def set_busy(self, busy: bool) -> None:
        """Guard panel dispatch while allowing values to remain editable."""
        self._busy = busy
        self._update_controls()

    def show_error(self, code: str, message: str) -> None:
        """Keep every input value available for correcting a failed request."""
        self.error_label.setText(f"[{code}] {message}")
        self.set_busy(False)

    def show_responses(
        self, responses: Sequence[PreviewPayload], generation: int
    ) -> bool:
        """Display only responses belonging to the current unchanged form."""
        self.set_busy(False)
        if generation != self.generation or generation != self._preview_generation:
            return False
        self._responses = tuple(responses)
        self.response_member_combo.clear()
        for response in self._responses:
            self.response_member_combo.addItem(
                response.preview.ref.name or response.preview.ref.object_id
            )
        self.response_member_combo.setVisible(len(self._responses) > 1)
        self._select_response(0)
        return True

    def _select_response(self, index: int) -> None:
        if 0 <= index < len(self._responses):
            payload = self._responses[index]
            self.response_canvas.set_preview(payload.preview, payload.spec)
            self.response_canvas.show()
