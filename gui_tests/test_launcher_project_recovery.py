"""Explicit-project startup and non-blocking recovery notice contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from gwexpy_studio.domain.project import Project
from gwexpy_studio.ui.bridge import BridgeResult, BridgeState
from gwexpy_studio.ui.window import MainWindow


class _RecordingBridge(QObject):
    """Small idle bridge that makes startup command order observable."""

    result_received = Signal(object)
    safe_to_destroy = Signal()
    state_changed = Signal(str)
    progress_received = Signal(object)
    state = BridgeState.IDLE

    def __init__(self) -> None:
        super().__init__()
        self.commands: list[tuple[str, dict[str, object]]] = []
        self.worker_thread = SimpleNamespace(isRunning=lambda: False)

    def send_command(self, kind: str, payload: dict[str, object]) -> str:
        self.commands.append((kind, payload))
        return f"command-{len(self.commands)}"

    def close(self, **_kwargs: object) -> None:
        """Provide the normal bridge cleanup surface."""

    def request_cancel(self) -> None:
        """Provide the normal bridge cancellation surface."""


def _opened_project_result(project: Project) -> BridgeResult:
    return BridgeResult(
        command_id="command-1",
        success=True,
        payload={
            "project": project,
            "workspace_status": {
                "generation": 1,
                "revision": 1,
                "dirty": False,
                "needs_restore": False,
                "resident_object_ids": [],
            },
        },
    )


@pytest.mark.gui
@pytest.mark.contract("GUI-TRL-001")
def test_explicit_project_opens_before_recovery_listing(qapp, tmp_path: Path) -> None:
    """A project argument is opened before Studio asks for recoveries."""
    bridge = _RecordingBridge()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    target = tmp_path / "trial.gwxproj"

    window.start_initial_workspace(target)

    assert bridge.commands == [("open_project", {"path": str(target.resolve())})]
    window._on_bridge_result(
        _opened_project_result(
            Project(
                project_id="trial-project",
                modified="2026-09-06T10:00:00+00:00",
            )
        )
    )

    assert bridge.commands == [
        ("open_project", {"path": str(target.resolve())}),
        ("list_recoveries", {}),
    ]
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.gui
@pytest.mark.contract("GUI-TRL-002")
def test_newer_same_project_recovery_is_a_nonblocking_notice(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup never auto-restores or opens a modal for the explicit project."""
    bridge = _RecordingBridge()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    target = tmp_path / "trial.gwxproj"
    window.show()
    window.start_initial_workspace(target)
    window._on_bridge_result(
        _opened_project_result(
            Project(
                project_id="trial-project",
                modified="2026-09-06T10:00:00+00:00",
            )
        )
    )
    monkeypatch.setattr(
        window,
        "_show_recovery_candidates",
        lambda _candidates: pytest.fail(
            "explicit project startup opened recovery modal"
        ),
    )

    window._on_bridge_result(
        BridgeResult(
            command_id="command-2",
            success=True,
            payload={
                "recoveries": [
                    {
                        "run_id": "candidate",
                        "original_path": str(target),
                        "project_id": "trial-project",
                        "created_at": "2026-09-06T11:00:00+00:00",
                    }
                ]
            },
        )
    )

    assert window.recovery_notice.isVisible()
    assert "newer recovery" in window.recovery_notice.text().lower()
    assert [kind for kind, _payload in bridge.commands] == [
        "open_project",
        "list_recoveries",
    ]
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.gui
@pytest.mark.parametrize(
    "candidate",
    (
        pytest.param(
            {
                "original_path": "other.gwxproj",
                "project_id": "trial-project",
                "created_at": "2026-09-06T11:00:00+00:00",
            },
            id="other-path",
            marks=pytest.mark.contract("GUI-TRL-003"),
        ),
        pytest.param(
            {
                "original_path": "trial.gwxproj",
                "project_id": "different-project",
                "created_at": "2026-09-06T11:00:00+00:00",
            },
            id="other-project",
            marks=pytest.mark.contract("GUI-TRL-004"),
        ),
        pytest.param(
            {
                "original_path": "trial.gwxproj",
                "project_id": "trial-project",
                "created_at": "2026-09-06T09:00:00+00:00",
            },
            id="older",
            marks=pytest.mark.contract("GUI-TRL-005"),
        ),
        pytest.param(
            {
                "original_path": "trial.gwxproj",
                "project_id": "trial-project",
                "created_at": "2026-09-06T10:00:00+00:00",
            },
            id="same-time",
            marks=pytest.mark.contract("GUI-TRL-006"),
        ),
    ),
)
def test_unrelated_or_stale_recovery_does_not_interrupt_explicit_project_startup(
    qapp, tmp_path: Path, candidate: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a strictly newer recovery of the same saved project raises a notice."""
    bridge = _RecordingBridge()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    target = tmp_path / "trial.gwxproj"
    window.start_initial_workspace(target)
    window._on_bridge_result(
        _opened_project_result(
            Project(
                project_id="trial-project",
                modified="2026-09-06T10:00:00+00:00",
            )
        )
    )
    monkeypatch.setattr(
        window,
        "_show_recovery_candidates",
        lambda _candidates: pytest.fail(
            "explicit project startup opened recovery modal"
        ),
    )
    payload = {"run_id": "candidate", **candidate}
    if payload["original_path"] == "trial.gwxproj":
        payload["original_path"] = str(target)

    window._on_bridge_result(
        BridgeResult(
            command_id="command-2",
            success=True,
            payload={"recoveries": [payload]},
        )
    )

    assert not window.recovery_notice.isVisible()
    assert [kind for kind, _payload in bridge.commands] == [
        "open_project",
        "list_recoveries",
    ]
    window.deleteLater()
    qapp.processEvents()


@pytest.mark.gui
@pytest.mark.contract("GUI-TRL-007")
def test_matching_recovery_with_malformed_timestamp_is_exposed_without_action(
    qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed time never hides a matching recovery or triggers an action."""
    bridge = _RecordingBridge()
    window = MainWindow(bridge=bridge)  # type: ignore[arg-type]
    target = tmp_path / "trial.gwxproj"
    window.show()
    window.start_initial_workspace(target)
    window._on_bridge_result(
        _opened_project_result(
            Project(
                project_id="trial-project",
                modified="not-an-iso-timestamp",
            )
        )
    )
    monkeypatch.setattr(
        window,
        "_show_recovery_candidates",
        lambda _candidates: pytest.fail(
            "explicit project startup opened recovery modal"
        ),
    )

    window._on_bridge_result(
        BridgeResult(
            command_id="command-2",
            success=True,
            payload={
                "recoveries": [
                    {
                        "run_id": "candidate",
                        "original_path": str(target),
                        "project_id": "trial-project",
                        "created_at": "also-not-an-iso-timestamp",
                    }
                ]
            },
        )
    )

    assert window.recovery_notice.isVisible()
    assert [kind for kind, _payload in bridge.commands] == [
        "open_project",
        "list_recoveries",
    ]
    window.deleteLater()
    qapp.processEvents()
