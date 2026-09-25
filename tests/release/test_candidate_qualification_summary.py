"""Branch binding for complete, verified publication summaries."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

import scripts.verify_publication_groups as groups
from scripts.trial_targets import target_ids, trial_target
from scripts.verify_publication_groups import (
    PublicationGroupError,
    _run_identity,
    candidate_qualification_summary_bytes,
    fetch_candidate_build_run_provenance,
    read_candidate_qualification_summary,
    read_publication_summary,
    verify_candidate_build_run_provenance,
)

SOURCE = "a" * 40
BRANCH = "candidate/studio-alpha"


def _canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _physical_summary() -> bytes:
    rows = []
    for number, target_id in enumerate(sorted(target_ids()), 1):
        run_id = 100 + number
        attempt = 1
        rows.append(
            {
                "artifacts": {
                    label: {
                        "digest": "sha256:" + ("a" if label == "release" else "b") * 64,
                        "id": run_id * 10 + index,
                        "name": f"trial-{prefix}-{target_id}-{run_id}-a{attempt}",
                    }
                    for index, (label, prefix) in enumerate(
                        (("release", "bundle"), ("kit", "qualification-kit")), 1
                    )
                },
                "build_id": f"P-{SOURCE[:7]}-20260926-r{number}-a{attempt}",
                "kit_manifest_sha256": "b" * 64,
                "qualification_results": [
                    {"architecture": arch, "sha256": "c" * 64}
                    for arch in sorted(trial_target(target_id).architectures)
                ],
                "source_sha": SOURCE,
                "target_id": target_id,
                "wheel_sha256": "d" * 64,
                "workflow": {
                    "repository": "example/studio",
                    "run_attempt": attempt,
                    "run_id": run_id,
                    "run_number": number,
                    "source_sha": SOURCE,
                    "workflow_path": ".github/workflows/build-trial-wheel.yml",
                },
                "zip_sha256": "e" * 64,
            }
        )
    return _canonical(
        {
            "kind": "audit",
            "publication_group": None,
            "schema": 2,
            "source_sha": SOURCE,
            "status": "passed",
            "targets": rows,
        }
    )


def _selected(physical: bytes) -> tuple[str, int, int, str]:
    row = read_publication_summary(physical, kind="audit")["targets"][0]
    release = row["artifacts"]["release"]
    return row["target_id"], row["workflow"]["run_id"], release["id"], release["digest"]


def _wrap(physical: bytes, **overrides: object) -> bytes:
    target_id, run_id, artifact_id, artifact_digest = _selected(physical)
    values = {
        "candidate_branch": BRANCH,
        "source_sha": SOURCE,
        "target_id": target_id,
        "build_run_id": run_id,
        "release_artifact_id": artifact_id,
        "release_artifact_digest": artifact_digest,
        **overrides,
    }
    return candidate_qualification_summary_bytes(physical, **values)


def _build_runs(physical: bytes) -> dict[int, dict[str, object]]:
    rows = read_publication_summary(physical, kind="audit")["targets"]
    return {
        row["workflow"]["run_id"]: {
            "id": row["workflow"]["run_id"],
            "run_number": row["workflow"]["run_number"],
            "run_attempt": row["workflow"]["run_attempt"],
            "path": ".github/workflows/build-trial-wheel.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": BRANCH,
            "head_sha": SOURCE,
            "repository": {"full_name": "example/studio"},
        }
        for row in rows
    }


def test_candidate_audit_rechecks_all_four_build_branches() -> None:
    physical = _physical_summary()
    runs = _build_runs(physical)
    verify_candidate_build_run_provenance(
        _wrap(physical), physical, repository="example/studio", runs=runs
    )
    runs[102]["head_branch"] = "candidate/other"
    with pytest.raises(PublicationGroupError, match="reviewed branch"):
        verify_candidate_build_run_provenance(
            _wrap(physical), physical, repository="example/studio", runs=runs
        )


def test_candidate_audit_rejects_tampered_schema3_branch() -> None:
    physical = _physical_summary()
    with pytest.raises(PublicationGroupError, match="reviewed branch"):
        verify_candidate_build_run_provenance(
            _wrap(physical, candidate_branch="candidate/forged"),
            physical,
            repository="example/studio",
            runs=_build_runs(physical),
        )


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("run_number", 99)])
def test_candidate_audit_rechecks_nonselected_build_identity(
    field: str, value: object
) -> None:
    physical = _physical_summary()
    runs = _build_runs(physical)
    runs[102][field] = value
    with pytest.raises(PublicationGroupError):
        verify_candidate_build_run_provenance(
            _wrap(physical), physical, repository="example/studio", runs=runs
        )


def test_candidate_audit_rejects_missing_run() -> None:
    physical = _physical_summary()
    runs = _build_runs(physical)
    del runs[102]
    with pytest.raises(PublicationGroupError, match="run set"):
        verify_candidate_build_run_provenance(
            _wrap(physical), physical, repository="example/studio", runs=runs
        )


def test_candidate_audit_fetches_all_four_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    physical = _physical_summary()
    runs = _build_runs(physical)
    requested: list[int] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        run_id = int(command[-1].rsplit("/", 1)[-1])
        requested.append(run_id)
        return subprocess.CompletedProcess(command, 0, json.dumps(runs[run_id]), "")

    monkeypatch.setattr(groups.subprocess, "run", fake_run)
    fetch_candidate_build_run_provenance(
        _wrap(physical), physical, repository="example/studio"
    )
    assert requested == [101, 102, 103, 104]
    runs[104]["head_branch"] = "candidate/other"
    with pytest.raises(PublicationGroupError, match="reviewed branch"):
        fetch_candidate_build_run_provenance(
            _wrap(physical), physical, repository="example/studio"
        )


def test_candidate_summary_binds_complete_audit_and_selected_build() -> None:
    physical = _physical_summary()
    raw = _wrap(physical)
    document = read_candidate_qualification_summary(raw, physical)
    target_id, run_id, artifact_id, artifact_digest = _selected(physical)
    assert document["schema"] == 3
    assert document["candidate_branch"] == BRANCH
    assert (
        document["qualification_summary_sha256"] == hashlib.sha256(physical).hexdigest()
    )
    assert document["selected_target_id"] == target_id
    assert document["build_run_id"] == run_id
    assert document["release_artifact_id"] == artifact_id
    assert document["release_artifact_digest"] == artifact_digest


def test_candidate_summary_rejects_fabricated_minimal_passed_schema2() -> None:
    forged = _canonical({"schema": 2, "source_sha": SOURCE, "status": "passed"})
    with pytest.raises(PublicationGroupError):
        candidate_qualification_summary_bytes(
            forged,
            candidate_branch=BRANCH,
            source_sha=SOURCE,
            target_id="debian13-x86_64",
            build_run_id=101,
            release_artifact_id=1011,
            release_artifact_digest="sha256:" + "f" * 64,
        )


@pytest.mark.parametrize(
    "field",
    ["target_id", "build_run_id", "release_artifact_id", "release_artifact_digest"],
)
def test_candidate_summary_rejects_wrong_selected_build_field(field: str) -> None:
    physical = _physical_summary()
    values: dict[str, object] = {
        "target_id": _selected(physical)[0],
        "build_run_id": _selected(physical)[1],
        "release_artifact_id": _selected(physical)[2],
        "release_artifact_digest": _selected(physical)[3],
    }
    values[field] = (
        "ubuntu24-x86_64"
        if field == "target_id"
        else "sha256:" + "f" * 64
        if field == "release_artifact_digest"
        else 999
    )
    with pytest.raises(PublicationGroupError):
        _wrap(physical, **values)


def test_candidate_summary_reader_rejects_different_physical_bytes() -> None:
    physical = _physical_summary()
    raw = _wrap(physical)
    with pytest.raises(PublicationGroupError):
        read_candidate_qualification_summary(raw, physical + b" ")


@pytest.mark.parametrize(
    "branch", ["main", "candidate/../bad", "candidate/bad\nnext", "candidate/"]
)
def test_candidate_summary_rejects_invalid_branch(branch: str) -> None:
    with pytest.raises(PublicationGroupError):
        _wrap(_physical_summary(), candidate_branch=branch)


def test_candidate_run_identity_accepts_reviewed_branch_and_rejects_main() -> None:
    run = {
        "path": ".github/workflows/build-trial-wheel.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_branch": BRANCH,
        "repository": "example/studio",
        "id": 101,
        "run_number": 1,
        "run_attempt": 1,
        "head_sha": SOURCE,
    }
    assert (
        _run_identity(
            run,
            repository="example/studio",
            default_branch="main",
            candidate_branch=BRANCH,
            candidate_sha=SOURCE,
        )["source_sha"]
        == SOURCE
    )
    with pytest.raises(PublicationGroupError, match="candidate SHA"):
        _run_identity(
            run,
            repository="example/studio",
            default_branch="main",
            candidate_branch=BRANCH,
            candidate_sha="b" * 40,
        )
    with pytest.raises(PublicationGroupError):
        _run_identity(run, repository="example/studio", default_branch="main")
    run["head_branch"] = "main"
    assert _run_identity(
        run, repository="example/studio", default_branch="main"
    )["source_sha"] == SOURCE


def test_candidate_audit_requires_four_builds_five_results_one_branch_and_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise campaign binding while replacing only expensive artifact I/O."""
    physical = read_publication_summary(_physical_summary(), kind="audit")
    rows = {row["target_id"]: row for row in physical["targets"]}
    builds = {
        target_id: {
            "run": {
                "path": ".github/workflows/build-trial-wheel.yml",
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "head_branch": BRANCH,
                "head_sha": SOURCE,
                "repository": "example/studio",
                "id": rows[target_id]["workflow"]["run_id"],
                "run_number": rows[target_id]["workflow"]["run_number"],
                "run_attempt": 1,
            }
        }
        for target_id in sorted(target_ids())
    }
    results = {target_id: {} for target_id in builds}

    def verified_build(
        target_id: str,
        record: dict,
        *,
        repository: str,
        default_branch: str,
        candidate_branch: str | None = None,
        candidate_sha: str | None = None,
    ) -> dict:
        workflow = groups._run_identity(
            record["run"],
            repository=repository,
            default_branch=default_branch,
            candidate_branch=candidate_branch,
            candidate_sha=candidate_sha,
        )
        row = rows[target_id]
        return {
            "target_id": target_id,
            "build_id": row["build_id"],
            "source_sha": workflow["source_sha"],
            "wheel_sha256": row["wheel_sha256"],
            "zip_sha256": row["zip_sha256"],
            "kit_manifest_sha256": row["kit_manifest_sha256"],
            "workflow": workflow,
            "release_artifact": row["artifacts"]["release"],
            "kit_artifact": row["artifacts"]["kit"],
        }

    monkeypatch.setattr(groups, "_verify_target_build", verified_build)
    monkeypatch.setattr(
        groups,
        "_qualification_rows",
        lambda target_id, _expected, _result: rows[target_id]["qualification_results"],
    )
    summary = groups.audit_summary_bytes(
        builds=builds,
        qualification_results=results,
        repository="example/studio",
        default_branch="main",
        candidate_branch=BRANCH,
        candidate_sha=SOURCE,
    )
    checked = read_publication_summary(summary, kind="audit")
    assert len(checked["targets"]) == 4
    assert sum(len(row["qualification_results"]) for row in checked["targets"]) == 5
    assert {row["source_sha"] for row in checked["targets"]} == {SOURCE}

    builds["debian13-x86_64"]["run"]["head_branch"] = "main"
    with pytest.raises(PublicationGroupError, match="reviewed branch"):
        groups.audit_summary_bytes(
            builds=builds,
            qualification_results=results,
            repository="example/studio",
            default_branch="main",
            candidate_branch=BRANCH,
            candidate_sha=SOURCE,
        )
    builds["debian13-x86_64"]["run"]["head_branch"] = BRANCH
    builds["debian13-x86_64"]["run"]["head_sha"] = "b" * 40
    with pytest.raises(PublicationGroupError, match="candidate SHA"):
        groups.audit_summary_bytes(
            builds=builds,
            qualification_results=results,
            repository="example/studio",
            default_branch="main",
            candidate_branch=BRANCH,
            candidate_sha=SOURCE,
        )
    for target_id in builds:
        builds[target_id]["run"]["head_sha"] = "b" * 40
    with pytest.raises(PublicationGroupError, match="candidate SHA"):
        groups.audit_summary_bytes(
            builds=builds,
            qualification_results=results,
            repository="example/studio",
            default_branch="main",
            candidate_branch=BRANCH,
            candidate_sha=SOURCE,
        )
