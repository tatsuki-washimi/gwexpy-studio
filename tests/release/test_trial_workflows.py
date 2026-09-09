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
    assert "scripts/package_trial_release.py" in workflow
    assert "scripts/verify_trial_release.py" in workflow
    assert '--bundle "$RUNNER_TEMP/trial-bundle"' in workflow
    assert '--output "$RUNNER_TEMP/trial-release"' in workflow
    assert '--release-directory "$RUNNER_TEMP/trial-release"' in workflow
    assert workflow.count("- name: Install Qt GL runtime") == 2
    assert (
        workflow.count("sudo apt-get install --no-install-recommends -y libegl1 libgl1")
        == 2
    )
    assert workflow.index("Install Qt GL runtime") < workflow.index(
        "Create the x86_64 resolver environment"
    )
    assert workflow.rindex("Install Qt GL runtime") < workflow.index(
        "Create the aarch64 resolver environment"
    )
    assert "trial-seed-${{ github.run_id }}-a${{ github.run_attempt }}" in workflow
    assert "trial-bundle-${{ github.run_id }}-a${{ github.run_attempt }}" in workflow
    assert "needs: prepare" in workflow
    assert "needs: [qualify-x86_64, qualify-aarch64]" in workflow
    assert workflow.count("load_trial_artifact") >= 3
    assert workflow.count("git rev-parse --verify HEAD") >= 4
    upload_start = workflow.index("- name: Upload the participant release archive")
    upload_block = workflow[upload_start:]
    assert "path: ${{ runner.temp }}/trial-release/*" in upload_block
    assert "${{ runner.temp }}/trial-bundle/*" not in upload_block


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
    assert workflow.count(
        'run.get("path") != ".github/workflows/build-trial-wheel.yml"'
    ) == 2
    assert '".github/workflows/build-trial-wheel.yml@" + default_branch' not in workflow
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
    assert "verify_trial_bundle.py" not in workflow
    assert workflow.count("scripts/verify_trial_release.py") == 2
    assert workflow.count(
        '--release-directory "$RUNNER_TEMP/trial-release"'
    ) == 2
    assert workflow.count(
        "from scripts.package_trial_release import verify_trial_release_directory"
    ) == 2
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


def test_publish_trial_workflow_releases_and_rechecks_exactly_two_outer_assets() -> (
    None
):
    """Only the verified participant ZIP and its sidecar cross the release boundary."""
    workflow = _workflow("publish-trial.yml")

    release_start = workflow.index('gh release create "$TAG"')
    release_end = workflow.index("\n\n      - name:", release_start)
    release_block = workflow[release_start:release_end]
    post_publish_start = workflow.index(
        "- name: Verify the release still names the tag at P"
    )
    post_publish_block = workflow[post_publish_start:]

    assert 'output.write(f"archive_filename={archive.name}\\n")' in workflow
    assert 'output.write(f"sidecar_filename={sidecar.name}\\n")' in workflow
    assert '"$RELEASE_DIRECTORY/$ARCHIVE_FILENAME"' in release_block
    assert '"$RELEASE_DIRECTORY/$SIDECAR_FILENAME"' in release_block
    assert "constraints-ubuntu24-x86_64.txt" not in release_block
    assert "TRIAL-MANIFEST.json" not in release_block
    assert 'rest_release.get("assets")' in post_publish_block
    assert "expected_assets = {" in post_publish_block
    assert 'f"gwexpy-studio-trial-{sys.argv[6]}.zip"' in post_publish_block
    assert 'f"gwexpy-studio-trial-{sys.argv[6]}.zip.sha256"' in post_publish_block


def test_publish_trial_workflow_keeps_trial_prereleases_non_latest() -> None:
    """The release command and post-publish check preserve trial visibility."""
    workflow = _workflow("publish-trial.yml")

    release_start = workflow.index('gh release create "$TAG"')
    release_end = workflow.index("\n\n      - name:", release_start)
    release_block = workflow[release_start:release_end]
    post_publish_start = workflow.index(
        "- name: Verify the release still names the tag at P"
    )
    post_publish_block = workflow[post_publish_start:]
    validation_start = workflow.index("gh api graphql", post_publish_start)
    validation_block = workflow[validation_start:]

    assert "--latest=false" in release_block
    assert release_start < validation_start
    assert "query($owner: String!, $name: String!, $tag: String!)" in validation_block
    assert '-f owner="$owner"' in validation_block
    assert '-f name="$name"' in validation_block
    assert '-f tag="$TAG"' in validation_block
    assert "repository(owner: $owner, name: $name)" in validation_block
    assert (
        "release(tagName: $tag) { tagName isPrerelease isLatest }" in validation_block
    )
    assert "release(tagName: $tag)" in validation_block
    assert "tagName" in validation_block
    assert "isPrerelease" in validation_block
    assert "isLatest" in validation_block
    assert '"repos/$REPOSITORY/git/ref/tags/$TAG"' in post_publish_block
    assert '"repos/$REPOSITORY/releases/tags/$TAG"' in post_publish_block
    assert 'object_.get("type") != "commit"' in post_publish_block
    assert 'object_.get("sha") != sys.argv[4]' in post_publish_block
    assert 'rest_release.get("tag_name") != sys.argv[5]' in post_publish_block
    assert 'rest_release.get("prerelease") is not True' in post_publish_block
    assert "isinstance(data, dict)" in validation_block
    assert "isinstance(repository, dict)" in validation_block
    assert "isinstance(release, dict)" in validation_block
    assert 'release.get("tagName") != sys.argv[5]' in validation_block
    assert 'release.get("isPrerelease") is not True' in validation_block
    assert 'release.get("isLatest") is not False' in validation_block


def test_public_ci_installs_the_qt_gl_runtime_libraries() -> None:
    """The hosted Linux runner must load PySide6 before GUI-related tests run."""
    workflow = _workflow("ci.yml")

    assert "- name: Install Qt GL runtime" in workflow
    assert "sudo apt-get install --no-install-recommends -y libegl1 libgl1" in workflow
    assert workflow.index("Install Qt GL runtime") < workflow.index(
        "Create a Python environment"
    )


def test_trial_documents_start_from_the_two_release_assets() -> None:
    """Participant documentation begins at the shared prerelease, not source."""
    english_readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    japanese_readme = (REPOSITORY_ROOT / "README.ja.md").read_text(
        encoding="utf-8"
    )
    english_quick_start = (REPOSITORY_ROOT / "docs" / "Quick-Start.md").read_text(
        encoding="utf-8"
    )
    japanese_quick_start = (
        REPOSITORY_ROOT / "docs" / "Quick-Start.ja.md"
    ).read_text(encoding="utf-8")

    assert "being prepared" not in english_readme
    assert "There is no public trial wheel" not in english_readme
    assert "準備中" not in japanese_readme
    assert "public trial wheelもPyPI releaseもない" not in japanese_readme
    for document in (english_readme, japanese_readme):
        assert ".zip.sha256" in document
        assert "GitHub prerelease" in document
    for document in (english_quick_start, japanese_quick_start):
        outer = document.index('sha256sum -c "${sidecars[0]}"')
        unpack = document.index('unzip "$archive"')
        enter = document.index('cd "$bundle"')
        inner = document.index("sha256sum -c SHA256SUMS")
        assert outer < unpack < enter < inner
        assert "Ubuntu 24.04" in document
        assert "x86_64" in document
        assert "WSL2" not in document
        assert "constraints-ubuntu24-aarch64.txt" not in document


def test_trial_readiness_defines_the_archive_and_manual_approval_contract() -> None:
    """Release governance and participant assets match the initial trial decision."""
    readiness = (
        REPOSITORY_ROOT / "docs" / "release" / "0.1.0a1-trial-readiness.md"
    ).read_text(encoding="utf-8")

    assert "gwexpy-studio-trial-<build-id>.zip" in readiness
    assert "gwexpy-studio-trial-<build-id>.zip.sha256" in readiness
    assert "Feedback.ja.md" in readiness
    assert "schema 3" in readiness
    assert "no required reviewers" in readiness
    assert "default-branch deployment policy" in readiness
    assert "manual workflow dispatch" in readiness
    assert "Require at least one reviewer" not in readiness
    assert "prevention of self-review" not in readiness
