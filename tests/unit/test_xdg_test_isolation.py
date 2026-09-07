"""Regression tests for the source-test XDG isolation boundary."""

from __future__ import annotations

import os
from pathlib import Path


def test_headless_suite_isolates_all_studio_xdg_homes(tmp_path: Path) -> None:
    """No source regression test can write Studio state to the real profile."""
    expected = {
        "XDG_CONFIG_HOME": tmp_path / "user-config",
        "XDG_DATA_HOME": tmp_path / "user-data",
        "XDG_STATE_HOME": tmp_path / "user-state",
        "XDG_CACHE_HOME": tmp_path / "user-cache",
    }

    for name, directory in expected.items():
        assert Path(os.environ[name]) == directory
    assert Path(os.environ["MPLCONFIGDIR"]) == (
        expected["XDG_CACHE_HOME"] / "gwexpy-studio" / "matplotlib"
    )
