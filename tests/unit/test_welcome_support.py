"""Unit contracts for trial welcome and support helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QUrl


@pytest.mark.contract("TRT-WLC-001")
def test_quick_start_is_a_short_complete_trial_path() -> None:
    """The in-app help names the five user-visible trial steps."""
    from gwexpy_studio.ui.support import quick_start_text

    text = quick_start_text()

    for phrase in (
        "Try Sample",
        "Crop",
        "ASD",
        "Save Project",
        "Open Project",
    ):
        assert phrase in text


@pytest.mark.contract("TRT-WLC-002")
def test_open_log_folder_uses_a_local_url_without_creating_logs(
    tmp_path: Path, monkeypatch
) -> None:
    """The explicit support action delegates safely without writing log state."""
    from gwexpy_studio.runtime.logging import log_directory
    from gwexpy_studio.ui.support import open_log_folder

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    target = log_directory()
    opened: list[QUrl] = []

    assert open_log_folder(opener=lambda url: opened.append(url) or True)
    assert opened == [QUrl.fromLocalFile(str(target))]
    assert not target.exists()


@pytest.mark.contract("TRT-WLC-003")
def test_open_log_folder_contains_desktop_service_failures() -> None:
    """A desktop-service failure remains an ordinary, non-crashing UI outcome."""
    from gwexpy_studio.ui.support import open_log_folder

    def unavailable(_url: QUrl) -> bool:
        raise RuntimeError("desktop integration unavailable")

    assert not open_log_folder(opener=unavailable)
