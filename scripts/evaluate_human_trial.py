"""Evaluate the cross-platform novice human-trial gate from anonymized facts."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    from .trial_targets import TrialTargetError, trial_target
except ImportError:  # pragma: no cover
    from trial_targets import TrialTargetError, trial_target  # type: ignore[no-redef]


class HumanTrialError(ValueError):
    """Raised when anonymized human-trial facts are malformed or incomplete."""


_TRIAL_FIELDS = {
    "build_id",
    "first_manual_use",
    "install_seconds",
    "install_unassisted",
    "launch_unassisted",
    "project_reopen",
    "review_continued",
    "review_displayed",
    "restore_continued",
    "restore_displayed",
    "target_id",
    "trial_id",
    "workflow_completed",
    "workflow_seconds",
}
_ISSUE_FIELDS = {
    "issue_id",
    "platform",
    "root_cause_id",
    "severity",
    "stage",
    "symptom",
    "trial_id",
    "workaround_attempted",
}
_TRIAL_ID = re.compile(r"T[0-9]{2,}")
_ISSUE_ID = re.compile(r"I[0-9]{2,}")
_BUILD_ID = re.compile(r"P-[0-9a-f]{7}-[0-9]{8}-r[1-9][0-9]*-a[1-9][0-9]*")
_PROJECT_STAGES = {
    "save",
    "close",
    "open",
    "project_reopen",
    "recovery",
    "review",
    "restore",
}


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _validated_trials(
    values: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    trials: list[dict[str, object]] = []
    ids: set[str] = set()
    for value in values:
        if set(value) != _TRIAL_FIELDS:
            raise HumanTrialError("human trial fields are invalid")
        trial_id = value.get("trial_id")
        target_id = value.get("target_id")
        build_id = value.get("build_id")
        if (
            not isinstance(trial_id, str)
            or _TRIAL_ID.fullmatch(trial_id) is None
            or trial_id in ids
        ):
            raise HumanTrialError("human trial ID is invalid or duplicated")
        if not isinstance(target_id, str) or not isinstance(build_id, str):
            raise HumanTrialError("human trial identity is invalid")
        try:
            trial_target(target_id)
        except TrialTargetError as exc:
            raise HumanTrialError(str(exc)) from exc
        if _BUILD_ID.fullmatch(build_id) is None:
            raise HumanTrialError("human trial Build ID is invalid")
        boolean_fields = (
            "first_manual_use",
            "install_unassisted",
            "launch_unassisted",
            "project_reopen",
            "review_displayed",
            "restore_displayed",
            "workflow_completed",
        )
        if any(type(value.get(field)) is not bool for field in boolean_fields):
            raise HumanTrialError("human trial outcome must be boolean")
        for displayed, continued in (
            ("review_displayed", "review_continued"),
            ("restore_displayed", "restore_continued"),
        ):
            expected_types = (bool,) if value[displayed] else (type(None),)
            if not isinstance(value[continued], expected_types):
                raise HumanTrialError("conditional Review/Restore result is invalid")
        for field in ("install_seconds", "workflow_seconds"):
            duration = value.get(field)
            if type(duration) is not int or duration < 0:
                raise HumanTrialError("human trial duration is invalid")
        ids.add(trial_id)
        trials.append(dict(value))
    return sorted(trials, key=lambda value: str(value["trial_id"]))


def _validated_issues(
    values: Sequence[Mapping[str, object]], trials: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    trial_by_id = {str(trial["trial_id"]): trial for trial in trials}
    issues: list[dict[str, object]] = []
    ids: set[str] = set()
    for value in values:
        if set(value) != _ISSUE_FIELDS:
            raise HumanTrialError("human issue fields are invalid")
        issue_id = value.get("issue_id")
        trial_id = value.get("trial_id")
        if (
            not isinstance(issue_id, str)
            or _ISSUE_ID.fullmatch(issue_id) is None
            or issue_id in ids
            or not isinstance(trial_id, str)
            or trial_id not in trial_by_id
        ):
            raise HumanTrialError("human issue identity is invalid")
        if value.get("platform") != trial_by_id[trial_id]["target_id"]:
            raise HumanTrialError("human issue platform does not match trial")
        if value.get("severity") not in {"P0", "P1", "P2", "P3"}:
            raise HumanTrialError("human issue severity is invalid")
        if value.get("workaround_attempted") is not False:
            raise HumanTrialError("workaround must not be attempted")
        if any(
            not isinstance(value.get(field), str) or not value[field]
            for field in ("root_cause_id", "stage", "symptom")
        ):
            raise HumanTrialError("human issue fact is invalid")
        ids.add(issue_id)
        issues.append(dict(value))
    return sorted(issues, key=lambda value: str(value["issue_id"]))


def evaluate_human_trial(
    trial_values: Sequence[Mapping[str, object]],
    issue_values: Sequence[Mapping[str, object]],
) -> bytes:
    """Return a canonical Pass/Stop summary without raw participant data."""
    trials = _validated_trials(trial_values)
    issues = _validated_issues(issue_values, trials)
    participants = len(trials)
    required_workflow = math.ceil(2 * participants / 3)
    target_counts = Counter(str(trial["target_id"]) for trial in trials)
    p0 = sum(issue["severity"] == "P0" for issue in issues)
    project_p1 = sum(
        issue["severity"] == "P1" and issue["stage"] in _PROJECT_STAGES
        for issue in issues
    )
    p1_roots = Counter(
        str(issue["root_cause_id"]) for issue in issues if issue["severity"] == "P1"
    )
    same_p1_twice = any(count >= 2 for count in p1_roots.values())
    counts = {
        "first_manual_use": sum(trial["first_manual_use"] is True for trial in trials),
        "install_unassisted": sum(
            trial["install_unassisted"] is True for trial in trials
        ),
        "launch_unassisted": sum(
            trial["launch_unassisted"] is True for trial in trials
        ),
        "project_reopen": sum(trial["project_reopen"] is True for trial in trials),
        "workflow_completed": sum(
            trial["workflow_completed"] is True for trial in trials
        ),
        "workflow_within_300_seconds": sum(
            trial["workflow_completed"] is True
            and isinstance(trial["workflow_seconds"], int)
            and trial["workflow_seconds"] <= 300
            for trial in trials
        ),
    }
    passed = (
        participants >= 3
        and target_counts["wsl2-ubuntu24"] >= 2
        and target_counts["macos15-arm64"] >= 1
        and all(
            counts[name] == participants
            for name in (
                "first_manual_use",
                "install_unassisted",
                "launch_unassisted",
                "project_reopen",
                "workflow_completed",
            )
        )
        and counts["workflow_within_300_seconds"] >= required_workflow
        and p0 == 0
        and project_p1 == 0
        and not same_p1_twice
    )
    summary = {
        "decision": "pass" if passed else "stop_and_fix",
        "gates": {
            "first_manual_use": {
                "actual": counts["first_manual_use"],
                "required": participants,
            },
            "install_unassisted": {
                "actual": counts["install_unassisted"],
                "required": participants,
            },
            "launch_unassisted": {
                "actual": counts["launch_unassisted"],
                "required": participants,
            },
            "macos_participants": {
                "actual": target_counts["macos15-arm64"],
                "required": 1,
            },
            "p0": {"actual": p0, "required": 0},
            "project_recovery_p1": {"actual": project_p1, "required": 0},
            "project_reopen": {
                "actual": counts["project_reopen"],
                "required": participants,
            },
            "same_p1_root_cause_at_least_twice": same_p1_twice,
            "workflow_completed": {
                "actual": counts["workflow_completed"],
                "required": participants,
            },
            "workflow_within_300_seconds": {
                "actual": counts["workflow_within_300_seconds"],
                "required": required_workflow,
            },
            "wsl2_participants": {
                "actual": target_counts["wsl2-ubuntu24"],
                "required": 2,
            },
        },
        "issues": len(issues),
        "participants": participants,
        "schema": 1,
    }
    return _canonical_json(summary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", required=True, type=Path)
    parser.add_argument("--issues", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Evaluate anonymized human-trial JSON from the command line."""
    arguments = _parser().parse_args(argv)
    try:
        trials = json.loads(arguments.trials.read_text(encoding="utf-8"))
        issues = json.loads(arguments.issues.read_text(encoding="utf-8"))
        if not isinstance(trials, list) or not isinstance(issues, list):
            raise HumanTrialError("human trial input must be JSON arrays")
        arguments.output.write_bytes(evaluate_human_trial(trials, issues))
    except (HumanTrialError, OSError, json.JSONDecodeError) as exc:
        print(f"evaluate_human_trial: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
