"""Actual Qt GUI death preserves committed work at three durable boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from gwexpy_studio.application.workspace_controller import WorkspaceController
from gwexpy_studio.persistence.project_io import load_project
from gwexpy_studio.persistence.recovery import RecoveryStore
from gwexpy_studio.worker.client import WorkerClient


@pytest.mark.parametrize(
    "phase",
    [
        pytest.param(
            "analysis_intent",
            marks=pytest.mark.contract("WSP-0105"),
            id="analysis_intent",
        ),
        pytest.param(
            "save_replace", marks=pytest.mark.contract("WSP-0106"), id="save_replace"
        ),
        pytest.param(
            "write_reply", marks=pytest.mark.contract("WSP-0107"), id="write_reply"
        ),
    ],
)
def test_real_gui_sigkill_preserves_committed_history_and_unapproved_draft(
    tmp_path, monkeypatch, phase
):
    assert sys.platform == "linux", "Crash acceptance requires Linux"
    helper = Path(__file__).parents[1] / "support" / "workspace_crash.py"
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "GWEXPY_STUDIO_SHM_PREFIX": "gui-crash-" + uuid.uuid4().hex + "-",
    }
    result = subprocess.run(
        [sys.executable, str(helper), "supervise", str(tmp_path), phase],
        env=env,
        capture_output=True,
        text=True,
        timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome == {"owned_processes": 0, "shared_memory": 0, "phase": phase}
    baseline = json.loads((tmp_path / "baseline.json").read_text())
    root = tmp_path / "recovery"
    observer = RecoveryStore(root)
    candidates = observer.candidates()
    assert len(candidates) == 1
    snapshot = candidates[0]
    pending_kind = {
        "analysis_intent": "analysis",
        "save_replace": "save_project",
        "write_reply": "write_data",
    }[phase]
    assert snapshot.pending["kind"] == pending_kind
    assert snapshot.project().history.cursor == 1
    assert len(snapshot.project().history.timeline) == 2
    project_path = tmp_path / "保存済み.gwxproj"
    persisted = load_project(project_path)
    assert persisted.history.cursor == 1
    assert persisted.to_dict()["operations"] == baseline["operations"]
    before_project = project_path.read_bytes()
    output = tmp_path / "partial.h5"
    before_output = output.read_bytes() if output.exists() else None
    if phase == "write_reply":
        assert before_output == b"partial output awaiting confirmation\n"
    if phase == "save_replace":
        assert list(tmp_path.glob(".gwx-*.tmp"))
        assert list(tmp_path.glob(".gwx-*.old"))
    (tmp_path / "入力.h5").unlink()

    def no_automatic_execution(*args, **kwargs):
        raise AssertionError(
            "Recovery must not start or retry scientific/external work"
        )

    monkeypatch.setattr(WorkerClient, "start", no_automatic_execution)
    controller = WorkspaceController(recovery_root=root)
    try:
        controller.recover_workspace(snapshot.run_id)
        current = controller.project.to_dict()
        for name in (
            "operations",
            "objects",
            "executions",
            "history",
            "source_manifests",
            "ui_state",
        ):
            assert current[name] == baseline[name]
        draft = current["ui_state"]["panel_drafts"]["parameter_panel"]
        assert draft["saved"]["timeseries.lowpass"]["frequency"] == "17.125"
        status = controller.workspace_status()
        assert status["dirty"] and status["needs_restore"]
        assert status["project_path"] is None
        assert status["recovery_error"] is None
        interruption = controller.project.activities[-1]
        assert interruption.error["code"] == (
            "output_unconfirmed" if phase == "write_reply" else "operation_interrupted"
        )
        assert project_path.read_bytes() == before_project
        assert (output.read_bytes() if output.exists() else None) == before_output
        assert not list(tmp_path.glob(".gwx-*"))
    finally:
        controller.finalize_workspace(clean=True)
        observer.close(clean=True)
