"""Candidate branch release control contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.release.test_candidate_qualification_summary import (
    BRANCH,
    SOURCE,
    _build_runs,
    _physical_summary,
    _wrap,
)

ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict:
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def test_build_and_source_identity_accept_only_dispatch_branch_head() -> None:
    for name in ("build-trial-wheel.yml", "source-identity.yml"):
        workflow = _workflow(name)
        text = str(workflow)
        assert "GITHUB_REF" in text
        assert "SOURCE_SHA" in text
        assert "candidate" in text.lower()
        assert "GITHUB_SHA" in text


def test_publish_rechecks_candidate_build_and_actual_summary_bytes() -> None:
    workflow = _workflow("publish-trial.yml")
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    for name in (
        "candidate_branch",
        "qualification_summary_base64",
        "physical_summary_base64",
        "operator_verified_summary",
    ):
        assert inputs[name]["required"] == "true"
    text = str(workflow)
    assert "candidate_branch" in text
    assert "qualification_summary" in text
    assert "physical_summary_base64" in text
    assert "operator_verified_summary" in text
    assert "sha256sum" in text or "hashlib.sha256" in text
    assert "environment" in text
    assert "head_branch" in text
    assert text.count("read_candidate_qualification_summary") >= 2
    for job in ("preflight", "publish"):
        steps = workflow["jobs"][job]["steps"]
        checks = [
            step
            for step in steps
            if "Requery every candidate Build branch" in step.get("name", "")
        ]
        assert len(checks) == 1
        assert "fetch_candidate_build_run_provenance(" in checks[0]["run"]
        assert "if" not in checks[0]
        assert checks[0]["env"]["GH_TOKEN"] == "${{ github.token }}"
    publish_steps = workflow["jobs"]["publish"]["steps"]
    selected = next(step for step in publish_steps if step.get("id") == "selected")
    assert selected["env"]["CANDIDATE_BRANCH"] == "${{ inputs.candidate_branch }}"
    assert 'not os.environ["CANDIDATE_BRANCH"]' in selected["run"]


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "CANDIDATE_BRANCH": "",
            "QUALIFICATION_SUMMARY_BASE64": "",
            "PHYSICAL_SUMMARY_BASE64": "",
            "OPERATOR_VERIFIED_SUMMARY": "false",
        },
        {"CANDIDATE_BRANCH": ""},
        {"QUALIFICATION_SUMMARY_BASE64": ""},
        {"PHYSICAL_SUMMARY_BASE64": ""},
        {"OPERATOR_VERIFIED_SUMMARY": "false"},
    ],
)
def test_publish_dispatch_requires_candidate_evidence_and_operator(
    overrides: dict[str, str],
) -> None:
    block = _workflow("publish-trial.yml")["jobs"]["preflight"]["steps"][0]["run"]
    physical = _physical_summary()
    summary = _wrap(physical)
    environment = {
        **os.environ,
        "BUILD_RUN_ID": "101",
        "CONTROL_SHA": SOURCE,
        "DEFAULT_BRANCH": "main",
        "GITHUB_REF": "refs/heads/main",
        "CANDIDATE_BRANCH": BRANCH,
        "OPERATOR_VERIFIED_SUMMARY": "true",
        "QUALIFICATION_SUMMARY_SHA256": hashlib.sha256(summary).hexdigest(),
        "QUALIFICATION_SUMMARY_BASE64": base64.b64encode(summary).decode(),
        "PHYSICAL_SUMMARY_BASE64": base64.b64encode(physical).decode(),
    }
    environment.update(overrides)
    result = subprocess.run(
        ["bash", "-c", block], env=environment, capture_output=True, text=True
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "CANDIDATE_BRANCH": "",
            "QUALIFICATION_SUMMARY_BASE64": "",
            "PHYSICAL_SUMMARY_BASE64": "",
        },
        {"OPERATOR_VERIFIED_SUMMARY": "false"},
    ],
)
def test_publish_job_recheck_rejects_missing_candidate_proof(
    overrides: dict[str, str],
) -> None:
    steps = _workflow("publish-trial.yml")["jobs"]["publish"]["steps"]
    block = next(
        step["run"]
        for step in steps
        if step["name"]
        == "Recheck operator supplied qualification summary in publish job"
    )
    physical = _physical_summary()
    summary = _wrap(physical)
    environment = {
        **os.environ,
        "CANDIDATE_BRANCH": BRANCH,
        "OPERATOR_VERIFIED_SUMMARY": "true",
        "QUALIFICATION_SUMMARY_BASE64": base64.b64encode(summary).decode(),
        "PHYSICAL_SUMMARY_BASE64": base64.b64encode(physical).decode(),
        "QUALIFICATION_SUMMARY_SHA256": hashlib.sha256(summary).hexdigest(),
        "SOURCE_SHA": SOURCE,
        "BUILD_RUN_ID": "101",
        "ARTIFACT_ID": "1011",
        "ARTIFACT_DIGEST": "sha256:" + "a" * 64,
        "TARGET_ID": "debian13-x86_64",
    }
    valid = subprocess.run(
        ["bash", "-c", block],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert valid.returncode == 0, valid.stderr
    environment.update(overrides)
    result = subprocess.run(
        ["bash", "-c", block],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_publish_dispatch_rejects_wrong_physical_bytes() -> None:
    workflow = _workflow("publish-trial.yml")
    steps = workflow["jobs"]["preflight"]["steps"]
    block = steps[0]["run"]
    physical = _physical_summary()
    summary = _wrap(physical)
    environment = {
        **os.environ,
        "BUILD_RUN_ID": "123",
        "CONTROL_SHA": SOURCE,
        "DEFAULT_BRANCH": "main",
        "GITHUB_REF": "refs/heads/main",
        "CANDIDATE_BRANCH": BRANCH,
        "OPERATOR_VERIFIED_SUMMARY": "true",
        "QUALIFICATION_SUMMARY_SHA256": hashlib.sha256(summary).hexdigest(),
        "QUALIFICATION_SUMMARY_BASE64": base64.b64encode(summary).decode(),
        "PHYSICAL_SUMMARY_BASE64": base64.b64encode(physical).decode(),
    }
    valid = subprocess.run(
        ["bash", "-c", block], env=environment, capture_output=True, text=True
    )
    assert valid.returncode == 0, valid.stderr
    environment["PHYSICAL_SUMMARY_BASE64"] = base64.b64encode(b"{}\n").decode()
    invalid = subprocess.run(
        ["bash", "-c", block], env=environment, capture_output=True, text=True
    )
    assert invalid.returncode != 0
    environment["QUALIFICATION_SUMMARY_BASE64"] = base64.b64encode(physical).decode()
    environment["PHYSICAL_SUMMARY_BASE64"] = base64.b64encode(physical).decode()
    environment["QUALIFICATION_SUMMARY_SHA256"] = hashlib.sha256(physical).hexdigest()
    schema2 = subprocess.run(
        ["bash", "-c", block], env=environment, capture_output=True, text=True
    )
    assert schema2.returncode != 0


def test_both_publish_jobs_requery_and_reject_tampered_branch(tmp_path: Path) -> None:
    physical = _physical_summary()
    summary = _wrap(physical)
    runs_path = tmp_path / "runs.json"
    runs = _build_runs(physical)
    runs_path.write_text(json.dumps(runs), encoding="utf-8")
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_RUNS'], encoding='utf-8') as stream:\n"
        "    runs = json.load(stream)\n"
        "print(json.dumps(runs[sys.argv[-1].rsplit('/', 1)[-1]]))\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    environment = {
        **os.environ,
        "PYTHONNOUSERSITE": "1",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "FAKE_RUNS": str(runs_path),
        "QUALIFICATION_SUMMARY_BASE64": base64.b64encode(summary).decode(),
        "PHYSICAL_SUMMARY_BASE64": base64.b64encode(physical).decode(),
        "REPOSITORY": "example/studio",
    }
    workflow = _workflow("publish-trial.yml")
    for job in ("preflight", "publish"):
        block = next(
            step["run"]
            for step in workflow["jobs"][job]["steps"]
            if "Requery every candidate Build branch" in step.get("name", "")
        )
        valid = subprocess.run(
            ["bash", "-c", block],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert valid.returncode == 0, valid.stderr
        runs[104]["head_branch"] = "candidate/other"
        runs_path.write_text(json.dumps(runs), encoding="utf-8")
        invalid = subprocess.run(
            ["bash", "-c", block],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert invalid.returncode != 0
        assert "reviewed branch" in invalid.stderr
        runs[104]["head_branch"] = BRANCH
        runs_path.write_text(json.dumps(runs), encoding="utf-8")
        environment["QUALIFICATION_SUMMARY_BASE64"] = ""
        missing = subprocess.run(
            ["bash", "-c", block],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert missing.returncode != 0
        environment["QUALIFICATION_SUMMARY_BASE64"] = base64.b64encode(summary).decode()


def test_publish_selected_build_rejects_forged_physical_audit(tmp_path: Path) -> None:
    steps = _workflow("publish-trial.yml")["jobs"]["preflight"]["steps"]
    block = next(step["run"] for step in steps if step.get("id") == "selected")
    run_id = 101
    artifact_id = 1011
    digest = "sha256:" + "a" * 64
    run = {
        "id": run_id,
        "path": ".github/workflows/build-trial-wheel.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "example/studio"},
        "head_branch": BRANCH,
        "head_sha": SOURCE,
        "run_number": 1,
        "run_attempt": 1,
    }
    (tmp_path / "build-run.json").write_text(json.dumps(run))
    (tmp_path / "build-artifacts.json").write_text(
        json.dumps(
            [
                {
                    "artifacts": [
                        {
                            "id": artifact_id,
                            "name": f"trial-bundle-debian13-x86_64-{run_id}-a1",
                            "digest": digest,
                            "expired": False,
                        }
                    ]
                }
            ]
        )
    )
    (tmp_path / "default-head.json").write_text(
        json.dumps({"object": {"type": "commit", "sha": SOURCE}})
    )
    physical = _physical_summary()
    summary = _wrap(physical)
    environment = {
        **os.environ,
        "PYTHONNOUSERSITE": "1",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "GITHUB_REPOSITORY": "example/studio",
        "BUILD_RUN_ID": str(run_id),
        "CONTROL_SHA": SOURCE,
        "DEFAULT_BRANCH": "main",
        "CANDIDATE_BRANCH": BRANCH,
        "QUALIFICATION_SUMMARY_BASE64": base64.b64encode(summary).decode(),
        "PHYSICAL_SUMMARY_BASE64": base64.b64encode(physical).decode(),
    }
    valid = subprocess.run(
        ["bash", "-c", block], cwd=ROOT, env=environment, capture_output=True, text=True
    )
    assert valid.returncode == 0, valid.stderr

    forged = (
        json.dumps(
            {"schema": 2, "source_sha": SOURCE, "status": "passed"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    forged_summary = json.loads(summary)
    forged_summary["qualification_summary_sha256"] = hashlib.sha256(forged).hexdigest()
    environment["QUALIFICATION_SUMMARY_BASE64"] = base64.b64encode(
        json.dumps(forged_summary, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    ).decode()
    environment["PHYSICAL_SUMMARY_BASE64"] = base64.b64encode(forged).decode()
    rejected = subprocess.run(
        ["bash", "-c", block], cwd=ROOT, env=environment, capture_output=True, text=True
    )
    assert rejected.returncode != 0
    assert "publication summary" in rejected.stderr

    run["head_branch"] = "main"
    (tmp_path / "build-run.json").write_text(json.dumps(run))
    environment["CANDIDATE_BRANCH"] = ""
    environment["QUALIFICATION_SUMMARY_BASE64"] = ""
    environment["PHYSICAL_SUMMARY_BASE64"] = ""
    bypass = subprocess.run(
        ["bash", "-c", block], cwd=ROOT, env=environment, capture_output=True, text=True
    )
    assert bypass.returncode != 0
