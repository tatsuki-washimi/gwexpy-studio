"""Small in-app help and privacy-preserving support dialogs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..runtime.diagnostics import format_diagnostics
from ..runtime.logging import log_directory


@dataclass(frozen=True, slots=True)
class DiagnosticsFacts:
    """The only application-supplied values permitted in diagnostics output."""

    project_schema_version: object = None
    worker_protocol_version: object = None
    io_capability_digest: object = None
    io_capability_snapshot: object = None
    last_error_code: object = None


DiagnosticsProvider = Callable[[], DiagnosticsFacts]


def quick_start_text() -> str:
    """Return the short, user-facing five-minute trial guide."""
    return (
        "1. Select Try Sample, or choose Open Data to select your own file.\n"
        "2. Review the data settings, then load the data.\n"
        "3. Select the loaded time series and choose Operations → Crop.\n"
        "4. Choose Operations → ASD to inspect its spectrum.\n"
        "5. Choose File → Save Project, then later use Open Project to continue."
    )


def open_log_folder(*, opener: Callable[[QUrl], bool] | None = None) -> bool:
    """Ask the desktop to open Studio's log folder without creating it."""
    destination = QUrl.fromLocalFile(str(log_directory()))
    try:
        return bool((opener or QDesktopServices.openUrl)(destination))
    except Exception:
        return False


class QuickStartDialog(QDialog):
    """Show the concise trial workflow without leaving the application."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build a read-only dialog with the five trial steps."""
        super().__init__(parent)
        self.setWindowTitle("Quick Start")
        self.setModal(False)
        layout = QVBoxLayout(self)
        heading = QLabel("GWexpy Studio Quick Start", self)
        heading.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(heading)
        self.instructions = QLabel(quick_start_text(), self)
        self.instructions.setWordWrap(True)
        layout.addWidget(self.instructions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        layout.addWidget(buttons)


class AboutDialog(QDialog):
    """Show safe diagnostics separately from the user-controlled log files."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        diagnostics_provider: DiagnosticsProvider | None = None,
        open_folder: Callable[[], bool] | None = None,
    ) -> None:
        """Build the About dialog using only the approved diagnostics formatter."""
        super().__init__(parent)
        self.setWindowTitle("About GWexpy Studio")
        self.setModal(False)
        self._diagnostics_provider = diagnostics_provider or DiagnosticsFacts
        self._open_folder = open_folder or open_log_folder

        layout = QVBoxLayout(self)
        heading = QLabel("GWexpy Studio", self)
        heading.setStyleSheet("font-size: 18px; font-weight: 600")
        layout.addWidget(heading)
        description = QLabel(
            "Copy Diagnostics contains version and environment facts only. "
            "It does not copy project paths or log contents.",
            self,
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        self.diagnostics_text = QPlainTextEdit(self)
        self.diagnostics_text.setObjectName("diagnostics-text")
        self.diagnostics_text.setReadOnly(True)
        layout.addWidget(self.diagnostics_text)
        self.feedback_label = QLabel(self)
        self.feedback_label.setWordWrap(True)
        layout.addWidget(self.feedback_label)

        self.copy_diagnostics_button = QPushButton("Copy Diagnostics", self)
        self.copy_diagnostics_button.clicked.connect(self.copy_diagnostics)
        layout.addWidget(self.copy_diagnostics_button)
        self.open_log_folder_button = QPushButton("Open Log Folder", self)
        self.open_log_folder_button.clicked.connect(self.request_open_log_folder)
        layout.addWidget(self.open_log_folder_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        buttons.accepted.connect(self.close)
        layout.addWidget(buttons)

    def _current_diagnostics(self) -> str:
        """Format fresh, strictly scoped facts without accepting arbitrary text."""
        try:
            facts = self._diagnostics_provider()
        except Exception:
            facts = DiagnosticsFacts()
        if not isinstance(facts, DiagnosticsFacts):
            facts = DiagnosticsFacts()
        return format_diagnostics(
            project_schema_version=facts.project_schema_version,
            worker_protocol_version=facts.worker_protocol_version,
            io_capability_digest=facts.io_capability_digest,
            io_capability_snapshot=facts.io_capability_snapshot,
            last_error_code=facts.last_error_code,
        )

    def refresh_diagnostics(self) -> str:
        """Regenerate the safe display text from the latest application facts."""
        text = self._current_diagnostics()
        self.diagnostics_text.setPlainText(text)
        return text

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API spelling
        """Populate diagnostics when the dialog becomes visible."""
        self.refresh_diagnostics()
        super().showEvent(event)

    def copy_diagnostics(self) -> None:
        """Copy exactly the sanitized display text, never an application log."""
        QApplication.clipboard().setText(self.refresh_diagnostics())
        self.feedback_label.setText("Diagnostics copied.")

    def request_open_log_folder(self) -> None:
        """Request a desktop folder view and report only its success state."""
        if self._open_folder():
            self.feedback_label.setText("Log folder request sent.")
        else:
            self.feedback_label.setText("Could not open the log folder.")
