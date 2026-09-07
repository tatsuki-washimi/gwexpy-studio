"""Native data I/O configuration with editable formats and explicit confirmation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .parameter_fields import finite_json

DATA_CLASSES = tuple(
    f"{family}{suffix}"
    for family in ("TimeSeries", "FrequencySeries", "Spectrogram")
    for suffix in ("", "Dict", "List", "Matrix")
)
_SAFE_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_SAFE_CAVEAT = re.compile(r"[A-Za-z][A-Za-z0-9 .,_()\-]{0,159}")
_UNAVAILABLE_REASONS = {
    "backend_not_bundled": (
        "backend not bundled",
        "Backend not included in this packaged build.",
    ),
    "fixture_unavailable": (
        "fixture unavailable",
        "This format has not been verified in this packaged build.",
    ),
    "native_unimplemented": (
        "native operation unavailable",
        "This operation is unavailable in this packaged build.",
    ),
    "native_error": (
        "native error during verification",
        "This format is unavailable in this packaged build.",
    ),
    "frozen_unverified": (
        "not verified",
        "This format is not verified in this packaged build.",
    ),
    "not_applicable": (
        "not applicable",
        "This format is not applicable to this operation.",
    ),
    "runtime_dependency_missing": (
        "runtime dependency missing",
        "A required runtime dependency is unavailable in this environment.",
    ),
    "probe_failed": (
        "runtime probe failed",
        "This format could not be verified in this environment.",
    ),
    "registry_missing": (
        "registry entry missing",
        "This reviewed format is no longer registered by the runtime.",
    ),
    "unreviewed_registry_entry": (
        "unreviewed registry entry",
        "This runtime format is not reviewed for this packaged build.",
    ),
}


@dataclass(frozen=True, slots=True)
class _FormatCapability:
    """One presentation-safe policy result for the panel's current direction."""

    available: bool
    tier: str | None
    caveat: str | None
    reason: str


class DataIOPanel(QDialog):
    """Configure a read/write request without importing scientific packages."""

    catalog_requested = Signal(object)
    inspect_requested = Signal(object)
    read_requested = Signal(object)
    write_requested = Signal(object)
    draft_changed = Signal()

    def __init__(
        self, *, direction: str = "read", parent: QWidget | None = None
    ) -> None:
        """Build a reusable form that survives missing dependencies and retry."""
        super().__init__(parent)
        if direction not in {"read", "write"}:
            raise ValueError("direction must be read or write")
        self.direction = direction
        self._inspection: dict[str, Any] | None = None
        self._submitted_request: dict[str, Any] | None = None
        self._busy = False
        self._capability_active = False
        self._auto_available = True
        self._format_capabilities: dict[str, _FormatCapability] = {}
        self.setModal(False)
        self.setWindowTitle("Open Data" if direction == "read" else "Export Data")
        self.resize(650, 660)
        layout = QVBoxLayout(self)
        heading = QLabel(self.windowTitle(), self)
        heading.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(heading)
        form = QFormLayout()
        self.datatype_combo = QComboBox(self)
        self.datatype_combo.addItems(DATA_CLASSES)
        form.addRow("Data type", self.datatype_combo)
        self.format_combo = QComboBox(self)
        self.format_combo.setEditable(True)
        self.format_combo.addItem("Auto", None)
        self.format_combo.setToolTip(
            "Choose a registered candidate, Auto, or enter a format name."
        )
        form.addRow("Format", self.format_combo)
        self.capability_label = QLabel(self)
        self.capability_label.setWordWrap(True)
        self.capability_label.setStyleSheet("color: #425466")
        self.capability_label.hide()
        form.addRow("", self.capability_label)
        self.paths_edit = QPlainTextEdit(self)
        self.paths_edit.setPlaceholderText("One file or directory path per line")
        self.paths_edit.setMaximumHeight(100)
        form.addRow("Paths", self.paths_edit)
        browse = QHBoxLayout()
        self.files_button = QPushButton("Choose files…", self)
        self.directory_button = QPushButton("Choose directory…", self)
        self.files_button.clicked.connect(self._choose_files)
        self.directory_button.clicked.connect(self._choose_directory)
        browse.addWidget(self.files_button)
        browse.addWidget(self.directory_button)
        form.addRow("", browse)
        self.combine_combo = QComboBox(self)
        self.combine_combo.addItems(["individual", "combined"])
        form.addRow("Multiple files", self.combine_combo)
        self.args_edit = QPlainTextEdit("[]", self)
        self.kwargs_edit = QPlainTextEdit("{}", self)
        for editor in (self.args_edit, self.kwargs_edit):
            editor.setMaximumHeight(90)
            editor.setTabChangesFocus(True)
        form.addRow("Positional args (JSON array)", self.args_edit)
        form.addRow("Keyword args (JSON object)", self.kwargs_edit)
        self.max_bytes_spin = QDoubleSpinBox(self)
        self.max_bytes_spin.setDecimals(0)
        self.max_bytes_spin.setRange(1, 2**53)
        self.max_bytes_spin.setValue(512 * 1024 * 1024)
        self.max_entries_spin = QSpinBox(self)
        self.max_entries_spin.setRange(1, 2_147_483_647)
        self.max_entries_spin.setValue(10_000)
        form.addRow("Maximum bytes", self.max_bytes_spin)
        form.addRow("Maximum entries", self.max_entries_spin)
        layout.addLayout(form)
        help_text = QLabel(
            'Typed JSON: {"__type__":"quantity","value":2,"unit":"m"}; '
            '{"__type__":"complex","real":1,"imag":2}; '
            '{"__type__":"tuple","items":[1,2]}. No Python expressions.',
            self,
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #bd3a30")
        layout.addWidget(self.error_label)
        self.confirmation = QPlainTextEdit(self)
        self.confirmation.setReadOnly(True)
        self.confirmation.setPlaceholderText(
            "Inspect to review resolved paths, format, arguments, and limits."
        )
        self.confirmation.setMaximumHeight(140)
        layout.addWidget(self.confirmation)
        buttons = QHBoxLayout()
        self.inspect_button = QPushButton("Inspect / Review", self)
        self.confirm_button = QPushButton(
            "Read Data" if direction == "read" else "Write Data", self
        )
        self.confirm_button.setEnabled(False)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.close)
        self.inspect_button.clicked.connect(self._inspect)
        self.confirm_button.clicked.connect(self._confirm)
        buttons.addWidget(self.inspect_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)
        self.datatype_combo.currentTextChanged.connect(self._datatype_changed)
        self.format_combo.currentTextChanged.connect(self._format_changed)
        self.combine_combo.currentTextChanged.connect(self._invalidate)
        for editor in (self.paths_edit, self.args_edit, self.kwargs_edit):
            editor.textChanged.connect(self._invalidate)
        self.max_bytes_spin.valueChanged.connect(self._invalidate)
        self.max_entries_spin.valueChanged.connect(self._invalidate)

    def request(self) -> dict[str, Any]:
        """Read typed JSON primitives and explicit resource limits from controls."""
        paths = [
            line.strip()
            for line in self.paths_edit.toPlainText().splitlines()
            if line.strip()
        ]
        if not paths:
            raise ValueError("Choose at least one path")
        args = finite_json(self.args_edit.toPlainText())
        kwargs = finite_json(self.kwargs_edit.toPlainText())
        if not isinstance(args, list):
            raise ValueError("Positional args must be a JSON array")
        if not isinstance(kwargs, dict):
            raise ValueError("Keyword args must be a JSON object")
        format_name = self._selected_format_name()
        return {
            "datatype": self.datatype_combo.currentText(),
            "format": format_name,
            "paths": paths,
            "args": args,
            "kwargs": kwargs,
            "combine": self.combine_combo.currentText(),
            "max_bytes": int(self.max_bytes_spin.value()),
            "max_entries": self.max_entries_spin.value(),
        }

    def _choose_files(self) -> None:
        if self.direction == "read":
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Choose data files", "", "All files (*)"
            )
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, "Export data", "", "All files (*)"
            )
            paths = [path] if path else []
        if paths:
            self.paths_edit.setPlainText("\n".join(paths))

    def _choose_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose data directory")
        if path:
            self.paths_edit.setPlainText(path)

    def _datatype_changed(self, *_args: Any) -> None:
        self._invalidate()
        self.catalog_requested.emit(
            {"datatype": self.datatype_combo.currentText(), "direction": self.direction}
        )

    def set_catalog(self, catalog: dict[str, Any]) -> None:
        """Update native candidates and artifact-only capability presentation."""
        if catalog.get("datatype") != self.datatype_combo.currentText():
            return
        previous = self._selected_format_name()
        self.format_combo.clear()
        self._capability_active = self._active_capability_catalog(catalog)
        self._format_capabilities = {}
        if self._capability_active:
            self._set_frozen_catalog(catalog)
        else:
            self._set_development_catalog(catalog)
        self.format_combo.setEditText(previous or "Auto")
        self._refresh_capability_label()
        self.set_busy(False)

    def set_unavailable_catalog(self) -> None:
        """Fail closed when a worker catalog cannot match its bootstrap facts."""
        self.set_catalog(
            {
                "datatype": self.datatype_combo.currentText(),
                "direction": self.direction,
                "formats": [],
                "capability_mode": "invalid",
                "capability_digest": None,
                "capability_effective_digest": None,
            }
        )

    @staticmethod
    def _active_capability_catalog(catalog: dict[str, Any]) -> bool:
        """Treat malformed supplied artifact annotations conservatively as active."""
        return (
            "capability_mode" in catalog
            and catalog.get("capability_mode") != "developer"
        )

    def _set_development_catalog(self, catalog: dict[str, Any]) -> None:
        """Retain the original native-registry form in ordinary source use."""
        self._auto_available = True
        self.format_combo.addItem("Auto", None)
        self.format_combo.setToolTip(
            "Choose a registered candidate, Auto, or enter a format name."
        )
        for entry in catalog.get("formats", []):
            if entry.get(self.direction):
                self.format_combo.addItem(entry["format"], entry["format"])

    def _set_frozen_catalog(self, catalog: dict[str, Any]) -> None:
        """List only policy-aware candidates without trusting presentation fields."""
        candidates: list[tuple[str, _FormatCapability, bool]] = []
        for entry in catalog.get("formats", []):
            if not isinstance(entry, dict):
                continue
            declared = entry.get("capabilities")
            if not entry.get(self.direction) and not (
                isinstance(declared, dict) and self.direction in declared
            ):
                continue
            format_name = entry.get("format")
            if (
                not isinstance(format_name, str)
                or _SAFE_FORMAT.fullmatch(format_name) is None
            ):
                continue
            capability = self._catalog_capability(entry)
            self._format_capabilities[format_name] = capability
            candidates.append(
                (format_name, capability, bool(entry.get("auto_identify")))
            )
        self._auto_available = self.direction == "read" and any(
            capability.available and auto_identify
            for _name, capability, auto_identify in candidates
        )
        self._add_auto_item()
        self.format_combo.setToolTip(
            "Choose a verified packaged format. Unavailable formats are listed "
            "for reference."
        )
        for format_name, capability, _auto_identify in candidates:
            self._add_capability_item(format_name, capability)

    def _catalog_capability(self, entry: dict[str, Any]) -> _FormatCapability:
        """Reduce untrusted worker metadata to a safe, fail-closed UI record."""
        all_capabilities = entry.get("capabilities")
        if not isinstance(all_capabilities, dict):
            return _FormatCapability(False, None, None, "frozen_unverified")
        raw = all_capabilities.get(self.direction)
        if not isinstance(raw, dict):
            return _FormatCapability(False, None, None, "frozen_unverified")
        tier = raw.get("tier")
        if tier not in {"A", "B", "C"}:
            return _FormatCapability(False, None, None, "frozen_unverified")
        available = raw.get("available") is True and tier in {"A", "B"}
        caveat = self._safe_caveat(raw.get("caveat")) if tier == "B" else None
        if tier == "B" and caveat is None:
            caveat = "Format-specific metadata limitations may apply."
        reason = raw.get("reason")
        safe_reason = reason if reason in _UNAVAILABLE_REASONS else "frozen_unverified"
        return _FormatCapability(available, tier, caveat, safe_reason)

    @staticmethod
    def _safe_caveat(value: object) -> str | None:
        """Allow only bounded note text that cannot expose a filesystem path."""
        if isinstance(value, str) and _SAFE_CAVEAT.fullmatch(value) is not None:
            return value
        return None

    def _add_auto_item(self) -> None:
        """Make automatic resolution explicit because the worker decides its format."""
        if self._auto_available:
            self.format_combo.addItem("Auto", None)
            return
        self.format_combo.addItem("Auto — Unavailable (choose a format)", None)
        self._set_item_enabled(0, False)

    def _add_capability_item(
        self, format_name: str, capability: _FormatCapability
    ) -> None:
        """Add a canonical candidate while keeping unavailable directions visible."""
        if capability.available:
            index = self.format_combo.count()
            self.format_combo.addItem(format_name, format_name)
            detail = "Supported in this packaged build."
            if capability.tier == "B" and capability.caveat is not None:
                detail = f"Supported with caveat: {capability.caveat}"
            self.format_combo.setItemData(index, detail, Qt.ItemDataRole.ToolTipRole)
            return
        short_reason, detail = _UNAVAILABLE_REASONS[capability.reason]
        index = self.format_combo.count()
        self.format_combo.addItem(
            f"{format_name} — Unavailable ({short_reason})", format_name
        )
        self.format_combo.setItemData(index, detail, Qt.ItemDataRole.ToolTipRole)
        self._set_item_enabled(index, False)

    def _set_item_enabled(self, index: int, enabled: bool) -> None:
        """Disable only an individual default-combobox item, not the field itself."""
        model = self.format_combo.model()
        if isinstance(model, QStandardItemModel):
            item = model.item(index)
            if item is not None:
                item.setEnabled(enabled)

    def _selected_format_name(self) -> str | None:
        """Return item data rather than decorated display text when selected."""
        current = self.format_combo.currentText().strip()
        index = self.format_combo.currentIndex()
        if index >= 0 and current == self.format_combo.itemText(index):
            stored = self.format_combo.itemData(index, Qt.ItemDataRole.UserRole)
            if isinstance(stored, str):
                return stored
            if stored is None:
                return None
        return None if current.lower() in {"", "auto"} else current

    def _format_changed(self, *_args: Any) -> None:
        """Refresh policy context before invalidating a prior inspection."""
        self._refresh_capability_label()
        self._invalidate()

    def _refresh_capability_label(self) -> None:
        """Expose concise status without echoing arbitrary catalog-provided text."""
        if not self._capability_active:
            self.capability_label.clear()
            self.capability_label.hide()
            return
        format_name = self._selected_format_name()
        if format_name is None:
            if self._auto_available:
                self.capability_label.setText(
                    "Packaged I/O policy is active. Auto is identified by the "
                    "worker and allowed only for a bundled format."
                )
            elif self.direction == "write":
                self.capability_label.setText(
                    "Packaged I/O policy is active. Choose an explicit bundled "
                    "format for export."
                )
            else:
                self.capability_label.setText(
                    "Packaged I/O policy is active. No verified automatic format "
                    "is available."
                )
            self.capability_label.show()
            return
        capability = self._format_capabilities.get(format_name)
        if capability is not None and capability.available:
            if capability.tier == "B" and capability.caveat is not None:
                self.capability_label.setText(
                    f"Tier B — Supported with caveat: {capability.caveat}"
                )
            else:
                self.capability_label.setText(
                    "Tier A — Supported in this packaged build."
                )
        else:
            _short_reason, detail = _UNAVAILABLE_REASONS[
                capability.reason if capability is not None else "frozen_unverified"
            ]
            self.capability_label.setText(f"Unavailable: {detail}")
        self.capability_label.show()

    def _active_format_is_available(self, format_name: str | None) -> bool:
        """Reject local attempts to dispatch an active Tier C or unknown direction."""
        if not self._capability_active:
            return True
        if format_name is None:
            if self._auto_available:
                return True
            message = (
                "Choose an explicit bundled format for export."
                if self.direction == "write"
                else "No verified automatic format is available in this packaged build."
            )
            self.show_error("io_capability_unavailable", message)
            return False
        capability = self._format_capabilities.get(format_name)
        if capability is not None and capability.available:
            return True
        _short_reason, message = _UNAVAILABLE_REASONS[
            capability.reason if capability is not None else "frozen_unverified"
        ]
        self.show_error("io_capability_unavailable", message)
        return False

    def _invalidate(self, *_args: Any) -> None:
        self._inspection = None
        self.confirm_button.setEnabled(False)
        self.confirmation.clear()
        self.draft_changed.emit()

    def draft_state(self) -> dict[str, Any]:
        """Capture raw editable fields without inspection or overwrite tokens."""
        return {
            "datatype": self.datatype_combo.currentText(),
            "format": self.format_combo.currentText(),
            "paths": self.paths_edit.toPlainText(),
            "args": self.args_edit.toPlainText(),
            "kwargs": self.kwargs_edit.toPlainText(),
            "combine": self.combine_combo.currentText(),
            "max_bytes": self.max_bytes_spin.value(),
            "max_entries": self.max_entries_spin.value(),
        }

    def restore_draft(self, state: dict[str, Any]) -> None:
        """Restore fields and require fresh review before any data read/write."""
        from PySide6.QtCore import QSignalBlocker

        blockers = [QSignalBlocker(self), QSignalBlocker(self.datatype_combo)]
        self.datatype_combo.setCurrentText(state.get("datatype", "TimeSeries"))
        self.format_combo.setEditText(state.get("format", "Auto"))
        for key, editor in (
            ("paths", self.paths_edit),
            ("args", self.args_edit),
            ("kwargs", self.kwargs_edit),
        ):
            editor.setPlainText(state.get(key, ""))
        self.combine_combo.setCurrentText(state.get("combine", "individual"))
        self.max_bytes_spin.setValue(state.get("max_bytes", 512 * 1024 * 1024))
        self.max_entries_spin.setValue(state.get("max_entries", 10_000))
        self._submitted_request = None
        self._invalidate()
        del blockers

    def _inspect(self) -> None:
        if self._busy:
            return
        try:
            request = self.request()
        except (ValueError, TypeError) as exc:
            self.show_error("invalid_params", str(exc))
            return
        if not self._active_format_is_available(request["format"]):
            return
        self.error_label.clear()
        self._submitted_request = request
        if self.direction == "write":
            self.show_inspection({"request": request})
            return
        self.set_busy(True)
        self.inspect_requested.emit({"request": request})

    def show_inspection(self, inspection: dict[str, Any]) -> None:
        """Present arguments and expanded resource estimates before confirmation."""
        self.set_busy(False)
        try:
            current_request = self.request()
        except (ValueError, TypeError) as exc:
            self.show_error("stale_inspection", f"Configuration changed: {exc}")
            return
        if (self._submitted_request or inspection.get("request")) != current_request:
            self.show_error("stale_inspection", "Configuration changed; inspect again.")
            return
        self._inspection = inspection
        self.confirmation.setPlainText(
            json.dumps(inspection, indent=2, ensure_ascii=False)
        )
        self.confirm_button.setEnabled(True)

    def _confirm(self) -> None:
        if self._busy or self._inspection is None:
            return
        self.set_busy(True)
        if self.direction == "read":
            self.read_requested.emit({"inspection": self._inspection})
        else:
            self.write_requested.emit(self._inspection["request"])

    def set_busy(self, busy: bool) -> None:
        """Disable duplicate dispatch while preserving configuration editing."""
        self._busy = busy
        self.datatype_combo.setEnabled(not busy)
        self.inspect_button.setEnabled(not busy)
        self.confirm_button.setEnabled(not busy and self._inspection is not None)

    def show_error(self, code: str, message: str) -> None:
        """Leave configured data type, paths, and arguments available for retry."""
        self.error_label.setText(f"[{code}] {message}")
        self.set_busy(False)
