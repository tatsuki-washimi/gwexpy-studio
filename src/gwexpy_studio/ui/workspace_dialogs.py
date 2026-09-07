"""Keep background UI checkpoints out of user confirmation transactions."""

from __future__ import annotations

from typing import Any


def workspace_dialog(window: Any, execute: Any, *args: Any) -> Any:
    """Guard nested Qt dialogs without changing an outer modal reservation."""
    previous = window._modal_active
    window._modal_active = True
    window._update_command_state()
    try:
        return execute(*args)
    finally:
        window._modal_active = previous
        window._update_command_state()
