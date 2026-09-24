"""Capture the four public installation-guide screenshots."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IMAGE_NAMES = (
    "01-welcome.png",
    "02-try-sample.png",
    "03-asd.png",
    "04-reopen-project.png",
)
IMAGE_SIZE = (1200, 800)


class CaptureError(RuntimeError):
    """Raised when a deterministic screenshot step cannot be completed."""


def _announce(label: str) -> None:
    """Make a long-running capture step observable in CI and terminals."""
    print(f"[capture] {label}", flush=True)


def _wait(app: Any, predicate: Callable[[], bool], label: str) -> None:
    """Process Qt events until a state predicate becomes true."""
    from PySide6.QtCore import QEventLoop, QTimer

    if predicate():
        return
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    deadline = QTimer()
    deadline.setSingleShot(True)
    expired = [False]

    def check() -> None:
        if predicate():
            loop.quit()

    def expire() -> None:
        expired[0] = True
        loop.quit()

    poll.timeout.connect(check)
    deadline.timeout.connect(expire)
    poll.start()
    deadline.start(30_000)
    loop.exec()
    poll.stop()
    deadline.stop()
    app.processEvents()
    if expired[0] or not predicate():
        raise CaptureError(f"timed out during {label}")


def _wait_for_sample_catalog(app: Any, window: Any, panel: Any) -> None:
    """Wait for the sample catalog to enable its inspection button."""
    from gwexpy_studio.ui.bridge import BridgeState

    _wait(
        app,
        lambda: (
            window.bridge.state is BridgeState.IDLE
            and panel.inspect_button.isEnabled()
        ),
        "sample catalog",
    )


def _accept_dialog(
    app: Any,
    dialog_type: type[Any],
    fill: Callable[[Any], None],
    action: Any,
) -> None:
    """Accept one operation dialog through its public OK button."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialogButtonBox

    poll = QTimer()
    poll.setInterval(10)
    deadline = QTimer()
    deadline.setSingleShot(True)
    handled = [False]
    failure: list[BaseException] = []

    def current_dialog() -> Any | None:
        active = app.activeModalWidget()
        if isinstance(active, dialog_type):
            return active
        visible = [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, dialog_type) and widget.isVisible()
        ]
        return visible[0] if len(visible) == 1 else None

    def stop() -> None:
        poll.stop()
        deadline.stop()

    def accept() -> None:
        if handled[0] or failure:
            return
        dialog = current_dialog()
        if dialog is None:
            return
        try:
            fill(dialog)
            buttons = dialog.findChild(QDialogButtonBox)
            if buttons is None:
                raise CaptureError("operation dialog has no button box")
            button = buttons.button(QDialogButtonBox.StandardButton.Ok)
            if button is None:
                raise CaptureError("operation dialog has no OK button")
            handled[0] = True
            stop()
            button.click()
        except BaseException as error:
            failure.append(error)
            stop()
            dialog.reject()

    def expire() -> None:
        failure.append(CaptureError("operation dialog timed out"))
        stop()
        dialog = current_dialog()
        if dialog is not None:
            dialog.reject()

    poll.timeout.connect(accept)
    deadline.timeout.connect(expire)
    poll.start()
    deadline.start(15_000)
    try:
        action.trigger()
    finally:
        stop()
    if failure:
        raise CaptureError("operation dialog failed") from failure[0]
    if not handled[0]:
        raise CaptureError("operation dialog did not appear")


def _capture_window(window: Any, destination: Path) -> None:
    """Save one fixed-size application window without user-specific content."""
    from PySide6.QtCore import QSize

    window.resize(*IMAGE_SIZE)
    window.setMinimumSize(QSize(*IMAGE_SIZE))
    window.setMaximumSize(QSize(*IMAGE_SIZE))
    window.repaint()
    pixmap = window.grab()
    if (pixmap.width(), pixmap.height()) != IMAGE_SIZE:
        raise CaptureError(
            f"unexpected screenshot size {(pixmap.width(), pixmap.height())}"
        )
    if not pixmap.save(str(destination), "PNG"):
        raise CaptureError(f"could not save screenshot: {destination}")


def _anonymize_source_labels(window: Any) -> None:
    """Replace controlled temporary source paths with a stable public label."""
    for index in range(window.source_tree.topLevelItemCount()):
        item = window.source_tree.topLevelItem(index)
        if item is not None:
            item.setText(0, "Sample data")


def _prepare_environment(work_root: Path) -> None:
    """Keep all application caches, settings, and project paths temporary."""
    home = work_root / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    for name in (
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    ):
        path = work_root / name.lower().replace("xdg_", "")
        path.mkdir()
        os.environ[name] = str(path)
    mpl = work_root / "mpl"
    mpl.mkdir()
    os.environ["MPLCONFIGDIR"] = str(mpl)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _run_capture(output_dir: Path, source_root: Path) -> None:
    """Drive the normal launcher and atomically publish four screenshots."""
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gwexpy-installation-capture-") as raw:
        work_root = Path(raw)
        _prepare_environment(work_root)
        sys.path.insert(0, str(source_root))
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from gwexpy_studio.ui import app as launcher
        from gwexpy_studio.ui.bridge import BridgeState
        from gwexpy_studio.ui.dialogs import AsdDialog, CropDialog
        from gwexpy_studio.ui.io_panel import DataIOPanel

        staging = work_root / "images"
        staging.mkdir()
        app, window = launcher.create_app(("capture-installation-screenshots",))
        reopened = None
        try:
            _announce("welcome")
            window.show()
            _wait(
                app,
                lambda: window.isVisible() and window.welcome_panel.isVisible(),
                "welcome",
            )

            _announce("initialize workspace")
            window.initialize_workspace()
            _wait(
                app,
                lambda: window.welcome_panel.try_sample_button.isEnabled(),
                "sample availability",
            )
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and window._pending_action is None
                    and not window._command_reserved
                ),
                "workspace initialization",
            )
            window._show_status("Ready")
            _capture_window(window, staging / IMAGE_NAMES[0])
            _announce("try sample")
            QTest.mouseClick(
                window.welcome_panel.try_sample_button,
                Qt.MouseButton.LeftButton,
            )
            _wait(app, lambda: window.open_data_panel is not None, "Try Sample")
            panel = window.open_data_panel
            if not isinstance(panel, DataIOPanel):
                raise CaptureError("Try Sample did not open the data panel")
            _announce("sample catalog")
            _wait_for_sample_catalog(app, window, panel)
            _announce("sample load")
            QTest.mouseClick(panel.inspect_button, Qt.MouseButton.LeftButton)
            _wait(app, lambda: panel.confirm_button.isEnabled(), "sample inspection")
            QTest.mouseClick(panel.confirm_button, Qt.MouseButton.LeftButton)
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 1
                ),
                "sample load",
            )
            _anonymize_source_labels(window)
            _capture_window(window, staging / IMAGE_NAMES[1])

            _announce("crop")
            def fill_crop(dialog: Any) -> None:
                dialog.start_edit.setText("0.125")
                dialog.end_edit.setText("0.875")

            _accept_dialog(app, CropDialog, fill_crop, window.crop_action)
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 2
                ),
                "Crop",
            )

            _announce("asd")
            def fill_asd(dialog: Any) -> None:
                dialog.fftlength_edit.setText("0.125")
                dialog.overlap_edit.setText("0.0625")

            _accept_dialog(app, AsdDialog, fill_asd, window.asd_action)
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE
                    and len(window.project.objects) == 3
                ),
                "ASD",
            )
            _anonymize_source_labels(window)
            _capture_window(window, staging / IMAGE_NAMES[2])

            _announce("save project")
            project = work_root / "installation-guide.gwxproj"
            if not window.save_project_to(str(project)):
                raise CaptureError("Save Project did not start")
            _wait(
                app,
                lambda: (
                    window.bridge.state is BridgeState.IDLE and project.is_file()
                ),
                "Save Project",
            )

            _announce("reopen project")
            _, reopened = launcher.create_app(("capture-installation-screenshots",))
            reopened.show()
            reopened.start_initial_workspace(project)
            _wait(
                app,
                lambda: (
                    reopened.bridge.state is BridgeState.IDLE
                    and len(reopened.project.objects) == 3
                    and reopened._initial_project_path is None
                    and reopened._pending_action is None
                    and not reopened._command_reserved
                ),
                "Open Project",
            )
            reopened.central_placeholder.setText(
                "Data not restored\nChoose File → Review / Restore Data"
            )
            reopened.central_stack.setCurrentWidget(reopened.central_placeholder)
            reopened._show_status("Project opened — select Review / Restore Data")
            _anonymize_source_labels(reopened)
            _capture_window(reopened, staging / IMAGE_NAMES[3])
        finally:
            _announce("close windows")
            for candidate in (reopened, window):
                if candidate is None:
                    continue
                try:
                    _announce("close candidate")
                    # This process owns the temporary project and has no user
                    # edits to preserve.  Bypass the interactive unsaved-view
                    # prompt so offscreen cleanup remains deterministic.
                    candidate._workspace_close_authorized = True
                    candidate.close()
                    _wait(
                        app,
                        lambda candidate=candidate: (
                            not candidate.isVisible()
                            and not candidate.bridge.worker_thread.isRunning()
                        ),
                        "close",
                    )
                except (CaptureError, RuntimeError):
                    candidate.deleteLater()
            app.processEvents()
            app.quit()

        output_dir.mkdir(parents=True, exist_ok=True)
        for name in IMAGE_NAMES:
            shutil.copy2(staging / name, output_dir / name)


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the explicit source and output locations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "docs" / "images" / "installation",
    )
    parser.add_argument("--source-root", type=Path, default=ROOT / "src")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Capture and publish the four installation-guide screenshots."""
    arguments = _arguments(argv)
    _run_capture(arguments.output_dir, arguments.source_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
