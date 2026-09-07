"""UI shell and presentation components for GWexpy Studio.

The formal launcher must configure Matplotlib's XDG cache before importing
the window/plot stack, so public exports are intentionally loaded lazily.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BridgeCommand",
    "BridgeResult",
    "BridgeState",
    "MainWindow",
    "WorkerBridge",
    "create_app",
    "main",
]


def __getattr__(name: str) -> Any:
    """Load public UI objects only when a caller requests them."""
    if name in {"create_app", "main"}:
        from .app import create_app, main

        return {"create_app": create_app, "main": main}[name]
    if name in {"BridgeCommand", "BridgeResult", "BridgeState", "WorkerBridge"}:
        from .bridge import BridgeCommand, BridgeResult, BridgeState, WorkerBridge

        return {
            "BridgeCommand": BridgeCommand,
            "BridgeResult": BridgeResult,
            "BridgeState": BridgeState,
            "WorkerBridge": WorkerBridge,
        }[name]
    if name == "MainWindow":
        from .window import MainWindow

        return MainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
