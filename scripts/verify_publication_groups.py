"""Prepare source-bound schema-2 publication and audit summaries.

This verifier is deliberately separate from the Publish workflow.  It consumes
trusted Build run metadata, the downloaded artifact wrappers, and the physical
qualification result bytes.  Release and kit identities are obtained from the
verified wrapper contents; callers cannot nominate expected hashes or Build
identities independently.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

try:  # Support imports and direct ``python scripts/...`` execution.
    from .build_qualification_kit import QualificationKitError
    from .capture_trial_resolution import ResolutionError, read_resolution_document
    from .extract_trial_artifact import (
        TrialArtifactError,
        extract_verified_trial_artifact,
    )
    from .package_trial_release import (
        TrialReleaseError,
        read_verified_trial_release,
        verify_trial_release_directory,
    )
    from .run_platform_qualification import (
        PlatformQualificationError,
        read_qualification2_result,
        read_qualification_kit,
    )
    from .trial_targets import TrialTargetError, target_ids, trial_target
except ImportError:  # pragma: no cover - direct CLI invocation.
    from build_qualification_kit import QualificationKitError  # type: ignore[no-redef]
    from capture_trial_resolution import (  # type: ignore[no-redef]
        ResolutionError,
        read_resolution_document,
    )
    from extract_trial_artifact import (  # type: ignore[no-redef]
        TrialArtifactError,
        extract_verified_trial_artifact,
    )
    from package_trial_release import (  # type: ignore[no-redef]
        TrialReleaseError,
        read_verified_trial_release,
        verify_trial_release_directory,
    )
    from run_platform_qualification import (  # type: ignore[no-redef]
        PlatformQualificationError,
        read_qualification2_result,
        read_qualification_kit,
    )
    from trial_targets import (  # type: ignore[no-redef]
        TrialTargetError,
        target_ids,
        trial_target,
    )


class PublicationGroupError(ValueError):
    """Raised when Build, artifact, release, kit, or result proof is invalid."""


_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ARTIFACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_BUILD_ID = re.compile(
    r"P-(?P<sha>[0-9a-f]{7})-(?P<date>[0-9]{8})-"
    r"r(?P<run>[1-9][0-9]*)-a(?P<attempt>[1-9][0-9]*)"
)
_WORKFLOW_PATH = ".github/workflows/build-trial-wheel.yml"
_GROUP_TARGETS = {
    "ubuntu": ("ubuntu24-x86_64",),
    "debian": ("debian13-x86_64",),
    "wsl2-mac": ("wsl2-ubuntu24", "macos15-arm64"),
}
_ALL_TARGETS = tuple(sorted(target_ids()))
_BUILD_RECORD_FIELDS = frozenset(
    {"run", "artifacts", "release_wrapper", "kit_wrapper"}
)
_SUMMARY_FIELDS = frozenset(
    {"kind", "publication_group", "schema", "source_sha", "status", "targets"}
)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _json_no_duplicates(value: str) -> object:
    """Parse descriptor JSON while rejecting duplicate object members."""
    def hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise _invalid("descriptor JSON contains duplicate keys")
            result[key] = item
        return result

    try:
        return json.loads(value, object_pairs_hook=hook)
    except json.JSONDecodeError as exc:
        raise _invalid("verifier input is not valid JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _invalid(message: str) -> PublicationGroupError:
    return PublicationGroupError(message)


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise _invalid(f"{label} must be a positive integer")
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _invalid(f"{label} is invalid")
    return value


def _source(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_SHA.fullmatch(value) is None:
        raise _invalid("source SHA is invalid")
    return value


def _build_match(value: object, source_sha: str) -> re.Match[str]:
    if not isinstance(value, str):
        raise _invalid("Build ID is invalid")
    match = _BUILD_ID.fullmatch(value)
    if match is None or match.group("sha") != source_sha[:7]:
        raise _invalid("Build ID is invalid")
    try:
        datetime.strptime(match.group("date"), "%Y%m%d")
    except ValueError as exc:
        raise _invalid("Build ID date is invalid") from exc
    return match


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{label} is invalid")
    return value


def _read_bytes(value: object, label: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, Path):
        try:
            return value.read_bytes()
        except OSError as exc:
            raise _invalid(f"{label} cannot be read") from exc
    raise _invalid(f"{label} must be bytes or a Path")


def _repository(run: Mapping[str, object]) -> str:
    value = run.get("repository")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("full_name"), str):
        return cast(str, value["full_name"])
    raise _invalid("Build repository is invalid")


def _run_identity(
    run: Mapping[str, object], *, repository: str, default_branch: str
) -> dict[str, object]:
    """Validate the immutable GitHub Build run facts used by the summary."""
    if run.get("path") != _WORKFLOW_PATH:
        raise _invalid("Build workflow path is invalid")
    if run.get("event") != "workflow_dispatch":
        raise _invalid("Build was not manually dispatched")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise _invalid("Build workflow did not complete successfully")
    if run.get("head_branch") != default_branch:
        raise _invalid("Build did not run on the default branch")
    if _repository(run) != repository:
        raise _invalid("Build repository does not match trusted repository")
    run_id = _positive_int(run.get("id"), "Build run ID")
    run_number = _positive_int(run.get("run_number"), "Build run number")
    run_attempt = _positive_int(run.get("run_attempt"), "Build run attempt")
    source_sha = _source(run.get("head_sha"))
    return {
        "repository": repository,
        "run_attempt": run_attempt,
        "run_id": run_id,
        "run_number": run_number,
        "source_sha": source_sha,
        "workflow_path": _WORKFLOW_PATH,
    }


def _artifact_records(value: object) -> list[Mapping[str, object]]:
    """Flatten either API artifacts or the workflow's paginated JSON pages."""
    if not isinstance(value, list):
        raise _invalid("Build artifact list is invalid")
    if all(isinstance(item, Mapping) and "artifacts" in item for item in value):
        pages = value
        artifacts: list[Mapping[str, object]] = []
        for page in pages:
            page_values = page["artifacts"]
            if not isinstance(page_values, list):
                raise _invalid("Build artifact page is invalid")
            artifacts.extend(_artifact_records(page_values))
        return artifacts
    if not all(isinstance(item, Mapping) for item in value):
        raise _invalid("Build artifact list is invalid")
    return [cast(Mapping[str, object], item) for item in value]


def _selected_artifact(
    artifacts: Sequence[Mapping[str, object]], *, name: str, label: str
) -> Mapping[str, object]:
    matches = [item for item in artifacts if item.get("name") == name]
    if len(matches) != 1:
        raise _invalid(f"{label} artifact is missing or duplicated")
    item = matches[0]
    _positive_int(item.get("id"), f"{label} artifact ID")
    digest = item.get("digest")
    if not isinstance(digest, str) or _ARTIFACT_DIGEST.fullmatch(digest) is None:
        raise _invalid(f"{label} artifact digest is invalid")
    if type(item.get("expired")) is not bool or item.get("expired") is not False:
        raise _invalid(f"{label} artifact is expired or malformed")
    return item


def _reject_extra_trial_artifacts(
    artifacts: Sequence[Mapping[str, object]],
    *,
    target_id: str,
    run_id: int,
    run_attempt: int,
) -> None:
    """Reject another target's release or kit from the same proof input."""
    expected = {
        f"trial-bundle-{target_id}-{run_id}-a{run_attempt}",
        f"trial-qualification-kit-{target_id}-{run_id}-a{run_attempt}",
    }
    current_attempt = re.compile(
        rf"trial-(?:bundle|qualification-kit)-.+-{run_id}-a{run_attempt}"
    )
    extra = {
        name
        for item in artifacts
        if isinstance((name := item.get("name")), str)
        and current_attempt.fullmatch(name)
        and name not in expected
    }
    if extra:
        raise _invalid("Build artifact list contains an extra trial artifact")


def _verify_resolution4(
    archive_name: str, archive_bytes: bytes, target_id: str
) -> None:
    """Require every new publication target architecture to carry schema 4."""
    target = trial_target(target_id)
    root = archive_name.removesuffix(".zip") + "/"
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = {
                info.filename.removeprefix(root): archive.read(info)
                for info in archive.infolist()
            }
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise _invalid("verified release archive cannot be inspected") from exc
    for architecture in target.architectures:
        filename = target.resolution_filename(architecture)
        raw = members.get(filename)
        if raw is None:
            raise _invalid("release resolution evidence is missing")
        try:
            document = read_resolution_document(raw)
        except ResolutionError as exc:
            raise _invalid("release resolution evidence is invalid") from exc
        if (
            document.get("schema") != 4
            or document.get("target_id") != target_id
            or document.get("architecture") != architecture
        ):
            raise _invalid("new publication requires schema-4 resolution evidence")


def _strict_kit_files(kit_payload: Mapping[str, object], kit_directory: Path) -> None:
    manifest = _mapping(kit_payload.get("manifest"), "kit manifest")
    archive = _mapping(manifest.get("archive"), "kit archive record")
    sidecar = _mapping(manifest.get("sidecar"), "kit sidecar record")
    runner = _mapping(manifest.get("runner"), "kit runner record")
    scripts = manifest.get("scripts")
    if not isinstance(scripts, list):
        raise _invalid("kit script records are invalid")
    filenames = {"QUALIFICATION-KIT.json", "SOURCE-MANIFEST.json"}
    for record, label in (
        (archive, "kit archive"),
        (sidecar, "kit sidecar"),
        (runner, "kit runner"),
    ):
        filename = record.get("filename")
        if not isinstance(filename, str):
            raise _invalid(f"{label} filename is invalid")
        filenames.add(filename)
    for record in scripts:
        script = _mapping(record, "kit script record")
        filename = script.get("filename")
        if not isinstance(filename, str):
            raise _invalid("kit script filename is invalid")
        filenames.add(filename)
    actual = {item.name for item in kit_directory.iterdir()}
    if actual != filenames:
        raise _invalid("trusted kit artifact contains extra or missing files")


def _verify_target_build(
    target_id: str,
    record: Mapping[str, object],
    *,
    repository: str,
    default_branch: str,
) -> dict[str, object]:
    if set(record) != _BUILD_RECORD_FIELDS:
        raise _invalid("Build input fields are invalid")
    try:
        trial_target(target_id)
    except TrialTargetError as exc:
        raise _invalid("Build target is invalid") from exc
    run = _mapping(record.get("run"), "Build run")
    workflow = _run_identity(run, repository=repository, default_branch=default_branch)
    run_id = cast(int, workflow["run_id"])
    run_attempt = cast(int, workflow["run_attempt"])
    release_name = f"trial-bundle-{target_id}-{run_id}-a{run_attempt}"
    kit_name = f"trial-qualification-kit-{target_id}-{run_id}-a{run_attempt}"
    artifacts = _artifact_records(record.get("artifacts"))
    for label in ("release_wrapper", "kit_wrapper"):
        if not isinstance(record.get(label), (str, Path)):
            raise _invalid(f"{label} is invalid")
    _reject_extra_trial_artifacts(
        artifacts,
        target_id=target_id,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    release_artifact = _selected_artifact(
        artifacts, name=release_name, label="release"
    )
    kit_artifact = _selected_artifact(
        artifacts, name=kit_name, label="qualification kit"
    )

    temporary = Path(tempfile.mkdtemp(prefix="gwexpy-publication-proof-"))
    try:
        release_directory = temporary / "release"
        kit_directory = temporary / "kit"
        release_digest = _artifact_digest(release_artifact, "release")
        kit_digest = _artifact_digest(kit_artifact, "qualification kit")
        try:
            extract_verified_trial_artifact(
                archive=Path(cast(Path, record["release_wrapper"])),
                output_directory=release_directory,
                expected_digest=release_digest,
            )
            extract_verified_trial_artifact(
                archive=Path(cast(Path, record["kit_wrapper"])),
                output_directory=kit_directory,
                expected_digest=kit_digest,
            )
            verify_trial_release_directory(release_directory)
            archive = next(release_directory.glob("*.zip"))
            sidecar = release_directory / f"{archive.name}.sha256"
            manifest, archive_bytes = read_verified_trial_release(archive, sidecar)
            kit_payload = read_qualification_kit(kit_directory)
        except (
            OSError,
            StopIteration,
            TrialArtifactError,
            TrialReleaseError,
            QualificationKitError,
            PlatformQualificationError,
        ) as exc:
            raise _invalid(
                f"trusted Build artifact verification failed: {exc}"
            ) from exc
        _strict_kit_files(kit_payload, kit_directory)
        _verify_resolution4(archive.name, archive_bytes, target_id)
        release_target = _mapping(manifest.get("target"), "release target")
        if release_target.get("id") != target_id:
            raise _invalid("release target does not match Build target")
        build = _mapping(manifest.get("build"), "release build")
        wheel = _mapping(manifest.get("wheel"), "release wheel")
        source_sha = _source(build.get("source_sha"))
        build_id = build.get("id")
        if not isinstance(build_id, str):
            raise _invalid("release Build ID is invalid")
        build_match = _build_match(build_id, source_sha)
        if (
            int(build_match.group("run")) != workflow["run_number"]
            or int(build_match.group("attempt")) != workflow["run_attempt"]
        ):
            raise _invalid("release Build ID does not match trusted Build")
        manifest_workflow = _mapping(build.get("workflow"), "release Build workflow")
        for key in (
            "repository",
            "workflow_path",
            "run_id",
            "run_number",
            "run_attempt",
        ):
            if manifest_workflow.get(key) != workflow[key]:
                raise _invalid(
                    "release Build workflow does not match trusted metadata"
                )
        if source_sha != workflow["source_sha"]:
            raise _invalid("release source does not match trusted Build")
        kit_manifest = _mapping(kit_payload.get("manifest"), "kit manifest")
        if (
            kit_manifest.get("target_id") != target_id
            or kit_manifest.get("build_id") != build_id
            or kit_manifest.get("source_sha") != source_sha
            or kit_payload.get("archive_bytes") != archive_bytes
        ):
            raise _invalid("qualification kit is not bound to the verified release")
        wheel_sha = _sha(wheel.get("sha256"), "release wheel SHA-256")
        zip_sha = _sha256(archive_bytes)
        kit_manifest_sha = _sha256(cast(bytes, kit_payload["manifest_bytes"]))
        return {
            "target_id": target_id,
            "build_id": build_id,
            "source_sha": source_sha,
            "wheel_sha256": wheel_sha,
            "zip_sha256": zip_sha,
            "kit_manifest_sha256": kit_manifest_sha,
            "workflow": workflow,
            "release_artifact": {
                "digest": _artifact_digest(release_artifact, "release"),
                "id": cast(int, release_artifact["id"]),
                "name": release_name,
            },
            "kit_artifact": {
                "digest": _artifact_digest(kit_artifact, "qualification kit"),
                "id": cast(int, kit_artifact["id"]),
                "name": kit_name,
            },
        }
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _artifact_digest(record: Mapping[str, object], label: str) -> str:
    value = record.get("digest")
    if not isinstance(value, str) or _ARTIFACT_DIGEST.fullmatch(value) is None:
        raise _invalid(f"{label} artifact digest is invalid")
    return value


def _qualification_rows(
    target_id: str,
    expected: Mapping[str, object],
    value: object,
) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        raise _invalid("qualification results for target are invalid")
    target = trial_target(target_id)
    if set(value) != set(target.architectures):
        raise _invalid("qualification result architecture set is incomplete or extra")
    rows: list[dict[str, str]] = []
    for architecture in sorted(target.architectures):
        raw = _read_bytes(value[architecture], "qualification result")
        identity = {
            "target_id": target_id,
            "architecture": architecture,
            "build_id": expected["build_id"],
            "source_sha": expected["source_sha"],
            "wheel_sha256": expected["wheel_sha256"],
            "zip_sha256": expected["zip_sha256"],
            "kit_manifest_sha256": expected["kit_manifest_sha256"],
        }
        try:
            read_qualification2_result(
                raw, expected_identity=identity, require_passed=True
            )
        except (ValueError, PlatformQualificationError) as exc:
            raise _invalid(
                "qualification result identity or status is invalid"
            ) from exc
        rows.append({"architecture": architecture, "sha256": _sha256(raw)})
    return rows


def _summary(
    *,
    kind: str,
    publication_group: str | None,
    targets: list[dict[str, object]],
    source_sha: str,
) -> bytes:
    return _canonical_json(
        {
            "kind": kind,
            "publication_group": publication_group,
            "schema": 2,
            "source_sha": source_sha,
            "status": "passed",
            "targets": targets,
        }
    )


def _build_summary(
    *,
    targets: Sequence[str],
    builds: Mapping[str, object],
    qualification_results: Mapping[str, object],
    repository: str,
    default_branch: str,
    kind: str,
    publication_group: str | None,
) -> bytes:
    if set(builds) != set(targets) or set(qualification_results) != set(targets):
        raise _invalid("Build or qualification target set is incomplete or extra")
    expected_builds: dict[str, dict[str, object]] = {}
    source_shas: set[str] = set()
    for target_id in targets:
        record = _mapping(builds[target_id], "Build input")
        expected = _verify_target_build(
            target_id,
            record,
            repository=repository,
            default_branch=default_branch,
        )
        expected_builds[target_id] = expected
        source_shas.add(cast(str, expected["source_sha"]))
    if len(source_shas) != 1:
        raise _invalid("Builds do not share one source SHA")
    source_sha = next(iter(source_shas))
    rows: list[dict[str, object]] = []
    for target_id in sorted(targets):
        expected = expected_builds[target_id]
        result_rows = _qualification_rows(
            target_id, expected, qualification_results[target_id]
        )
        rows.append(
            {
                "artifacts": {
                    "kit": expected["kit_artifact"],
                    "release": expected["release_artifact"],
                },
                "build_id": expected["build_id"],
                "kit_manifest_sha256": expected["kit_manifest_sha256"],
                "qualification_results": result_rows,
                "source_sha": expected["source_sha"],
                "target_id": target_id,
                "wheel_sha256": expected["wheel_sha256"],
                "workflow": expected["workflow"],
                "zip_sha256": expected["zip_sha256"],
            }
        )
    return _summary(
        kind=kind,
        publication_group=publication_group,
        targets=rows,
        source_sha=source_sha,
    )


def publication_group_summary_bytes(
    *,
    group_id: str,
    builds: Mapping[str, object],
    qualification_results: Mapping[str, object],
    repository: str,
    default_branch: str,
) -> bytes:
    """Return one exact schema-2 publication-group summary."""
    if group_id not in _GROUP_TARGETS:
        raise _invalid("publication group is invalid")
    if not isinstance(repository, str) or not repository:
        raise _invalid("trusted repository is invalid")
    if not isinstance(default_branch, str) or not default_branch:
        raise _invalid("default branch is invalid")
    return _build_summary(
        targets=_GROUP_TARGETS[group_id],
        builds=builds,
        qualification_results=qualification_results,
        repository=repository,
        default_branch=default_branch,
        kind="publication-group",
        publication_group=group_id,
    )


def audit_summary_bytes(
    *,
    builds: Mapping[str, object],
    qualification_results: Mapping[str, object],
    repository: str,
    default_branch: str,
) -> bytes:
    """Return the distinct all-five-configuration audit summary."""
    return _build_summary(
        targets=_ALL_TARGETS,
        builds=builds,
        qualification_results=qualification_results,
        repository=repository,
        default_branch=default_branch,
        kind="audit",
        publication_group=None,
    )


def read_publication_summary(
    raw: bytes, *, kind: str | None = None
) -> dict[str, object]:
    """Read a canonical schema-2 summary without accepting legacy summaries."""
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid("publication summary is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != _SUMMARY_FIELDS:
        raise _invalid("publication summary fields are invalid")
    if _canonical_json(document) != raw or document.get("schema") != 2:
        raise _invalid("publication summary is not canonical schema 2 JSON")
    summary_kind = document.get("kind")
    if summary_kind not in {"publication-group", "audit"} or (
        kind is not None and summary_kind != kind
    ):
        raise _invalid("publication summary kind is invalid")
    group = document.get("publication_group")
    if summary_kind == "audit":
        if group is not None:
            raise _invalid("audit summary cannot be a publication group")
        targets = _ALL_TARGETS
    else:
        if group not in _GROUP_TARGETS:
            raise _invalid("publication group summary group is invalid")
        targets = _GROUP_TARGETS[cast(str, group)]
    source_sha = _source(document.get("source_sha"))
    if document.get("status") != "passed" or not isinstance(
        document.get("targets"), list
    ):
        raise _invalid("publication summary status or targets are invalid")
    rows = cast(list[object], document["targets"])
    target_set = {
        row.get("target_id") for row in rows if isinstance(row, Mapping)
    }
    if (
        target_set != set(targets)
        or len(rows) != len(targets)
        or [row.get("target_id") for row in rows if isinstance(row, Mapping)]
        != sorted(targets)
    ):
        raise _invalid("publication summary target set is invalid")
    for row in rows:
        if not isinstance(row, Mapping):
            raise _invalid("publication summary target record is invalid")
        if set(row) != {
            "artifacts",
            "build_id",
            "kit_manifest_sha256",
            "qualification_results",
            "source_sha",
            "target_id",
            "wheel_sha256",
            "workflow",
            "zip_sha256",
        }:
            raise _invalid("publication summary target fields are invalid")
        target_id = row.get("target_id")
        if not isinstance(target_id, str) or row.get("source_sha") != source_sha:
            raise _invalid("publication summary target source is invalid")
        try:
            target = trial_target(target_id)
        except TrialTargetError as exc:
            raise _invalid("publication summary target is invalid") from exc
        build_id = row.get("build_id")
        if not isinstance(build_id, str):
            raise _invalid("publication summary Build ID is invalid")
        match = _build_match(build_id, source_sha)
        for label in ("wheel_sha256", "zip_sha256", "kit_manifest_sha256"):
            _sha(row.get(label), f"publication summary {label}")
        workflow = row.get("workflow")
        if not isinstance(workflow, Mapping) or set(workflow) != {
            "repository",
            "run_attempt",
            "run_id",
            "run_number",
            "source_sha",
            "workflow_path",
        }:
            raise _invalid("publication summary workflow identity is invalid")
        repository = workflow.get("repository")
        if not isinstance(repository, str) or not repository:
            raise _invalid("publication summary repository is invalid")
        if workflow.get("workflow_path") != _WORKFLOW_PATH:
            raise _invalid("publication summary workflow path is invalid")
        run_id = _positive_int(workflow.get("run_id"), "publication summary run ID")
        run_number = _positive_int(
            workflow.get("run_number"), "publication summary run number"
        )
        run_attempt = _positive_int(
            workflow.get("run_attempt"), "publication summary run attempt"
        )
        if workflow.get("source_sha") != source_sha:
            raise _invalid("publication summary workflow source is invalid")
        if (
            int(match.group("run")) != run_number
            or int(match.group("attempt")) != run_attempt
        ):
            raise _invalid("publication summary Build ID does not match workflow")
        artifacts = row.get("artifacts")
        if not isinstance(artifacts, Mapping) or set(artifacts) != {"kit", "release"}:
            raise _invalid("publication summary artifacts are invalid")
        expected_names = {
            "release": f"trial-bundle-{target_id}-{run_id}-a{run_attempt}",
            "kit": f"trial-qualification-kit-{target_id}-{run_id}-a{run_attempt}",
        }
        for label in ("release", "kit"):
            artifact = artifacts.get(label)
            if not isinstance(artifact, Mapping) or set(artifact) != {
                "digest",
                "id",
                "name",
            }:
                raise _invalid("publication summary artifact identity is invalid")
            if artifact.get("name") != expected_names[label]:
                raise _invalid("publication summary artifact name is invalid")
            _positive_int(artifact.get("id"), "publication summary artifact ID")
            digest = artifact.get("digest")
            if not isinstance(digest, str) or _ARTIFACT_DIGEST.fullmatch(
                digest
            ) is None:
                raise _invalid("publication summary artifact digest is invalid")
        result_rows = row.get("qualification_results")
        if not isinstance(result_rows, list) or len(result_rows) != len(
            target.architectures
        ):
            raise _invalid("publication summary result set is invalid")
        result_architectures = [
            item.get("architecture")
            for item in result_rows
            if isinstance(item, Mapping)
        ]
        if result_architectures != sorted(target.architectures):
            raise _invalid("publication summary result architecture set is invalid")
        for result in result_rows:
            if not isinstance(result, Mapping) or set(result) != {
                "architecture",
                "sha256",
            }:
                raise _invalid("publication summary result record is invalid")
            _sha(result.get("sha256"), "publication summary result SHA-256")
    return cast(dict[str, object], document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--publication-group", choices=tuple(_GROUP_TARGETS))
    parser.add_argument("--audit", action="store_true")
    return parser


def _write_fresh_pair(output: Path, summary: bytes) -> None:
    """Install summary and sidecar together without replacing existing files."""
    output = Path(output)
    sidecar = output.with_suffix(output.suffix + ".sha256")
    if (
        output.exists()
        or output.is_symlink()
        or sidecar.exists()
        or sidecar.is_symlink()
    ):
        raise _invalid("summary output and sidecar must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    installed: list[Path] = []
    try:
        staged_output = stage / output.name
        staged_sidecar = stage / sidecar.name
        staged_output.write_bytes(summary)
        staged_sidecar.write_text(
            f"{_sha256(summary)}  {output.name}\n", encoding="ascii"
        )
        try:
            import os

            os.link(staged_output, output)
            installed.append(output)
            os.link(staged_sidecar, sidecar)
            installed.append(sidecar)
        except OSError:
            for path in installed:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate one descriptor and write its canonical summary and sidecar."""
    args = _parser().parse_args(argv)
    if bool(args.publication_group) == bool(args.audit):
        print("choose exactly one of --publication-group or --audit")
        return 2
    try:
        descriptor = _json_no_duplicates(args.input.read_text(encoding="utf-8"))
        if not isinstance(descriptor, Mapping):
            raise _invalid("verifier input is invalid")
        if set(descriptor) != {
            "builds",
            "default_branch",
            "qualification_results",
            "repository",
        }:
            raise _invalid("verifier input fields are invalid")
        repository = descriptor.get("repository")
        default_branch = descriptor.get("default_branch")
        builds = descriptor.get("builds")
        result_paths = descriptor.get("qualification_results")
        if not isinstance(result_paths, Mapping) or not isinstance(builds, Mapping):
            raise _invalid("verifier input sections are invalid")
        results: dict[str, dict[str, Path]] = {}
        for target_id, target_results in result_paths.items():
            if not isinstance(target_id, str):
                raise _invalid("qualification result target key is invalid")
            if not isinstance(target_results, Mapping):
                raise _invalid("qualification result paths are invalid")
            target_result_paths: dict[str, Path] = {}
            for architecture, path in target_results.items():
                if not isinstance(architecture, str) or not isinstance(path, str):
                    raise _invalid("qualification result path entry is invalid")
                target_result_paths[architecture] = Path(path)
            results[target_id] = target_result_paths
        if not isinstance(repository, str) or not isinstance(default_branch, str):
            raise _invalid("trusted repository or branch is invalid")
        if args.audit:
            summary = audit_summary_bytes(
                builds=builds,
                qualification_results=results,
                repository=repository,
                default_branch=default_branch,
            )
        else:
            summary = publication_group_summary_bytes(
                group_id=args.publication_group,
                builds=builds,
                qualification_results=results,
                repository=repository,
                default_branch=default_branch,
            )
        _write_fresh_pair(args.output, summary)
    except (OSError, UnicodeError, PublicationGroupError) as exc:
        print(f"verify_publication_groups: error: {exc}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI boundary.
    raise SystemExit(main())
