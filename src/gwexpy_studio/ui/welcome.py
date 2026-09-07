"""The empty-workspace welcome panel for the trial desktop application."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_MAX_RECENT_PROJECTS = 10
_RECENT_AVAILABLE_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class WelcomePanel(QWidget):
    """Offer ordinary data/project actions before a workspace has data."""

    open_data_requested = Signal()
    open_project_requested = Signal()
    try_sample_requested = Signal()
    clear_recent_projects_requested = Signal()
    recent_project_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create the compact start screen and its explicit user actions."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        layout.setSpacing(12)
        layout.addStretch()

        heading = QLabel("GWexpy Studio", self)
        heading.setObjectName("welcome-heading")
        heading.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        heading.setStyleSheet("font-size: 24px; font-weight: 600")
        layout.addWidget(heading)
        subtitle = QLabel(
            "Open measurement data or continue a saved project.", self
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(subtitle)

        primary_actions = QHBoxLayout()
        self.open_data_button = QPushButton("Open Data", self)
        self.open_data_button.setObjectName("welcome-open-data")
        self.open_data_button.clicked.connect(self.open_data_requested)
        primary_actions.addWidget(self.open_data_button)
        self.open_project_button = QPushButton("Open Project", self)
        self.open_project_button.setObjectName("welcome-open-project")
        self.open_project_button.clicked.connect(self.open_project_requested)
        primary_actions.addWidget(self.open_project_button)
        layout.addLayout(primary_actions)

        self.try_sample_button = QPushButton("Try Sample", self)
        self.try_sample_button.setObjectName("welcome-try-sample")
        self.try_sample_button.clicked.connect(self.try_sample_requested)
        layout.addWidget(self.try_sample_button)

        self.recent_projects_label = QLabel("Recent Projects", self)
        self.recent_projects_label.setObjectName("welcome-recent-heading")
        layout.addWidget(self.recent_projects_label)
        self.recent_projects = QListWidget(self)
        self.recent_projects.setObjectName("welcome-recent-projects")
        self.recent_projects.setMaximumHeight(230)
        self.recent_projects.itemActivated.connect(self._request_recent_project)
        layout.addWidget(self.recent_projects)
        self.clear_recent_projects_button = QPushButton("Clear Recent Projects", self)
        self.clear_recent_projects_button.setObjectName("welcome-clear-recent-projects")
        self.clear_recent_projects_button.clicked.connect(
            self.clear_recent_projects_requested
        )
        layout.addWidget(self.clear_recent_projects_button)
        layout.addStretch()
        self._project_actions_available = True
        self.set_recent_projects(())

    def set_action_availability(
        self, *, data_available: bool, project_available: bool
    ) -> None:
        """Synchronize welcome controls with their matching File actions."""
        self._project_actions_available = project_available
        self.open_data_button.setEnabled(data_available)
        self.try_sample_button.setEnabled(data_available)
        self.open_project_button.setEnabled(project_available)
        self.clear_recent_projects_button.setEnabled(
            project_available and self.recent_projects.count() > 0
        )
        for index in range(self.recent_projects.count()):
            item = self.recent_projects.item(index)
            if item is not None:
                self._set_recent_item_enabled(
                    item,
                    project_available
                    and bool(item.data(_RECENT_AVAILABLE_ROLE)),
                )

    def set_recent_projects(self, paths: Iterable[Path]) -> None:
        """Render at most ten path-only recent records without reading projects."""
        self.recent_projects.clear()
        shown = 0
        for path in paths:
            if shown == _MAX_RECENT_PROJECTS:
                break
            self._add_recent_project(path)
            shown += 1
        has_recent = shown > 0
        self.recent_projects_label.setVisible(has_recent)
        self.recent_projects.setVisible(has_recent)
        self.clear_recent_projects_button.setEnabled(
            has_recent and self._project_actions_available
        )

    def _add_recent_project(self, path: Path) -> None:
        """Add one path-only record, disabling it if its file disappeared."""
        item = QListWidgetItem(str(path))
        item.setData(Qt.ItemDataRole.UserRole, str(path))
        file_available = self._project_file_is_available(path)
        item.setData(_RECENT_AVAILABLE_ROLE, file_available)
        if not file_available:
            item.setText(f"{path} (missing)")
            item.setToolTip("This project file is no longer available.")
        self._set_recent_item_enabled(
            item, file_available and self._project_actions_available
        )
        self.recent_projects.addItem(item)

    @staticmethod
    def _set_recent_item_enabled(item: QListWidgetItem, enabled: bool) -> None:
        """Toggle one list item while preserving all non-enable interaction flags."""
        flags = item.flags()
        if enabled:
            item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled)
        else:
            item.setFlags(flags & ~Qt.ItemFlag.ItemIsEnabled)

    @staticmethod
    def _project_file_is_available(path: Path) -> bool:
        """Check only file availability; project contents stay unopened at startup."""
        try:
            return path.is_file()
        except OSError:
            return False

    def _request_recent_project(self, item: QListWidgetItem) -> None:
        """Emit a stored path only for an enabled item selected by the user."""
        if not item.flags() & Qt.ItemFlag.ItemIsEnabled:
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(value, str):
            self.recent_project_requested.emit(value)
