"""Pure command-line parsing for the installed Studio launcher."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class LauncherUsageError(ValueError):
    """Raised when launcher arguments cannot name one Studio project."""


def parse_project_argument(arguments: Sequence[str]) -> Path | None:
    """Return one canonical project path without opening or reading it.

    The installed launcher deliberately has a small command-line surface for
    the first trial: no argument opens the Welcome/recovery flow and exactly
    one ``.gwxproj`` argument opens that saved project.
    """
    values = tuple(arguments)
    if not values:
        return None
    if len(values) != 1:
        raise LauncherUsageError("expected zero arguments or one .gwxproj path")
    path = Path(values[0]).expanduser()
    if path.suffix.lower() != ".gwxproj":
        raise LauncherUsageError("expected a .gwxproj project path")
    return path.resolve(strict=False)
