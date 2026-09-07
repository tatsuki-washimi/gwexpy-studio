"""Welcome and support behaviors kept separate from the main window shell."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtGui import QAction

from ..runtime.trial import RecentProjectStore, sample_path
from ..worker.protocol import PROTOCOL_VERSION
from .support import AboutDialog, DiagnosticsFacts, QuickStartDialog


class TrialSupportTools:
    """Coordinate trial-only welcome, sample, recent, and support interactions."""

    def _init_trial_support(self: Any) -> None:
        """Initialize state before the main window creates visible controls."""
        self._recent_projects = RecentProjectStore()
        self.quick_start_dialog: QuickStartDialog | None = None
        self.about_dialog: AboutDialog | None = None
        self._last_error_code: str | None = None

    def _init_trial_support_actions(self: Any) -> None:
        """Wire welcome and Help controls to their ordinary window actions."""
        self.welcome_panel.open_data_requested.connect(self.open_action.trigger)
        self.welcome_panel.open_project_requested.connect(
            self.open_project_action.trigger
        )
        self.welcome_panel.try_sample_requested.connect(self._try_sample)
        self.welcome_panel.clear_recent_projects_requested.connect(
            self._clear_recent_projects
        )
        self.welcome_panel.recent_project_requested.connect(
            self._open_recent_project
        )
        help_menu = self.menuBar().addMenu("&Help")
        self.quick_start_action = QAction("Quick Start", self)
        self.quick_start_action.triggered.connect(self._show_quick_start)
        help_menu.addAction(self.quick_start_action)
        self.about_action = QAction("About GWexpy Studio", self)
        self.about_action.triggered.connect(self._show_about)
        help_menu.addAction(self.about_action)
        self._refresh_recent_projects()

    def _update_trial_support_action_state(self: Any) -> None:
        """Mirror the ordinary File action availability on welcome controls."""
        if not hasattr(self, "welcome_panel") or not hasattr(
            self, "open_project_action"
        ):
            return
        self.welcome_panel.set_action_availability(
            data_available=self.open_action.isEnabled(),
            project_available=self.open_project_action.isEnabled(),
        )

    def _try_sample(self: Any) -> None:
        """Prepopulate ordinary Open Data review with the bundled sample."""
        if not self.open_action.isEnabled():
            return
        try:
            path = sample_path()
        except (OSError, RuntimeError, ValueError):
            self._show_error("sample_unavailable", "Could not prepare the sample data")
            return
        self.open_action.trigger()
        if self.open_data_panel is None:
            return
        self.open_data_panel.paths_edit.setPlainText(str(path))
        # The bundled CSV has its physical column labels in the first row.
        # Keep that format-specific detail visible in the ordinary review UI
        # rather than relying on an implicit, special-purpose reader path.
        self.open_data_panel.kwargs_edit.setPlainText('{"skiprows": 1}')
        self._show_status("Sample selected — review settings before reading.")

    def _refresh_recent_projects(self: Any) -> None:
        """Refresh path-only recent items without opening a project document."""
        self.welcome_panel.set_recent_projects(self._recent_projects.list())

    def _clear_recent_projects(self: Any) -> None:
        """Clear Studio's small recent-path list while leaving projects untouched."""
        try:
            self._recent_projects.clear()
        except OSError:
            self._show_error(
                "recent_projects_unavailable", "Could not clear recent projects"
            )
            return
        self._refresh_recent_projects()

    def _open_recent_project(self: Any, path: str) -> None:
        """Open an available recent path through the usual document transition."""
        if not self.open_project_action.isEnabled():
            return
        try:
            candidate = Path(path).expanduser().resolve(strict=False)
            available = candidate.is_file()
        except (OSError, RuntimeError, ValueError):
            available = False
            candidate = None
        if not available or candidate is None:
            self._show_status("That recent project is no longer available.")
            self._refresh_recent_projects()
            return
        self._request_document_change("open_project", {"path": str(candidate)})

    def _record_current_project_as_recent(self: Any) -> None:
        """Record a successfully opened or saved project without reading its data."""
        path = self._workspace_status.get("project_path")
        if not isinstance(path, str):
            return
        try:
            self._recent_projects.record(path)
        except (OSError, RuntimeError, ValueError):
            self._show_status("Recent projects could not be updated.")
            return
        self._refresh_recent_projects()

    def _show_welcome_for_empty_document(self: Any) -> None:
        """Choose the welcome page only when the transitioned document is empty."""
        self.central_stack.setCurrentWidget(
            self.welcome_panel
            if not self._project.objects
            else self.central_placeholder
        )

    def _diagnostics_facts(self: Any) -> DiagnosticsFacts:
        """Return the fixed set of path-free facts allowed into About diagnostics."""
        snapshot = getattr(self, "_io_capability_snapshot", None)
        policy_digest = (
            snapshot.get("policy_digest") if isinstance(snapshot, dict) else None
        )
        return DiagnosticsFacts(
            project_schema_version=self._project.schema_version,
            worker_protocol_version=PROTOCOL_VERSION,
            io_capability_digest=policy_digest,
            io_capability_snapshot=snapshot,
            last_error_code=self._last_error_code,
        )

    def _show_quick_start(self: Any) -> None:
        """Show the short trial guide as an in-app, non-modal help dialog."""
        if self.quick_start_dialog is None:
            self.quick_start_dialog = QuickStartDialog(self)
        self.quick_start_dialog.show()
        self.quick_start_dialog.raise_()
        self.quick_start_dialog.activateWindow()

    def _show_about(self: Any) -> None:
        """Show fresh privacy-preserving diagnostics and the explicit log action."""
        if self.about_dialog is None:
            self.about_dialog = AboutDialog(
                self, diagnostics_provider=self._diagnostics_facts
            )
        self.about_dialog.refresh_diagnostics()
        self.about_dialog.show()
        self.about_dialog.raise_()
        self.about_dialog.activateWindow()
