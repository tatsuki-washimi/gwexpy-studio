"""Static contracts for the separated M2 build and publish workflows."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT = "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"
DOWNLOAD = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
UPLOAD = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"


def _workflow(name: str) -> str:
    return (REPOSITORY_ROOT / ".github" / "workflows" / name).read_text(
        encoding="utf-8"
    )


def _quick_start_download_block(name: str) -> str:
    document = (REPOSITORY_ROOT / "docs" / name).read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)\n```", document, flags=re.DOTALL)
    matching = [block for block in blocks if "sha256sum -c SHA256SUMS" in block]
    assert len(matching) == 1
    return matching[0]


_TRIAL_GUIDES = {
    "ubuntu24-x86_64": "ubuntu",
    "debian13-x86_64": "debian",
    "wsl2-ubuntu24": "wsl2",
    "macos15-arm64": "macos",
}


def _trial_guide_documents() -> dict[str, tuple[str, ...]]:
    return {
        target: tuple(
            (REPOSITORY_ROOT / "docs" / "trial" / directory / name).read_text(
                encoding="utf-8"
            )
            for name in ("Quick-Start.md", "Quick-Start.ja.md")
        )
        for target, directory in _TRIAL_GUIDES.items()
    }


def _trial_install_block(document: str) -> str:
    blocks = re.findall(r"```bash\n(.*?)\n```", document, flags=re.DOTALL)
    matching = [block for block in blocks if "pip install --only-binary=:all:" in block]
    assert len(matching) == 1
    return matching[0]


def _write_fake_unzip(tmp_path: Path, body: str) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    unzip = fake_bin / "unzip"
    unzip.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    unzip.chmod(0o755)
    return fake_bin


def _run_download_block(
    tmp_path: Path, block: str, fake_bin: Path, **extra_environment: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        **extra_environment,
    }
    return subprocess.run(
        ["bash", "-c", block],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("name", ["Quick-Start.md", "Quick-Start.ja.md"])
def test_quick_start_stops_before_unzip_on_outer_checksum_failure(
    tmp_path: Path, name: str
) -> None:
    """A mismatched sidecar must stop before the participant ZIP is opened."""
    archive = tmp_path / "gwexpy-studio-trial-test.zip"
    archive.write_bytes(b"trial archive\n")
    (tmp_path / f"{archive.name}.sha256").write_text(
        f"{'0' * 64}  {archive.name}\n", encoding="ascii"
    )
    marker = tmp_path / "unzip-reached"
    fake_bin = _write_fake_unzip(tmp_path, ': > "$UNZIP_MARKER"')
    block = _quick_start_download_block(name)

    completed = _run_download_block(tmp_path, block, fake_bin, UNZIP_MARKER=str(marker))

    assert completed.returncode != 0
    assert not marker.exists()
    assert block.splitlines()[0] == "set -euo pipefail"


@pytest.mark.parametrize("name", ["Quick-Start.md", "Quick-Start.ja.md"])
@pytest.mark.parametrize(
    "unzip_body",
    [
        ":",
        (
            'bundle="${1%.zip}"\n'
            'mkdir "$bundle"\n'
            'printf "%064d  missing-file\\n" 0 > "$bundle/SHA256SUMS"'
        ),
    ],
    ids=["missing-top-level-directory", "bad-internal-checksum"],
)
def test_quick_start_stops_on_cd_or_internal_checksum_failure(
    tmp_path: Path, name: str, unzip_body: str
) -> None:
    """Stop after entering or checking the participant bundle fails."""
    archive = tmp_path / "gwexpy-studio-trial-test.zip"
    content = b"trial archive\n"
    archive.write_bytes(content)
    (tmp_path / f"{archive.name}.sha256").write_text(
        f"{hashlib.sha256(content).hexdigest()}  {archive.name}\n",
        encoding="ascii",
    )
    marker = tmp_path / "continued-after-failure"
    fake_bin = _write_fake_unzip(tmp_path, unzip_body)
    block = _quick_start_download_block(name)
    block += '\nprintf "continued\\n" > "$AFTER_FAILURE_MARKER"'

    completed = _run_download_block(
        tmp_path, block, fake_bin, AFTER_FAILURE_MARKER=str(marker)
    )

    assert completed.returncode != 0
    assert not marker.exists()


def _workflow_document(name: str) -> dict[str, object]:
    document = yaml.load(_workflow(name), Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document


def _all_uses(value: object) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "uses":
                assert isinstance(nested, str)
                references.append(nested)
            else:
                references.extend(_all_uses(nested))
    elif isinstance(value, list):
        for nested in value:
            references.extend(_all_uses(nested))
    return references


@pytest.mark.parametrize("name", ["build-trial-wheel.yml", "publish-trial.yml"])
def test_trial_workflows_can_only_be_dispatched_manually(name: str) -> None:
    """Build and publish must not acquire push, pull-request, or timer triggers."""
    document = _workflow_document(name)

    triggers = document.get("on")
    assert isinstance(triggers, dict)
    assert set(triggers) == {"workflow_dispatch"}


@pytest.mark.parametrize("name", ["build-trial-wheel.yml", "publish-trial.yml"])
def test_every_trial_workflow_action_is_pinned_to_a_commit(name: str) -> None:
    """Every current and future action reference must use one immutable SHA."""
    references = _all_uses(_workflow_document(name))

    assert references
    for reference in references:
        action, separator, revision = reference.rpartition("@")
        assert action and separator == "@", reference
        assert re.fullmatch(r"[0-9a-f]{40}", revision), reference


def test_publish_workflow_does_not_claim_an_environment_review_pause() -> None:
    """The environment has no reviewers; authorization occurs before dispatch."""
    workflow = _workflow("publish-trial.yml").casefold()
    stale_phrases = (
        "before " + "approval",
        "pre-" + "approval",
        "post-" + "approval",
        "after " + "approval",
    )

    for phrase in stale_phrases:
        assert phrase not in workflow


def test_build_trial_workflow_routes_four_targets_and_five_capture_configurations() -> (
    None
):
    """A read-only build binds P to one reused wheel before bundle assembly."""
    workflow = _workflow("build-trial-wheel.yml")

    assert "workflow_dispatch:" in workflow
    assert "source_sha:" in workflow
    assert "trial_target:" in workflow
    assert "wsl2-ubuntu24" in workflow
    assert "macos15-arm64" in workflow
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
    assert "runner: ubuntu-24.04-arm" in workflow
    assert "runs-on: macos-15" in workflow
    assert "macos-latest" not in workflow
    assert CHECKOUT in workflow
    assert DOWNLOAD in workflow
    assert UPLOAD in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "GITHUB_SHA" in workflow
    assert "inputs.source_sha" in workflow
    assert "github.run_number" in workflow
    assert "github.run_attempt" in workflow
    assert "scripts/build_trial_wheel.py" in workflow
    assert workflow.count("scripts/verify_trial_seed.py") == 7
    for target in (
        "ubuntu24-x86_64",
        "debian13-x86_64",
        "wsl2-ubuntu24",
        "macos15-arm64",
    ):
        assert target in workflow
    assert "if: inputs.trial_target == 'ubuntu24-x86_64'" in workflow
    assert "if: inputs.trial_target == 'debian13-x86_64'" in workflow
    assert "if: inputs.trial_target == 'wsl2-ubuntu24'" in workflow
    assert "if: inputs.trial_target == 'macos15-arm64'" in workflow
    assert workflow.count("--backend conda") == 4
    assert workflow.count("--conda-executable") == 4
    assert "-m venv" not in workflow
    assert "bootstrap_trial_conda.py" in workflow
    assert "debian_container_reference" in workflow
    assert "--platform linux/amd64" in workflow
    assert ":ro\"" in workflow
    assert "runuser -u trial" in workflow
    assert "docker run --rm --interactive --platform linux/amd64" in workflow
    assert 'test "$(id -u)" -ne 0' in workflow
    assert "--checkout /work/source --output /work/output/qualification" in workflow
    assert "path: ${{ runner.temp }}/debian-output/qualification/*" in workflow
    assert "GIT_OPTIONAL_LOCKS=0" in workflow
    assert "unset QT_QPA_PLATFORM" in workflow
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
        workflow.count("sudo apt-get install --no-install-recommends -y ")
        == 2
    )
    assert workflow.index("Install Qt GL runtime") < workflow.index(
        "Bootstrap conda and qualify native Ubuntu"
    )
    assert (
        "trial-seed-${{ inputs.trial_target }}-${{ github.run_id }}"
        "-a${{ github.run_attempt }}" in workflow
    )
    assert (
        "trial-bundle-${{ inputs.trial_target }}-${{ github.run_id }}"
        "-a${{ github.run_attempt }}" in workflow
    )
    assert "needs: prepare" in workflow
    assert (
        "needs: [prepare, qualify-ubuntu, qualify-debian, qualify-linux, qualify-macos]"
        in workflow
    )
    assert workflow.count("git rev-parse --verify HEAD") >= 4
    assert "scripts/build_qualification_kit.py" in workflow
    assert "trial-qualification-kit-${{ inputs.trial_target }}" in workflow
    upload_start = workflow.index("- name: Upload the participant release archive")
    upload_block = workflow[upload_start:]
    assert "path: ${{ runner.temp }}/trial-release/*" in upload_block
    assert "${{ runner.temp }}/trial-bundle/*" not in upload_block


def test_publish_trial_workflow_rechecks_an_explicit_build_in_publish_job() -> None:
    """Only the serialized environment-bound job can publish a verified build."""
    workflow = _workflow("publish-trial.yml")

    assert "workflow_dispatch:" in workflow
    assert "build_run_id:" in workflow
    assert "qualification_summary_sha256:" in workflow
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
    assert (
        workflow.count('run.get("path") != ".github/workflows/build-trial-wheel.yml"')
        == 2
    )
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
    assert workflow.count("scripts/verify_trial_release.py") == 3
    assert workflow.count('--release-directory "$RUNNER_TEMP/trial-release"') == 2
    assert (
        workflow.count(
            "from scripts.package_trial_release import verify_trial_release_directory"
        )
        == 2
    )
    assert "trial-{target_id}-0.1.0a1-p." in workflow
    assert "QUALIFICATION_SUMMARY_SHA256" in workflow
    assert "gh release create" in workflow
    assert "--prerelease" in workflow
    assert "--verify-tag" in workflow
    assert "refs/tags/" in workflow
    assert "--paginate --slurp" in workflow
    assert "release pages are invalid" in workflow
    assert "debian13-x86_64|macos15-arm64|ubuntu24-x86_64|wsl2-ubuntu24" in workflow
    publish_recheck = workflow[workflow.index("Fetch and reverify selected build") :]
    assert 'run.get("event") != "workflow_dispatch"' in publish_recheck
    assert 'run.get("status") != "completed"' in publish_recheck
    assert 'run.get("head_branch") != os.environ["DEFAULT_BRANCH"]' in publish_recheck
    assert 'repository.get("full_name") != os.environ["REPOSITORY"]' in publish_recheck
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
        "- name: Verify tag, release state, assets, and downloaded bytes"
    )
    post_publish_block = workflow[post_publish_start:]

    assert "archive_filename={archive.name}" in workflow
    assert "sidecar_filename={sidecar.name}" in workflow
    assert '"$RELEASE_DIRECTORY/$ARCHIVE_FILENAME"' in release_block
    assert '"$RELEASE_DIRECTORY/$SIDECAR_FILENAME"' in release_block
    assert "constraints-ubuntu24-x86_64.txt" not in release_block
    assert "TRIAL-MANIFEST.json" not in release_block
    assert 'rest_release.get("assets")' in post_publish_block
    assert "expected_assets = {" in post_publish_block
    assert "{sys.argv[6], sys.argv[7]}" in post_publish_block
    assert 'gh release download "$TAG"' in post_publish_block


def test_publish_trial_workflow_keeps_trial_prereleases_non_latest() -> None:
    """The release command and post-publish check preserve trial visibility."""
    workflow = _workflow("publish-trial.yml")

    release_start = workflow.index('gh release create "$TAG"')
    release_end = workflow.index("\n\n      - name:", release_start)
    release_block = workflow[release_start:release_end]
    post_publish_start = workflow.index(
        "- name: Verify tag, release state, assets, and downloaded bytes"
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
    """The README routes participants to all four target-specific guides."""
    english_readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    japanese_readme = (REPOSITORY_ROOT / "README.ja.md").read_text(encoding="utf-8")
    assert "being prepared" not in english_readme
    assert "There is no public trial wheel" not in english_readme
    assert "準備中" not in japanese_readme
    assert "public trial wheelもPyPI releaseもない" not in japanese_readme
    for document in (english_readme, japanese_readme):
        assert "docs/trial" in document
        for target in _TRIAL_GUIDES:
            assert target in document
    for target, documents in _trial_guide_documents().items():
        for document in documents:
            assert target in document
            assert ".zip.sha256" in document
            assert "SHA256SUMS" in document


def test_trial_guide_install_commands_use_one_verified_wheel() -> None:
    """Each target guide selects one wheel and its target constraints."""
    for target, documents in _trial_guide_documents().items():
        for document in documents:
            install = _trial_install_block(document)
            assert target in install or (
                target == "wsl2-ubuntu24" and "constraints-ubuntu24-" in install
            )
            assert "test \"${#" in install and "-eq 1" in install
            assert "<build-id>" not in install
            assert "--only-binary=:all:" in install
            assert "gwexpy_studio-*.whl" in install


def test_trial_participant_commands_isolate_python_user_paths() -> None:
    """Trial installs and launches must not reuse user-site or PYTHONPATH code."""
    for documents in _trial_guide_documents().values():
        for document in documents:
            install_block = _trial_install_block(document)
            activate = document.index("conda activate", document.index("env_name="))
            env_name = re.search(r"env_name=gwexpy-studio-[a-z0-9-]+", document)
            assert env_name is not None
            disable_user_site = document.index("export PYTHONNOUSERSITE=1")
            clear_pythonpath = document.index("unset PYTHONPATH")
            install = document.index("pip install --only-binary=:all:")
            assert activate < disable_user_site < install
            assert activate < clear_pythonpath < install
            assert "PYTHONNOUSERSITE=1" in install_block or disable_user_site < install


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


def test_platform_trial_documents_match_distribution_and_human_gate() -> None:
    documents = {
        target: {
            name: (REPOSITORY_ROOT / "docs" / "trial" / directory / name).read_text(
                encoding="utf-8"
            )
            for name in ("Quick-Start.md", "Quick-Start.ja.md", "Feedback.ja.md")
        }
        for target, directory in _TRIAL_GUIDES.items()
    }

    for target, target_documents in documents.items():
        for document in target_documents.values():
            assert target in document
            assert "--only-binary=:all:" in document
            assert "Build ID" in document
        for quick_start in (
            target_documents["Quick-Start.md"],
            target_documents["Quick-Start.ja.md"],
        ):
            assert "PYTHONNOUSERSITE=1" in quick_start
            assert "unset PYTHONPATH" in quick_start
            assert "Save" in quick_start
            assert "Close" in quick_start
            assert "Open" in quick_start
    assert "constraints-ubuntu24-" in documents["wsl2-ubuntu24"]["Quick-Start.md"]
    assert "constraints-ubuntu24-x86_64.txt" in documents["ubuntu24-x86_64"][
        "Quick-Start.md"
    ]
    assert "constraints-debian13-x86_64.txt" in documents["debian13-x86_64"][
        "Quick-Start.md"
    ]
    assert "constraints-macos15-arm64.txt" in documents["macos15-arm64"][
        "Quick-Start.md"
    ]
    assert "libEGL.so.1" in documents["wsl2-ubuntu24"]["Quick-Start.md"]

    evidence = (
        REPOSITORY_ROOT / "docs" / "trial" / "Human-Trial-Evidence.md"
    ).read_text(encoding="utf-8")
    assert "N >= 3" in evidence
    assert "ceil(2N / 3)" in evidence
    assert any("WSL2" in line and "2名以上" in line for line in evidence.splitlines())
    assert any(
        ("Mac" in line or "macOS" in line) and "1名以上" in line
        for line in evidence.splitlines()
    )
    assert "Issue ID" in evidence
    assert "root cause ID" in evidence
    assert "Save → Close → Open" in evidence


def test_project_metadata_is_not_linux_only() -> None:
    metadata = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert (
        'description = "Desktop application for GWexpy scientific analysis."'
        in metadata
    )
    assert "Linux desktop application" not in metadata
