"""Resolve Studio-owned XDG directories without creating them."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_APPLICATION_DIRECTORY = "gwexpy-studio"
_XDG_DEFAULTS = {
    "XDG_CONFIG_HOME": (".config",),
    "XDG_DATA_HOME": (".local", "share"),
    "XDG_STATE_HOME": (".local", "state"),
    "XDG_CACHE_HOME": (".cache",),
}


@dataclass(frozen=True, slots=True)
class UserPaths:
    """Studio-owned roots derived from the current XDG environment."""

    config: Path
    data: Path
    state: Path
    cache: Path


def _xdg_base(variable: str) -> Path:
    """Return an absolute XDG base or the relevant default below the home directory."""
    configured = os.environ.get(variable)
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate
    return Path.home().joinpath(*_XDG_DEFAULTS[variable])


def resolve_user_paths() -> UserPaths:
    """Resolve all Studio XDG roots without creating application directories."""
    return UserPaths(
        config=_xdg_base("XDG_CONFIG_HOME") / _APPLICATION_DIRECTORY,
        data=_xdg_base("XDG_DATA_HOME") / _APPLICATION_DIRECTORY,
        state=_xdg_base("XDG_STATE_HOME") / _APPLICATION_DIRECTORY,
        cache=_xdg_base("XDG_CACHE_HOME") / _APPLICATION_DIRECTORY,
    )


def config_directory() -> Path:
    """Return Studio's configuration root without creating it."""
    return resolve_user_paths().config


def data_directory() -> Path:
    """Return Studio's data root without creating it."""
    return resolve_user_paths().data


def state_directory() -> Path:
    """Return Studio's state root without creating it."""
    return resolve_user_paths().state


def cache_directory() -> Path:
    """Return Studio's cache root without creating it."""
    return resolve_user_paths().cache


def bootstrap_matplotlib() -> Path:
    """Set Matplotlib's configuration directory without creating it."""
    directory = cache_directory() / "matplotlib"
    os.environ["MPLCONFIGDIR"] = os.fspath(directory)
    return directory
