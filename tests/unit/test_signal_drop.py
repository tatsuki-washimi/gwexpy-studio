"""Offscreen contracts for safe file drag-and-drop into the generic I/O form."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from gwexpy_studio.ui.signal_tools import SignalTools
from gwexpy_studio.ui.window import MainWindow


@pytest.fixture
def qapp() -> QApplication:
    """Create the offscreen Qt application required by the real Open Data form."""
    return QApplication.instance() or QApplication([])


@pytest.mark.gui
@pytest.mark.contract("TRT-IO-006")
def test_open_data_preserves_trailing_space_in_filename(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Inspection must target the exact dropped file, not its trimmed neighbor."""
    del qapp
    from gwexpy_studio.ui.io_panel import DataIOPanel

    source = tmp_path / "measurement.gwf "
    neighbor = tmp_path / "measurement.gwf"
    source.write_bytes(b"source")
    neighbor.write_bytes(b"neighbor")
    panel = DataIOPanel(direction="read")
    try:
        panel.paths_edit.setPlainText(str(source))
        assert panel.request()["paths"] == [str(source)]
    finally:
        panel.close()


@pytest.mark.gui
@pytest.mark.contract("TRT-IO-006")
def test_file_drop_prefills_open_data_without_inspecting_or_reading(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Dropping a file opens the form and retains Inspect/Read confirmation."""
    del qapp
    source = tmp_path / "explicit-input.gwf"
    source.write_bytes(b"fixture placeholder")
    event_mime = QMimeData()
    event_mime.setUrls([QUrl.fromLocalFile(str(source))])

    class DropHarness(QWidget):
        _drop_path = MainWindow._drop_path
        _validate_drop_event = MainWindow._validate_drop_event
        show_open_data = SignalTools.show_open_data

        def __init__(self) -> None:
            super().__init__()
            self.open_data_panel = None
            self.catalog_requests: list[dict[str, object]] = []
            self.legacy_opened: list[str] = []

        def _reject_for_bridge_state(self) -> bool:
            return False

        def _panel_dispatch(
            self, _panel: object, action: str, payload: dict[str, object]
        ) -> None:
            if action == "catalog_io":
                self.catalog_requests.append(payload)

        def _queue_ui_checkpoint(self) -> None:
            return None

        def open_file(self, uri: str) -> None:
            self.legacy_opened.append(uri)

        def _show_error(self, _code: str, _message: str) -> None:
            raise AssertionError("a valid local file drop must not show an error")

    window = DropHarness()
    event = QDropEvent(
        QPointF(1.0, 1.0),
        Qt.DropAction.CopyAction,
        event_mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    try:
        MainWindow.dropEvent(window, event)

        panel = window.open_data_panel
        assert panel is not None
        assert panel.paths_edit.toPlainText() == str(source)
        assert panel.inspect_button.isEnabled()
        assert not panel.confirm_button.isEnabled()
        assert window.catalog_requests == [
            {"datatype": "TimeSeries", "direction": "read"}
        ]
        assert window.legacy_opened == []
        assert event.isAccepted()
    finally:
        if window.open_data_panel is not None:
            window.open_data_panel.close()
        window.close()


@pytest.mark.gui
@pytest.mark.contract("TRT-IO-006")
@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n"])
def test_file_drop_rejects_line_breaks_before_prefilling(
    qapp: QApplication, tmp_path: Path, line_break: str
) -> None:
    """A decoded local path cannot inject extra entries into the line-based form."""
    del qapp
    source = tmp_path / f"line{line_break}break.gwf"
    event_mime = QMimeData()
    event_mime.setUrls([QUrl.fromLocalFile(str(source))])

    class DropHarness:
        _drop_path = MainWindow._drop_path
        _validate_drop_event = MainWindow._validate_drop_event

        def __init__(self) -> None:
            self.open_data_calls = 0
            self.open_data_panel = None
            self.errors: list[tuple[str, str]] = []

        def _reject_for_bridge_state(self) -> bool:
            return False

        def show_open_data(self) -> None:
            self.open_data_calls += 1

        def _show_error(self, code: str, message: str) -> None:
            self.errors.append((code, message))

    window = DropHarness()
    event = QDropEvent(
        QPointF(1.0, 1.0),
        Qt.DropAction.CopyAction,
        event_mime,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    MainWindow.dropEvent(window, event)

    assert window.open_data_calls == 0
    assert window.errors == [
        ("invalid_drop", "Dropped path cannot contain line breaks")
    ]
    assert not event.isAccepted()
