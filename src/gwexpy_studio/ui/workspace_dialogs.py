"""Keep background UI checkpoints out of user confirmation transactions."""

from __future__ import annotations

from typing import Any


def workspace_dialog(
    window: Any,
    execute: Any,
    *args: Any,
    dialog_instance: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Guard a dialog and keep optional instance metadata out of ``execute``."""
    del dialog_instance
    previous = window._modal_active
    window._modal_active = True
    window._update_command_state()
    try:
        return execute(*args, **kwargs)
    finally:
        window._modal_active = previous
        window._update_command_state()
