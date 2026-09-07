"""GUI test fixtures — fully isolated from headless test infrastructure."""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    pass


def _assert_no_scientific_imports() -> None:
    """Assert that gwexpy and gwpy are not in sys.modules."""
    for mod_name in ("gwexpy", "gwpy"):
        if mod_name in sys.modules:
            pytest.fail(
                f"GUI process must not import {mod_name}",
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def isolated_workspace_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every Studio-owned XDG write inside this GUI test's directory."""
    homes = {
        "XDG_CONFIG_HOME": tmp_path / "config",
        "XDG_DATA_HOME": tmp_path / "data",
        "XDG_STATE_HOME": tmp_path / "state",
        "XDG_CACHE_HOME": tmp_path / "cache",
    }
    for name, directory in homes.items():
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv(
        "MPLCONFIGDIR", str(homes["XDG_CACHE_HOME"] / "gwexpy-studio" / "matplotlib")
    )


@pytest.fixture(autouse=True)
def mock_bridge_capability_handshake(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Put inert direct-window bridge doubles through the real startup contract.

    Production tests using ``WorkerBridge`` retain their real worker process.
    A focused startup test can opt out with ``explicit_capability_handshake``
    to observe the unavailable state and supply individual bridge results.
    """
    if request.node.get_closest_marker("explicit_capability_handshake"):
        return

    from gwexpy_studio.ops.io_capabilities import (
        CapabilityManifest,
        probe_effective_capabilities,
    )
    from gwexpy_studio.ui.bridge import BridgeResult, BridgeState, WorkerBridge
    from gwexpy_studio.ui.window import MainWindow

    snapshot = probe_effective_capabilities(CapabilityManifest(mode="developer"))
    original_init = MainWindow.__init__

    def initialized_window(self: MainWindow, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        bridge = self.bridge
        commands = getattr(bridge, "commands", None)
        if (
            isinstance(bridge, WorkerBridge)
            or bridge.state is not BridgeState.IDLE
            or not isinstance(commands, list)
        ):
            return

        self.initialize_workspace()
        start_id = self._pending_command_id
        assert start_id is not None
        self._on_bridge_result(
            BridgeResult(
                command_id=start_id,
                success=True,
                payload={"ready": True, "io_capabilities": snapshot.document()},
            )
        )
        recovery_id = self._pending_command_id
        assert recovery_id is not None
        self._on_bridge_result(
            BridgeResult(
                command_id=recovery_id,
                success=True,
                payload={"recoveries": []},
            )
        )
        commands.clear()

    monkeypatch.setattr(MainWindow, "__init__", initialized_window)


@pytest.fixture(scope="session", autouse=True)
def gui_environment_guard() -> None:
    """Validate GUI environment without importing scientific packages."""
    from importlib.metadata import version as dist_version

    from packaging.version import Version

    # Verify PySide6 is available
    try:
        pyside_ver = Version(dist_version("PySide6-Essentials"))
    except Exception:
        pytest.fail(
            "PySide6-Essentials is not installed in the GUI test environment",
            pytrace=False,
        )

    if not (Version("6.11.0") <= pyside_ver < Version("7.0.0")):
        pytest.fail(
            f"PySide6-Essentials {pyside_ver} is outside verified range "
            "[6.11.0, 7.0.0)",
            pytrace=False,
        )

    _assert_no_scientific_imports()


@pytest.fixture(scope="session")
def qapp(gui_environment_guard: None) -> Generator[Any, None, None]:
    """Create and own the single QApplication for the test session."""
    del gui_environment_guard
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # Ensure no prior QApplication exists
    existing = QApplication.instance()
    if existing is not None:
        pytest.fail(
            "A QApplication already exists before session fixture",
            pytrace=False,
        )

    raw_app = QApplication.instance() or QApplication([])
    if not isinstance(raw_app, QApplication):
        pytest.fail("Existing app is not a QApplication", pytrace=False)
    app: QApplication = raw_app
    app.setAttribute(Qt.ApplicationAttribute.AA_Use96Dpi, True)

    _assert_no_scientific_imports()

    yield app

    # Teardown: process pending events and close
    app.processEvents()
    # Verify no top-level widgets remain
    remaining = [w for w in app.topLevelWidgets() if w.isVisible()]
    if remaining:
        for w in remaining:
            w.close()
            w.deleteLater()
        app.processEvents()

    _assert_no_scientific_imports()
