"""Static contracts for the separated M2 build and publish workflows."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT = "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
DOWNLOAD = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
UPLOAD = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _workflow(name: str) -> str:
    return (REPOSITORY_ROOT / ".github" / "workflows" / name).read_text(
        encoding="utf-8"
    )


def test_build_trial_workflow_builds_one_seed_and_qualifies_both_architectures() -> (
    None
):
    """A read-only build binds P to one reused wheel before bundle assembly."""
    workflow = _workflow("build-trial-wheel.yml")

    assert "workflow_dispatch:" in workflow
    assert "source_sha:" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert (
        "permissions:\n  contents: read\n\nenv:\n"
        '  PYTHONDONTWRITEBYTECODE: "1"\n\nconcurrency:'
    ) in workflow
    assert "contents: write" not in workflow
    assert "actions: write" not in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "timeout-minutes:" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "runs-on: ubuntu-24.04-arm" in workflow
    assert CHECKOUT in workflow
    assert DOWNLOAD in workflow
    assert UPLOAD in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "GITHUB_SHA" in workflow
    assert "inputs.source_sha" in workflow
    assert "github.run_number" in workflow
    assert "github.run_attempt" in workflow
    assert "scripts/build_trial_wheel.py" in workflow
    assert "scripts/capture_trial_resolution.py" in workflow
    assert "scripts/assemble_trial_bundle.py" in workflow
    assert "scripts/verify_trial_bundle.py" in workflow
    assert "trial-seed-${{ github.run_id }}-a${{ github.run_attempt }}" in workflow
    assert "trial-bundle-${{ github.run_id }}-a${{ github.run_attempt }}" in workflow
    assert "needs: prepare" in workflow
    assert "needs: [qualify-x86_64, qualify-aarch64]" in workflow
    assert workflow.count("load_trial_artifact") >= 3
    assert workflow.count("git rev-parse --verify HEAD") >= 4


def test_publish_trial_workflow_rechecks_an_explicit_build_after_approval() -> None:
    """Only a protected, serialized job can turn a verified bundle into a tag."""
    workflow = _workflow("publish-trial.yml")

    assert "workflow_dispatch:" in workflow
    assert "build_run_id:" in workflow
    assert "permissions:\n  actions: read\n  contents: read" in workflow
    assert (
        "permissions:\n  actions: read\n  contents: read\n\nenv:\n"
        '  PYTHONDONTWRITEBYTECODE: "1"\n\nconcurrency:'
    ) in workflow
    assert (
        "concurrency:\n  group: trial-publish\n  cancel-in-progress: false" in workflow
    )
    assert "preflight:" in workflow
    assert "publish:" in workflow
    assert "needs: preflight" in workflow
    assert "environment: trial-publish" in workflow
    assert "actions: read\n      contents: write" in workflow
    assert CHECKOUT in workflow
    assert DOWNLOAD not in workflow
    assert "build-trial-wheel.yml" in workflow
    assert '".github/workflows/build-trial-wheel.yml@" + default_branch' in workflow
    assert "conclusion" in workflow
    assert "head_sha" in workflow
    assert "verify_default_head" in workflow
    assert workflow.count("verify_default_head") >= 3
    assert workflow.count("verify_selected_build") >= 2
    assert "actions/runs/$BUILD_RUN_ID/artifacts" in workflow
    assert workflow.count('"repos/$REPOSITORY/actions/artifacts/$ARTIFACT_ID/zip"') == 2
    assert workflow.count("scripts/extract_trial_artifact.py") == 2
    assert workflow.count('--expected-digest "$ARTIFACT_DIGEST"') == 2
    assert "git/matching-refs/tags/" in workflow
    assert "verify_trial_bundle.py" in workflow
    assert "trial-0.1.0a1-p." in workflow
    assert "v0.1.0a1" in workflow
    assert "gh release create" in workflow
    assert "--prerelease" in workflow
    assert "--verify-tag" in workflow
    assert "refs/tags/" in workflow
    assert "--paginate --slurp" in workflow
    assert "release pages are invalid" in workflow
    assert workflow.index("final-default-head.json") < workflow.index(
        '"repos/$REPOSITORY/git/refs"'
    )
    assert "target-commitish" not in workflow
