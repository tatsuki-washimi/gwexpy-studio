"""Offscreen launcher policy failure contracts for a generated trial wheel."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from gwexpy_studio.ui.bridge import BridgeState
from gwexpy_studio.ui.window import MainWindow

_TRIAL_VERSION = "0.1.0a1+trial.p.g0854741.20260907.r1.a1"


def _wait_for_capability_result(window: MainWindow, qapp: QApplication) -> None:
    """Pump the real child worker until the capability start command settles."""
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if (
            not window._command_reserved
            and window.bridge.state
            in {BridgeState.IDLE, BridgeState.FAILED, BridgeState.CLOSED}
        ):
            return
        QTest.qWait(10)
    pytest.fail("worker capability startup did not settle")


@pytest.mark.gui
def test_trial_gate_message_clicker_closes_the_expected_message_box(
    qapp: QApplication,
) -> None:
    """The recovery gate uses the native button signal in an offscreen modal."""
    from scripts import run_trial_technical_gate as gate

    owner = QWidget()
    dialog = QMessageBox(owner)
    dialog.setWindowTitle("Recover unfinished work")
    dialog.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
    click = gate._schedule_message_box_button(
        app=qapp,
        owner=owner,
        label="Restore",
        title="Recover unfinished work",
        required=True,
    )
    try:
        dialog.exec()
        click.raise_if_failed()
        assert click.handled
    finally:
        dialog.deleteLater()
        owner.deleteLater()
        qapp.processEvents()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-TRL-IO-001")
def test_missing_trial_policy_never_enables_data_or_project_actions(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trial launcher rejects an absent asset instead of inheriting user policy."""
    import gwexpy_studio.ui.app as app_module
    from gwexpy_studio.ops.io_capabilities import CAPABILITY_ENVIRONMENT

    package = tmp_path / "gwexpy_studio"
    assets = package / "assets"
    assets.mkdir(parents=True)
    (assets / "trial-build.json").write_text(
        json.dumps(
            {
                "build_id": "P-0854741-20260907-r1-a1",
                "schema": 1,
                "source_manifest_sha256": "b" * 64,
                "source_sha": "0854741" + "a" * 33,
                "version": _TRIAL_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module.resources, "files", lambda _package: package)
    monkeypatch.setattr(
        app_module,
        "_installed_package_version",
        lambda: _TRIAL_VERSION,
    )
    monkeypatch.setenv(CAPABILITY_ENVIRONMENT, "/outside/unreviewed-policy.json")

    window: MainWindow | None = None
    try:
        with app_module._trial_capability_environment():
            assert (
                os.environ[CAPABILITY_ENVIRONMENT]
                != "/outside/unreviewed-policy.json"
            )
            window = MainWindow()
            window.show()
            window.initialize_workspace()
            _wait_for_capability_result(window, qapp)

            assert window._io_capability_ready is False
            assert not window.open_action.isEnabled()
            assert not window.export_action.isEnabled()
            assert not window.open_project_action.isEnabled()
    finally:
        if window is not None:
            window._workspace_close_authorized = True
            window.close()
            window.deleteLater()
            qapp.processEvents()

    assert os.environ[CAPABILITY_ENVIRONMENT] == "/outside/unreviewed-policy.json"
