"""Non-GUI contracts for the bounded trial-gate diagnostic observer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts.diagnose_trial_gate import (
    OBSERVATION_KEYS,
    DiagnosticObservation,
    DiagnosticSchemaError,
    _hash_id,
    _result_is_current,
    _safe_code,
    _safe_label,
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


def test_trace_records_ordered_boundaries_without_raw_text(tmp_path) -> None:
    observation = DiagnosticObservation(
        branch_sha="a" * 40,
        original_gate_sha="b" * 64,
        source_sha="c" * 40,
        wheel_sha="d" * 64,
        snapshot_path=tmp_path / "diagnostic-snapshot.json",
    )
    observation.record_stage("io_read")
    observation.record_wait("normal I/O read", False)
    observation.observe_dispatch("preview", True)
    observation.observe_result("preview", True, current=False)
    observation.observe_select("select_entered", {"kind_allowed": True})
    observation.observe_error_code("invalid_response")
    observation.observe_predicate_vector(
        bridge_idle=True,
        object_count=1,
        resident_nonempty=True,
        preview_seen=False,
    )

    import json as _json

    trace = _json.loads(
        (tmp_path / "diagnostic-trace.json").read_bytes().decode("utf-8")
    )
    assert trace["schema"] == 1
    assert isinstance(trace["instrumented"], list) and trace["instrumented"]
    events = [entry["event"] for entry in trace["events"]]
    assert events == [
        "stage",
        "wait",
        "dispatch_returned",
        "result",
        "select_entered",
        "show_error",
        "predicate_vector",
    ]
    seqs = [entry["seq"] for entry in trace["events"]]
    assert seqs == sorted(seqs)
    assert all(entry["mono_ns"] > 0 for entry in trace["events"])
    raw = (tmp_path / "diagnostic-trace.json").read_bytes()
    assert b"/home" not in raw
    # Snapshot schema stays frozen at the closed field set.
    document = _json.loads(observation.to_json())
    assert set(document) == set(OBSERVATION_KEYS)


def test_trace_sanitizes_untrusted_label_and_code(tmp_path) -> None:
    assert _safe_label("normal I/O read") == "normal I/O read"
    assert _safe_label("/home/user/secret") == "untrusted"
    assert _safe_code("invalid_response") == "invalid_response"
    assert _safe_code("has space") == "unknown_code"
    assert _hash_id(None) == "none"
    assert len(_hash_id("object-1")) == 16
