"""Document-aware public controller for the desktop workspace."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .alpha import AlphaController
from .workspace_actions import WorkspaceActions
from .workspace_restore import WorkspaceRestore


class WorkspaceController(WorkspaceActions, WorkspaceRestore):
    """Own the project, its worker, user gestures and recovery journal."""

    def __init__(
        self, *, recovery_root: Path | None = None, recovery: bool = True, **kwargs: Any
    ) -> None:
        """Initialize document ownership without starting a scientific worker."""
        AlphaController.__init__(self, **kwargs)
        self._group_depth = 0
        self._cancel_event = threading.Event()
        self._cancel_client = None
        self._init_document(recovery_root, recovery)
