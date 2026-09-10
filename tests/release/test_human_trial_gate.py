"""Cross-platform novice human-trial decision contracts."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _module():
    from scripts import evaluate_human_trial

    return evaluate_human_trial


def _trial(trial_id: str, target_id: str, workflow_seconds: int) -> dict[str, object]:
    return {
        "build_id": "P-abcdef0-20260910-r8-a1",
        "first_manual_use": True,
        "install_seconds": 600,
        "install_unassisted": True,
        "launch_unassisted": True,
        "project_reopen": True,
        "review_displayed": False,
        "review_continued": None,
        "restore_displayed": False,
        "restore_continued": None,
        "target_id": target_id,
        "trial_id": trial_id,
        "workflow_completed": True,
        "workflow_seconds": workflow_seconds,
    }


def test_human_gate_passes_two_wsl_and_one_mac_novices() -> None:
    summary = json.loads(
        _module().evaluate_human_trial(
            [
                _trial("T01", "wsl2-ubuntu24", 240),
                _trial("T02", "wsl2-ubuntu24", 300),
                _trial("T03", "macos15-arm64", 360),
            ],
            [],
        )
    )

    assert summary["decision"] == "pass"
    assert summary["gates"]["workflow_within_300_seconds"] == {
        "actual": 2,
        "required": 2,
    }
    assert summary["participants"] == 3


def test_human_gate_stops_for_same_p1_root_cause_twice() -> None:
    trials = [
        _trial("T01", "wsl2-ubuntu24", 240),
        _trial("T02", "wsl2-ubuntu24", 280),
        _trial("T03", "macos15-arm64", 290),
    ]
    issues = [
        {
            "issue_id": "I01",
            "platform": "wsl2-ubuntu24",
            "root_cause_id": "RC01",
            "severity": "P1",
            "stage": "launch",
            "symptom": "window did not appear",
            "trial_id": "T01",
            "workaround_attempted": False,
        },
        {
            "issue_id": "I02",
            "platform": "wsl2-ubuntu24",
            "root_cause_id": "RC01",
            "severity": "P1",
            "stage": "launch",
            "symptom": "window did not appear",
            "trial_id": "T02",
            "workaround_attempted": False,
        },
    ]

    summary = json.loads(_module().evaluate_human_trial(trials, issues))

    assert summary["decision"] == "stop_and_fix"
    assert summary["gates"]["same_p1_root_cause_at_least_twice"] is True


def test_human_gate_rejects_participant_severity_and_workaround() -> None:
    issue = {
        "issue_id": "I01",
        "platform": "wsl2-ubuntu24",
        "root_cause_id": "RC01",
        "severity": "P1",
        "stage": "install",
        "symptom": "failed",
        "trial_id": "T01",
        "workaround_attempted": True,
    }
    with pytest.raises(_module().HumanTrialError, match="workaround"):
        _module().evaluate_human_trial(
            [
                _trial("T01", "wsl2-ubuntu24", 240),
                _trial("T02", "wsl2-ubuntu24", 240),
                _trial("T03", "macos15-arm64", 240),
            ],
            [issue],
        )
