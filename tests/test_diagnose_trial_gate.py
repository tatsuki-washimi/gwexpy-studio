"""Non-GUI contracts for the bounded trial-gate diagnostic observer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.diagnose_trial_gate import (
    OBSERVATION_KEYS,
    DiagnosticObservation,
    DiagnosticSchemaError,
    _result_is_current,
    classify_process_outcome,
    parse_snapshot,
)


def _observation() -> DiagnosticObservation:
    return DiagnosticObservation(
        branch_sha="a" * 40,
        original_gate_sha="b" * 64,
        source_sha="c" * 40,
        wheel_sha="d" * 64,
    )


def test_snapshot_is_closed_and_never_contains_raw_callback_text() -> None:
    observation = _observation()
    observation.record_stage("io-read")

    def callback() -> None:
        raise RuntimeError("secret path /home/user/token=abc")

    with pytest.raises(RuntimeError):
        observation.forward_callback(callback)

    document = json.loads(observation.to_json())
    assert set(document) == set(OBSERVATION_KEYS)
    assert document["failure_category"] == "callback_failure"
    encoded = observation.to_json()
    assert b"secret" not in encoded
    assert b"/home" not in encoded
    assert b"token" not in encoded
    assert parse_snapshot(encoded)["schema"] == 1


def test_forward_callback_preserves_return_value_and_arguments() -> None:
    observation = _observation()
    seen: list[tuple[str, int]] = []

    def callback(label: str, number: int) -> str:
        seen.append((label, number))
        return "accepted"

    assert observation.forward_callback(callback, "read", 7) == "accepted"
    assert seen == [("read", 7)]
    assert observation.failure_category == "none"


def test_watchdog_and_process_exit_have_distinct_fixed_outcomes() -> None:
    watchdog = classify_process_outcome(returncode=None, timed_out=True)
    exited = classify_process_outcome(returncode=1, timed_out=False)
    passed = classify_process_outcome(returncode=0, timed_out=False)

    assert watchdog == ("watchdog", "phase_watchdog")
    assert exited == ("process_exit", "process_exit")
    assert passed == ("passed", "none")


def test_parse_snapshot_rejects_extra_keys() -> None:
    observation = _observation()
    document = json.loads(observation.to_json())
    document["raw_exception"] = "must not be accepted"

    with pytest.raises(DiagnosticSchemaError):
        parse_snapshot(json.dumps(document).encode("utf-8"))


def test_restore_and_autopreview_boundaries_are_observed_separately() -> None:
    observation = _observation()
    observation.observe_dispatch("review_restore", True)
    observation.observe_result("preview", True)
    assert observation.observations["restore_dispatch_requested"] is False
    assert observation.observations["autopreview_result"] == "unknown"

    observation.observe_dispatch_requested("restore_project")
    assert observation.observations["restore_dispatch_requested"] is True
    assert observation.observations["restore_dispatch_accepted"] is False
    observation.observe_dispatch("restore_project", False)
    assert observation.observations["restore_dispatch_accepted"] is False
    observation.observe_result("restore_project", True)
    assert observation.observations["autopreview_result"] == "unknown"
    observation.observe_result("preview", True)
    assert observation.observations["autopreview_result"] == "success"


def test_wait_expiry_is_terminal_failed_before_parent_process_exit() -> None:
    observation = _observation()
    observation.record_stage("io_read")
    observation.record_wait("untrusted label", False)

    assert observation.outcome == "failed"
    assert observation.failure_category == "wait_expired"
    assert observation.stage == "io_read"


def test_failed_restore_result_stays_distinct_from_later_wait_expiry() -> None:
    observation = _observation()
    observation.observe_result("restore_project", False)
    observation.record_wait("restore", False)

    assert observation.observations["restore_result_observed"] is True
    assert observation.observations["restore_result_success"] is False
    assert observation.failure_category == "result_failure"


def test_stale_bridge_result_is_not_current() -> None:
    assert _result_is_current("current", "stale") is False
    assert _result_is_current("current", "current") is True
    assert _result_is_current(None, "unmatched") is True


def test_residency_requires_the_object_captured_at_io_read_settled() -> None:
    observation = _observation()
    observation.record_stage("io_read_settled")
    window = SimpleNamespace(
        bridge=SimpleNamespace(
            state="idle",
            worker_thread=SimpleNamespace(isRunning=lambda: False),
        ),
        _pending_action=None,
        _command_reserved=False,
        _modal_active=False,
        project=SimpleNamespace(objects=[SimpleNamespace(object_id="expected")]),
        _workspace_status={"resident_object_ids": ["other"]},
        open_data_panel=None,
    )
    observation.observe_window(window)
    assert observation.observations["object_present"] is True
    assert observation.observations["object_resident"] is False
    window._workspace_status["resident_object_ids"] = ["expected"]
    observation.observe_window(window)
    assert observation.observations["object_resident"] is True


def test_read_result_captures_target_before_preview_settlement() -> None:
    observation = _observation()
    observation.record_stage("io_read")
    observation.observe_result("signal_read_io", True)
    window = SimpleNamespace(
        bridge=SimpleNamespace(
            state="idle",
            worker_thread=SimpleNamespace(isRunning=lambda: False),
        ),
        _pending_action=None,
        _command_reserved=False,
        _modal_active=False,
        project=SimpleNamespace(objects=[SimpleNamespace(object_id="read-target")]),
        _workspace_status={
            "resident_object_ids": ["read-target"],
            "needs_restore": False,
        },
        open_data_panel=None,
    )

    observation.observe_window(window)

    assert observation.observations["read_result_success"] is True
    assert observation.observations["object_present"] is True
    assert observation.observations["object_resident"] is True
    assert observation.observations["preview_completed"] is False
