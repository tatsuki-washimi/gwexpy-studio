"""Compatibility exports for Studio's headless-neutral XDG resolver."""

from __future__ import annotations

from ..user_paths import (
    UserPaths,
    bootstrap_matplotlib,
    cache_directory,
    config_directory,
    data_directory,
    resolve_user_paths,
    state_directory,
)

__all__ = [
    "UserPaths",
    "bootstrap_matplotlib",
    "cache_directory",
    "config_directory",
    "data_directory",
    "resolve_user_paths",
    "state_directory",
]
