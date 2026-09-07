"""Build one identity-bound trial wheel from a canonical public-source stage."""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

# This script runs against P before it is exported.  Importing sibling helpers
# must not create an excluded ``__pycache__`` directory in that checkout.
sys.dont_write_bytecode = True

try:  # Support both ``python scripts/...`` and ``import scripts...``.
    from .export_release_source import export_release_source
    from .release_source_manifest import (
        ManifestEntry,
        ReleaseSourceError,
        ReleaseSourceManifest,
        ReleaseSourcePolicy,
        compare_manifests,
        load_policy_bytes,
        manifest_digest,
    )
    from .verify_public_source import scan_public_checkout
except ImportError:  # pragma: no cover - exercised by direct CLI invocation.
    from export_release_source import export_release_source  # type: ignore[no-redef]
    from release_source_manifest import (  # type: ignore[no-redef]
        ManifestEntry,
        ReleaseSourceError,
        ReleaseSourceManifest,
        ReleaseSourcePolicy,
        compare_manifests,
        load_policy_bytes,
        manifest_digest,
    )
    from verify_public_source import scan_public_checkout  # type: ignore[no-redef]


_BASE_VERSION = "0.1.0a1"
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_UTC_DATE = re.compile(r"[0-9]{8}")
_TRIAL_VERSION = re.compile(
    r"0\.1\.0a1\+trial\.p\.[0-9a-f]{7}\.[0-9]{8}\.r[1-9][0-9]*\.a[1-9][0-9]*"
)
_VERSION_ASSIGNMENT = re.compile(rb'^__version__ = "([^"\r\n]+)"$', re.MULTILINE)
_VERSION_PATH = Path("src/gwexpy_studio/_version.py")
_TRIAL_BUILD_PATH = Path("src/gwexpy_studio/assets/trial-build.json")
_CAPABILITY_PATH = Path("src/gwexpy_studio/assets/io-capabilities.json")
_ALLOWLIST_PATH = Path("packaging/release-source-allowlist.txt")
_POLICY_INPUT_PATH = Path("packaging/trial-io-capabilities.json")
_GENERATED_PATHS = (_VERSION_PATH, _TRIAL_BUILD_PATH, _CAPABILITY_PATH)
_PACKAGE_SOURCE_PREFIX = "src/gwexpy_studio/"
_PACKAGE_WHEEL_PREFIX = "gwexpy_studio/"
_SOURCE_LICENSE_PATH = "LICENSE"
_ENTRY_POINTS_BYTES = b"[gui_scripts]\ngwexpy-studio = gwexpy_studio.ui.app:main\n"
_TOP_LEVEL_BYTES = b"gwexpy_studio\n"
_PYPROJECT_PATH = Path("pyproject.toml")
_TRIAL_PROJECT_NAME = "gwexpy-studio"
_TRIAL_PROJECT_REQUIRES_PYTHON = ">=3.12,<3.13"
_TRIAL_PROJECT_DEPENDENCIES = (
    "PySide6-Essentials==6.11.2",
    "gwexpy>=0.2.0,<0.3.0",
    "gwpy>=4.0.0,<5.0.0",
    "numpy>=2.0.0,<3.0.0",
    "scipy>=1.15.0,<2.0.0",
    "astropy>=7.0.0,<9.0.0",
    "matplotlib>=3.10.0,<4.0.0",
)
_TRIAL_PROJECT_OPTIONAL_DEPENDENCIES = {
    "dev": (
        "setuptools>=68",
        "pytest",
        "pytest-cov",
        "pytest-timeout",
        "ruff",
        "mypy",
        "jsonschema",
        "packaging",
    )
}
_TRIAL_GUI_SCRIPTS = {"gwexpy-studio": "gwexpy_studio.ui.app:main"}
_TRIAL_LICENSE = {"file": "LICENSE"}
_RELEVANT_METADATA_FIELDS = frozenset(
    {
        "name",
        "version",
        "requires-python",
        "requires-dist",
        "provides-extra",
        "dynamic",
        "license-file",
    }
)
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_REQUIREMENT_SPECIFIER = re.compile(r"(===|==|!=|<=|>=|<|>|~=)([^,;\s]+)")
_SIMPLE_MARKER = re.compile(r'extra=="[A-Za-z0-9._-]+"')
_TRIAL_CAPABILITY_POLICY = {
    "schema_version": 1,
    "entries": [
        {
            "datatype": "TimeSeries",
            "format": "csv",
            "direction": "read",
            "tier": "A",
        }
    ],
}


class TrialBuildError(RuntimeError):
    """Raised when a trial build cannot prove its prescribed provenance."""


@dataclass(frozen=True)
class TrialIdentity:
    """The CI-derived identity embedded in one staged trial wheel."""

    source_sha: str
    build_id: str
    version: str


@dataclass(frozen=True)
class TrialBuildResult:
    """The stable identifiers for one successfully assembled trial wheel."""

    identity: TrialIdentity
    wheel_filename: str
    wheel_sha256: str


@dataclass(frozen=True)
class _GitTreeEntry:
    """One regular-file or directory entry pinned in P's raw Git tree."""

    relative_path: PurePosixPath
    mode: str
    object_type: str
    object_id: str


def derive_trial_identity(
    *,
    source_sha: str,
    utc_date: str,
    run: int,
    attempt: int,
) -> TrialIdentity:
    """Derive the only allowed build ID and local-version identifier."""
    if type(source_sha) is not str or _SOURCE_SHA.fullmatch(source_sha) is None:
        raise TrialBuildError("source SHA must be 40 lowercase hexadecimal characters")
    if type(utc_date) is not str or _UTC_DATE.fullmatch(utc_date) is None:
        raise TrialBuildError("UTC date must use YYYYMMDD")
    try:
        datetime.strptime(utc_date, "%Y%m%d")
    except ValueError as exc:
        raise TrialBuildError("UTC date must be a valid calendar date") from exc
    _require_positive_integer(run, "run")
    _require_positive_integer(attempt, "attempt")

    short_sha = source_sha[:7]
    return TrialIdentity(
        source_sha=source_sha,
        build_id=f"P-{short_sha}-{utc_date}-r{run}-a{attempt}",
        version=(
            f"{_BASE_VERSION}+trial.p.{short_sha}.{utc_date}.r{run}.a{attempt}"
        ),
    )


def make_version_delta(source_bytes: bytes, trial_version: str) -> bytes:
    """Replace exactly the approved base-version assignment in a staged copy."""
    if (
        type(trial_version) is not str
        or _TRIAL_VERSION.fullmatch(trial_version) is None
    ):
        raise TrialBuildError("trial version is not an allowed derived version")
    assignments = tuple(_VERSION_ASSIGNMENT.finditer(source_bytes))
    if len(assignments) != 1:
        raise TrialBuildError("source version file must contain one version assignment")
    assignment = assignments[0]
    source_version = assignment.group(1).decode("ascii", errors="strict")
    if source_version != _BASE_VERSION:
        raise TrialBuildError(
            f"source base version must be {_BASE_VERSION}, got {source_version!r}"
        )
    replacement = trial_version.encode("ascii")
    return (
        source_bytes[: assignment.start(1)]
        + replacement
        + source_bytes[assignment.end(1) :]
    )


def verify_staging_delta(
    *,
    source_manifest: ReleaseSourceManifest,
    staging_manifest: ReleaseSourceManifest,
    generated_files: Mapping[str, bytes],
) -> None:
    """Require staging to differ from M(P) only by the reviewed three files."""
    expected_paths = tuple(path.as_posix() for path in _GENERATED_PATHS)
    if tuple(generated_files) != expected_paths:
        raise TrialBuildError("generated staging files do not match the reviewed delta")

    source_entries = {entry.path: entry for entry in source_manifest.entries}
    version_entry = source_entries.get(_VERSION_PATH.as_posix())
    if version_entry is None or version_entry.file_type != "file":
        raise TrialBuildError("source manifest is missing the package version file")

    expected_entries = dict(source_entries)
    for path, content in generated_files.items():
        existing = source_entries.get(path)
        if path != _VERSION_PATH.as_posix() and existing is not None:
            raise TrialBuildError("generated trial asset already exists in source P")
        expected_entries[path] = ManifestEntry(
            path=path,
            file_type="file",
            mode=existing.mode if existing is not None else "0644",
            sha256=_sha256(content),
        )
    expected_manifest = ReleaseSourceManifest(
        tuple(
            sorted(
                expected_entries.values(), key=lambda entry: entry.path.encode("utf-8")
            )
        )
    )
    differences = compare_manifests(expected_manifest, staging_manifest)
    if differences:
        summary = "; ".join(
            f"{difference.kind}:{difference.path}" for difference in differences
        )
        raise TrialBuildError(f"unexpected staging delta: {summary}")


def build_trial_wheel(
    *,
    source_root: Path,
    output_directory: Path,
    source_sha: str,
    utc_date: str,
    run: int,
    attempt: int,
) -> TrialBuildResult:
    """Export P, inject the bounded delta, and atomically publish four assets."""
    identity = derive_trial_identity(
        source_sha=source_sha,
        utc_date=utc_date,
        run=run,
        attempt=attempt,
    )
    source = _source_root(source_root)
    _verify_source_git_identity(source, identity.source_sha)
    output = _output_directory(output_directory, source)

    with tempfile.TemporaryDirectory(
        prefix=".trial-wheel-",
        dir=_external_temporary_parent(source),
    ) as temporary_name:
        temporary = Path(temporary_name)
        git_tree = temporary / "git-tree"
        _materialize_git_tree(source, identity.source_sha, git_tree)
        policy, allowlist_bytes = _source_policy(git_tree)
        source_manifest = scan_public_checkout(git_tree, policy)
        source_entries = {entry.path: entry for entry in source_manifest.entries}
        _verify_selected_source_allowlist(source_entries, allowlist_bytes)
        _reject_generated_source_files(source_entries)
        _read_trial_project_metadata(
            git_tree / _PYPROJECT_PATH,
            source_entries,
        )
        capability_bytes = _read_trial_capability_policy(
            git_tree / _POLICY_INPUT_PATH,
            source_entries,
        )
        staging = temporary / "canonical-source"
        detached_source_manifest = temporary / "canonical-source-manifest.json"
        exported_manifest = export_release_source(
            git_tree,
            staging,
            policy,
            detached_source_manifest,
        )
        if exported_manifest != source_manifest:
            _raise_source_snapshot_mismatch(source_manifest, exported_manifest)

        generated_files = _inject_generated_files(
            staging=staging,
            source_manifest=source_manifest,
            identity=identity,
            capability_bytes=capability_bytes,
        )
        prebuild_staging_manifest = scan_public_checkout(staging, policy)
        verify_staging_delta(
            source_manifest=source_manifest,
            staging_manifest=prebuild_staging_manifest,
            generated_files=generated_files,
        )

        artifacts = temporary / "artifacts"
        artifacts.mkdir(mode=0o700)
        wheel = _build_one_wheel(staging, artifacts, identity)
        staging_manifest = scan_public_checkout(staging, policy)
        verify_staging_delta(
            source_manifest=source_manifest,
            staging_manifest=staging_manifest,
            generated_files=generated_files,
        )
        wheel_bytes = _verify_wheel(
            wheel,
            identity,
            generated_files,
            staging_manifest,
        )
        _assert_wheel_path_matches_bytes(wheel, wheel_bytes)
        wheel_sha256 = _sha256(wheel_bytes)
        trial_manifest_bytes = _trial_manifest_bytes(
            identity=identity,
            source_manifest=source_manifest,
            staging_manifest=staging_manifest,
            generated_files=generated_files,
            wheel=wheel,
            wheel_sha256=wheel_sha256,
        )
        artifact_bytes = {
            "SOURCE-MANIFEST.json": source_manifest.to_bytes(),
            "STAGING-MANIFEST.json": staging_manifest.to_bytes(),
            "TRIAL-MANIFEST.json": trial_manifest_bytes,
            wheel.name: wheel_bytes,
        }
        _verify_source_git_identity(source, identity.source_sha)
        _publish_artifacts(
            artifact_bytes,
            output,
            source,
        )

    return TrialBuildResult(
        identity=identity,
        wheel_filename=wheel.name,
        wheel_sha256=wheel_sha256,
    )


def _require_positive_integer(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise TrialBuildError(f"{label} must be a positive integer")


def _source_root(path: Path) -> Path:
    source = Path(os.path.abspath(path))
    if source.is_symlink():
        raise TrialBuildError("source root must not be a symlink")
    if not source.is_dir():
        raise TrialBuildError("source root must be an existing directory")
    return source


def _output_directory(path: Path, source: Path) -> Path:
    output = Path(os.path.abspath(path))
    if output.exists() or output.is_symlink():
        raise TrialBuildError("output directory must not already exist")
    if _is_within(output, source):
        raise TrialBuildError("output directory must be outside source P")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise TrialBuildError("output directory parent must be an existing directory")
    physical_source = source.resolve(strict=True)
    physical_parent = output.parent.resolve(strict=True)
    if _is_within(physical_parent, physical_source):
        raise TrialBuildError("output directory must be outside source P")
    return output


def _external_temporary_parent(source: Path) -> Path:
    """Select a trusted, P-external parent for the private staging hierarchy.

    CI must run beneath a hierarchy without concurrent same-UID ancestor
    replacement. ``TemporaryDirectory`` makes a private child, but POSIX cannot
    atomically prove that an intentionally hostile ancestor remains outside P.
    """
    candidate = Path(tempfile.gettempdir())
    try:
        physical_parent = candidate.resolve(strict=True)
        physical_source = source.resolve(strict=True)
    except OSError as exc:
        raise TrialBuildError("system temporary directory is unavailable") from exc
    if not physical_parent.is_dir() or _is_within(physical_parent, physical_source):
        raise TrialBuildError("system temporary directory must be outside source P")
    return physical_parent


def _materialize_git_tree(source: Path, source_sha: str, destination: Path) -> None:
    """Create a private source tree from P's raw committed Git objects only."""
    if destination.exists() or destination.is_symlink():
        raise TrialBuildError("Git tree staging path must not already exist")
    entries = _parse_git_tree_entries(
        _git_bytes(source, "ls-tree", "--full-tree", "-r", "-t", "-z", source_sha)
    )
    try:
        destination.mkdir(mode=0o700)
        _materialize_git_tree_entries(source, destination, entries)
    except OSError as exc:
        raise TrialBuildError("cannot materialize source from raw Git tree") from exc


def _parse_git_tree_entries(content: bytes) -> tuple[_GitTreeEntry, ...]:
    """Parse the NUL-delimited raw tree listing without consulting attributes."""
    if not content or not content.endswith(b"\0"):
        raise TrialBuildError("Git tree listing is malformed")
    seen_paths: set[PurePosixPath] = set()
    entries: list[_GitTreeEntry] = []
    for record in content[:-1].split(b"\0"):
        try:
            header, raw_path = record.split(b"\t", 1)
            raw_mode, raw_type, raw_object_id = header.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            object_type = raw_type.decode("ascii")
            object_id = raw_object_id.decode("ascii")
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise TrialBuildError("Git tree listing is malformed") from exc
        relative_path = _git_tree_relative_path(path)
        if relative_path in seen_paths:
            raise TrialBuildError("Git tree contains duplicate paths")
        seen_paths.add(relative_path)
        if re.fullmatch(r"[0-9a-f]{40,64}", object_id) is None:
            raise TrialBuildError("Git tree object ID is malformed")
        if object_type == "tree" and mode == "040000":
            entries.append(
                _GitTreeEntry(relative_path, mode, object_type, object_id)
            )
            continue
        if object_type == "blob" and mode in {"100644", "100755"}:
            entries.append(
                _GitTreeEntry(relative_path, mode, object_type, object_id)
            )
            continue
        if object_type == "blob" and mode == "120000":
            raise TrialBuildError("Git tree contains a symbolic link")
        raise TrialBuildError("Git tree contains an unsupported source entry")
    return tuple(entries)


def _materialize_git_tree_entries(
    source: Path,
    destination: Path,
    entries: tuple[_GitTreeEntry, ...],
) -> None:
    """Materialize only the parsed regular files and directories from P."""
    for entry in entries:
        if entry.object_type == "tree":
            _make_git_tree_directory(destination.joinpath(*entry.relative_path.parts))
    for entry in entries:
        if entry.object_type != "blob":
            continue
        destination_path = destination.joinpath(*entry.relative_path.parts)
        _make_git_tree_directory(destination_path.parent)
        if destination_path.exists() or destination_path.is_symlink():
            raise TrialBuildError("Git tree path conflicts with an existing entry")
        content = _git_bytes(source, "cat-file", "blob", entry.object_id)
        with destination_path.open("xb") as output:
            output.write(content)
        destination_path.chmod(0o755 if entry.mode == "100755" else 0o644)


def _git_tree_relative_path(path: str) -> PurePosixPath:
    """Reject Git-tree paths that could escape the private staging root."""
    relative_path = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or relative_path.is_absolute()
        or relative_path.as_posix() != path
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise TrialBuildError("Git tree contains an unsafe source path")
    return relative_path


def _make_git_tree_directory(path: Path) -> None:
    """Create a canonical directory while refusing file/symlink collisions."""
    try:
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
    except OSError as exc:
        raise TrialBuildError("Git tree directory cannot be materialized") from exc
    if path.is_symlink() or not path.is_dir():
        raise TrialBuildError("Git tree directory path is unsafe")
    path.chmod(0o755)


def _publish_artifacts(
    artifact_bytes: Mapping[str, bytes],
    output: Path,
    source: Path,
) -> None:
    """Publish through retained descriptors in a non-hostile output hierarchy.

    CI must place ``output.parent`` in an isolated hierarchy with no concurrent
    same-UID ancestor rename capability. Linux pathname/descriptor APIs cannot
    atomically prove ancestry relative to P at the final ``renameat2`` call.
    """
    expected_filenames = set(artifact_bytes)
    if len(expected_filenames) != 4:
        raise TrialBuildError("trial publication must contain exactly four artifacts")
    parent_descriptor = _open_output_parent(output, source)
    stage_name = ""
    stage_descriptor: int | None = None
    published_artifacts_status: os.stat_result | None = None
    published = False
    try:
        stage_name, stage_descriptor = _create_output_stage(
            parent_descriptor,
            output.name,
        )
        _assert_output_parent_path_matches_descriptor(
            output.parent,
            parent_descriptor,
        )
        published_artifacts_status = _write_artifacts_to_stage(
            artifact_bytes,
            stage_descriptor,
        )
        _assert_output_parent_path_matches_descriptor(
            output.parent,
            parent_descriptor,
        )
        _assert_output_stage_bound(parent_descriptor, stage_name, stage_descriptor)
        _rename_no_replace(
            stage_descriptor,
            "artifacts",
            parent_descriptor,
            output.name,
        )
        published = True
        _assert_output_parent_path_matches_descriptor(
            output.parent,
            parent_descriptor,
        )
        _assert_published_artifacts(
            parent_descriptor,
            output.name,
            published_artifacts_status,
            expected_filenames,
        )
    finally:
        if stage_descriptor is not None:
            if published:
                _remove_empty_output_stage(
                    parent_descriptor,
                    stage_name,
                    stage_descriptor,
                )
            else:
                _cleanup_output_stage(
                    parent_descriptor,
                    stage_name,
                    stage_descriptor,
                )
            os.close(stage_descriptor)
        os.close(parent_descriptor)


def _open_output_parent(output: Path, source: Path) -> int:
    """Open and validate the current output parent before publication."""
    try:
        descriptor = os.open(output.parent, _directory_open_flags())
    except OSError as exc:
        raise TrialBuildError(
            "output directory parent cannot be opened safely"
        ) from exc
    try:
        parent_path = _descriptor_path(descriptor, "output directory parent")
        source_path = source.resolve(strict=True)
        if _is_within(parent_path, source_path):
            raise TrialBuildError("output directory must be outside source P")
        try:
            os.stat(output.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return descriptor
        raise FileExistsError("output directory appeared before publication")
    except Exception:
        os.close(descriptor)
        raise


def _assert_output_parent_path_matches_descriptor(
    parent_path: Path,
    expected_descriptor: int,
) -> None:
    """Reject a parent pathname swapped after its directory fd was retained."""
    try:
        current_descriptor = os.open(parent_path, _directory_open_flags())
    except OSError as exc:
        raise TrialBuildError(
            "output directory parent changed during publication"
        ) from exc
    try:
        if not _same_identity(
            os.fstat(current_descriptor),
            os.fstat(expected_descriptor),
        ):
            raise TrialBuildError("output directory parent changed during publication")
    finally:
        os.close(current_descriptor)


def _descriptor_path(descriptor: int, label: str) -> Path:
    """Resolve a retained directory descriptor without trusting its old pathname."""
    try:
        target = os.readlink(f"/proc/self/fd/{descriptor}")
    except OSError as exc:
        raise TrialBuildError(f"cannot resolve {label} safely") from exc
    if target.endswith(" (deleted)"):
        raise TrialBuildError(f"{label} was deleted during publication")
    try:
        return Path(target).resolve(strict=True)
    except OSError as exc:
        raise TrialBuildError(f"cannot resolve {label} safely") from exc


def _create_output_stage(parent_descriptor: int, output_name: str) -> tuple[str, int]:
    """Create a private, retained sibling stage under the output parent fd."""
    for _ in range(128):
        stage_name = f".{output_name}.stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(stage_name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        try:
            named_status = os.stat(
                stage_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            descriptor = os.open(
                stage_name,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise TrialBuildError("output stage could not be opened safely") from exc
        try:
            _validate_private_output_stage(named_status)
            opened_status = os.fstat(descriptor)
            _validate_private_output_stage(opened_status)
            if not _same_identity(named_status, opened_status):
                raise TrialBuildError("output stage changed during initial binding")
            return stage_name, descriptor
        except Exception:
            os.close(descriptor)
            raise
    raise TrialBuildError("unable to allocate a unique output stage")


def _write_artifacts_to_stage(
    artifact_bytes: Mapping[str, bytes],
    stage_descriptor: int,
) -> os.stat_result:
    """Write only the verified in-memory artifact bytes into the private stage."""
    if not artifact_bytes:
        raise TrialBuildError("assembled artifacts are empty")
    try:
        os.mkdir("artifacts", 0o700, dir_fd=stage_descriptor)
        artifacts_descriptor = os.open(
            "artifacts",
            _directory_open_flags(),
            dir_fd=stage_descriptor,
        )
    except OSError as exc:
        raise TrialBuildError(
            "output artifact stage could not be created safely"
        ) from exc
    try:
        for name in sorted(artifact_bytes, key=lambda value: value.encode("utf-8")):
            _write_regular_artifact(name, artifact_bytes[name], artifacts_descriptor)
        os.fsync(artifacts_descriptor)
        artifacts_status = os.fstat(artifacts_descriptor)
        _validate_private_output_stage(artifacts_status)
        return artifacts_status
    finally:
        os.close(artifacts_descriptor)


def _write_regular_artifact(
    name: str,
    content: bytes,
    destination_parent: int,
) -> None:
    """Write one sealed artifact without returning to mutable temporary paths."""
    if not name or name in {".", ".."} or Path(name).name != name:
        raise TrialBuildError("assembled artifact name is unsafe")
    try:
        destination_descriptor = os.open(
            name,
            (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | _no_follow_flag()
                | getattr(os, "O_CLOEXEC", 0)
            ),
            0o600,
            dir_fd=destination_parent,
        )
    except OSError as exc:
        raise TrialBuildError("output artifact could not be created safely") from exc
    try:
        _write_descriptor_bytes(destination_descriptor, content)
        os.fchmod(destination_descriptor, 0o644)
        os.fsync(destination_descriptor)
    finally:
        os.close(destination_descriptor)


def _write_descriptor_bytes(descriptor: int, content: bytes) -> None:
    """Write one complete byte string while handling short writes."""
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("output artifact write failed")
        remaining = remaining[written:]


def _assert_output_stage_bound(
    parent_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
) -> None:
    """Require the output stage name to still refer to its retained fd."""
    _assert_output_stage_identity(parent_descriptor, stage_name, stage_descriptor)
    try:
        names = os.listdir(stage_descriptor)
    except OSError as exc:
        raise TrialBuildError("output stage cannot be inspected safely") from exc
    if names != ["artifacts"]:
        raise TrialBuildError("output stage has unexpected contents")


def _assert_output_stage_identity(
    parent_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
) -> None:
    """Bind the private stage name to the retained descriptor identity."""
    try:
        named_status = os.stat(
            stage_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise TrialBuildError("output stage disappeared before publication") from exc
    _validate_private_output_stage(named_status)
    descriptor_status = os.fstat(stage_descriptor)
    _validate_private_output_stage(descriptor_status)
    if not _same_identity(named_status, descriptor_status):
        raise TrialBuildError("output stage changed before publication")


def _assert_published_artifacts(
    parent_descriptor: int,
    output_name: str,
    expected_status: os.stat_result | None,
    expected_filenames: set[str],
) -> None:
    """Confirm the final output is the verified directory moved from the stage."""
    if expected_status is None:
        raise TrialBuildError("published output has no retained stage identity")
    try:
        current_status = os.stat(
            output_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise TrialBuildError(
            "published output disappeared during publication"
        ) from exc
    _validate_private_output_stage(current_status)
    if not _same_identity(current_status, expected_status):
        raise TrialBuildError("published output changed during publication")
    try:
        output_descriptor = os.open(
            output_name,
            _directory_open_flags(),
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise TrialBuildError("published output cannot be inspected safely") from exc
    try:
        if set(os.listdir(output_descriptor)) != expected_filenames:
            raise TrialBuildError("published output has unexpected contents")
    finally:
        os.close(output_descriptor)


def _rename_no_replace(
    source_parent: int,
    source_name: str,
    destination_parent: int,
    destination_name: str,
) -> None:
    """Publish through Linux renameat2 without ever replacing an existing output."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise TrialBuildError(
            "atomic no-replace publication requires Linux renameat2"
        ) from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            source_parent,
            os.fsencode(source_name),
            destination_parent,
            os.fsencode(destination_name),
            1,  # RENAME_NOREPLACE
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError("output directory appeared before publication")
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _remove_empty_output_stage(
    parent_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
) -> None:
    """Best-effort cleanup after the stage's artifact child was published."""
    try:
        _assert_output_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        if os.listdir(stage_descriptor):
            return
    except TrialBuildError:
        return
    except OSError:
        return
    try:
        os.rmdir(stage_name, dir_fd=parent_descriptor)
    except OSError:
        return


def _cleanup_output_stage(
    parent_descriptor: int,
    stage_name: str,
    stage_descriptor: int,
) -> None:
    """Remove only the known private stage leaves after a failed publication."""
    try:
        _assert_output_stage_identity(parent_descriptor, stage_name, stage_descriptor)
    except TrialBuildError:
        return
    try:
        artifacts_descriptor = os.open(
            "artifacts",
            _directory_open_flags(),
            dir_fd=stage_descriptor,
        )
    except OSError:
        artifacts_descriptor = None
    if artifacts_descriptor is not None:
        try:
            for name in os.listdir(artifacts_descriptor):
                status = os.stat(
                    name,
                    dir_fd=artifacts_descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(status.st_mode):
                    return
                os.unlink(name, dir_fd=artifacts_descriptor)
        except OSError:
            return
        finally:
            os.close(artifacts_descriptor)
        try:
            os.rmdir("artifacts", dir_fd=stage_descriptor)
        except OSError:
            return
    try:
        _assert_output_stage_identity(parent_descriptor, stage_name, stage_descriptor)
        if os.listdir(stage_descriptor):
            return
    except TrialBuildError:
        return
    except OSError:
        return
    try:
        os.rmdir(stage_name, dir_fd=parent_descriptor)
    except OSError:
        return


def _validate_private_output_stage(status: os.stat_result) -> None:
    """Reject a replaced or externally owned output staging directory."""
    if not stat.S_ISDIR(status.st_mode):
        raise TrialBuildError("output stage is not a directory")
    if stat.S_IMODE(status.st_mode) != 0o700 or status.st_uid != os.geteuid():
        raise TrialBuildError("output stage is not private")


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare the device/inode identity retained across publication steps."""
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _same_status(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare an artifact's identity and mutation witness fields."""
    return (
        _same_identity(first, second)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _directory_open_flags() -> int:
    """Return Linux flags needed to bind directory operations to one fd."""
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        raise TrialBuildError("platform lacks O_DIRECTORY for safe publication")
    return (
        os.O_RDONLY
        | directory_flag
        | _no_follow_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )


def _no_follow_flag() -> int:
    """Require an OS primitive that refuses symbolic-link path components."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise TrialBuildError("platform lacks O_NOFOLLOW for safe publication")
    return no_follow


def _regular_file(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path))
    if candidate.is_symlink() or not candidate.is_file():
        raise TrialBuildError(f"{label} must be a regular file")
    return candidate


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _verify_source_git_identity(source: Path, source_sha: str) -> None:
    """Bind the validated identity input to the committed checkout root P."""
    source_physical = source.resolve(strict=True)
    top_level = Path(_git_output(source, "rev-parse", "--show-toplevel"))
    try:
        top_level_physical = top_level.resolve(strict=True)
    except OSError as exc:
        raise TrialBuildError("source root must be a real Git checkout") from exc
    if top_level_physical != source_physical:
        raise TrialBuildError("source root must be the Git checkout root")
    actual_sha = _git_output(
        source,
        "rev-parse",
        "--verify",
        "--end-of-options",
        "HEAD^{commit}",
    )
    if _SOURCE_SHA.fullmatch(actual_sha) is None:
        raise TrialBuildError("source Git HEAD is not a 40-character SHA")
    if actual_sha != source_sha:
        raise TrialBuildError("source SHA does not match Git HEAD")
    if _git_output(
        source,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise TrialBuildError("source Git checkout is not clean")


def _git_output(source: Path, *arguments: str) -> str:
    """Run one bounded Git query without exposing local command diagnostics."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
            env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrialBuildError("source root must be a real Git checkout") from exc
    if completed.returncode != 0:
        raise TrialBuildError("source root must be a real Git checkout")
    return completed.stdout.strip()


def _git_bytes(source: Path, *arguments: str) -> bytes:
    """Read one immutable Git-object command result without worktree filters."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), *arguments],
            capture_output=True,
            check=False,
            timeout=30,
            env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrialBuildError("cannot read the requested Git source object") from exc
    if completed.returncode != 0:
        raise TrialBuildError("cannot read the requested Git source object")
    return completed.stdout


def _git_environment() -> dict[str, str]:
    """Remove ambient Git repository overrides from identity queries."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _source_policy(source: Path) -> tuple[ReleaseSourcePolicy, bytes]:
    """Load only the allowlist committed within the public source checkout."""
    allowlist = _regular_file(source / _ALLOWLIST_PATH, "source allowlist")
    content = allowlist.read_bytes()
    return load_policy_bytes(content, allowlist), content


def _verify_selected_source_allowlist(
    source_entries: Mapping[str, ManifestEntry],
    allowlist_bytes: bytes,
) -> None:
    """Require the controlling source policy itself to be selected by M(P)."""
    entry = source_entries.get(_ALLOWLIST_PATH.as_posix())
    if entry is None or entry.file_type != "file":
        raise TrialBuildError("source allowlist is not selected in source M")
    if _sha256(allowlist_bytes) != entry.sha256:
        raise TrialBuildError("source allowlist changed during source scan")


def _reject_generated_source_files(source_entries: Mapping[str, ManifestEntry]) -> None:
    for path in (_TRIAL_BUILD_PATH, _CAPABILITY_PATH):
        if path.as_posix() in source_entries:
            raise TrialBuildError("generated trial assets must not be part of source P")


def _read_trial_project_metadata(
    path: Path,
    source_entries: Mapping[str, ManifestEntry],
) -> None:
    """Freeze the static project metadata that pip may consume from the wheel."""
    relative_path = _PYPROJECT_PATH.as_posix()
    entry = source_entries.get(relative_path)
    if entry is None or entry.file_type != "file":
        raise TrialBuildError("project metadata is absent from source M")
    content = _regular_file(path, "project metadata").read_bytes()
    if _sha256(content) != entry.sha256:
        raise TrialBuildError("project metadata changed during build")
    try:
        document = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise TrialBuildError("project metadata is malformed") from exc
    project = document.get("project") if isinstance(document, dict) else None
    if not isinstance(project, dict):
        raise TrialBuildError("project metadata has no project table")
    if (
        project.get("name") != _TRIAL_PROJECT_NAME
        or project.get("requires-python") != _TRIAL_PROJECT_REQUIRES_PYTHON
        or project.get("license") != _TRIAL_LICENSE
        or project.get("gui-scripts") != _TRIAL_GUI_SCRIPTS
        or project.get("dynamic") != ["version"]
    ):
        raise TrialBuildError("project metadata does not match the M2 wheel policy")
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list) or tuple(dependencies) != (
        _TRIAL_PROJECT_DEPENDENCIES
    ):
        raise TrialBuildError("project runtime dependencies do not match M2 policy")
    optional = project.get("optional-dependencies")
    if not isinstance(optional, dict) or set(optional) != set(
        _TRIAL_PROJECT_OPTIONAL_DEPENDENCIES
    ):
        raise TrialBuildError("project optional dependencies do not match M2 policy")
    for extra, expected in _TRIAL_PROJECT_OPTIONAL_DEPENDENCIES.items():
        values = optional.get(extra)
        if not isinstance(values, list) or tuple(values) != expected:
            raise TrialBuildError(
                "project optional dependencies do not match M2 policy"
            )


def _read_trial_capability_policy(
    path: Path,
    source_entries: Mapping[str, ManifestEntry],
) -> bytes:
    relative_path = _POLICY_INPUT_PATH.as_posix()
    entry = source_entries.get(relative_path)
    if entry is None or entry.file_type != "file":
        raise TrialBuildError("trial I/O capability policy is absent from source M")
    content = _regular_file(path, "trial I/O capability policy").read_bytes()
    if _sha256(content) != entry.sha256:
        raise TrialBuildError("trial I/O capability policy changed during build")
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise TrialBuildError("trial I/O capability policy is malformed") from exc
    if document != _TRIAL_CAPABILITY_POLICY:
        raise TrialBuildError(
            "trial I/O capability policy is not the reviewed Tier A CSV read"
        )
    return content


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("duplicate JSON field")
    return document


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value!r}")


def _inject_generated_files(
    *,
    staging: Path,
    source_manifest: ReleaseSourceManifest,
    identity: TrialIdentity,
    capability_bytes: bytes,
) -> dict[str, bytes]:
    source_entries = {entry.path: entry for entry in source_manifest.entries}
    version_entry = source_entries.get(_VERSION_PATH.as_posix())
    if version_entry is None or version_entry.file_type != "file":
        raise TrialBuildError("source manifest is missing the package version file")
    version_path = staging / _VERSION_PATH
    version_bytes = make_version_delta(version_path.read_bytes(), identity.version)
    trial_build_bytes = _canonical_json(
        {
            "build_id": identity.build_id,
            "schema": 1,
            "source_manifest_sha256": manifest_digest(source_manifest),
            "source_sha": identity.source_sha,
            "version": identity.version,
        }
    )
    generated_files = {
        _VERSION_PATH.as_posix(): version_bytes,
        _TRIAL_BUILD_PATH.as_posix(): trial_build_bytes,
        _CAPABILITY_PATH.as_posix(): capability_bytes,
    }
    _write_new_file(
        version_path,
        version_bytes,
        mode=int(version_entry.mode, 8),
        replace=True,
    )
    _write_new_file(staging / _TRIAL_BUILD_PATH, trial_build_bytes)
    _write_new_file(staging / _CAPABILITY_PATH, capability_bytes)
    return generated_files


def _write_new_file(
    path: Path,
    content: bytes,
    *,
    mode: int = 0o644,
    replace: bool = False,
) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise TrialBuildError(f"generated file parent is unsafe: {path.name}")
    if path.is_symlink() or (path.exists() and not replace):
        raise TrialBuildError(f"refusing to overwrite generated file: {path.name}")
    path.write_bytes(content)
    path.chmod(mode)


def _raise_source_snapshot_mismatch(
    source_manifest: ReleaseSourceManifest,
    exported_manifest: ReleaseSourceManifest,
) -> None:
    differences = compare_manifests(source_manifest, exported_manifest)
    summary = "; ".join(
        f"{difference.kind}:{difference.path}" for difference in differences
    )
    raise TrialBuildError(f"canonical source snapshot differs from P: {summary}")


def _build_one_wheel(
    staging: Path,
    artifacts: Path,
    identity: TrialIdentity,
) -> Path:
    build_source = artifacts.parent / "wheel-build-source"
    if build_source.exists() or build_source.is_symlink():
        raise TrialBuildError("wheel build source must not already exist")
    shutil.copytree(staging, build_source)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(artifacts),
            str(build_source),
        ],
        cwd=build_source,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        raise TrialBuildError("wheel build failed without installing dependencies")
    wheels = tuple(sorted(artifacts.glob("*.whl")))
    if len(wheels) != 1:
        raise TrialBuildError("wheel build must produce exactly one wheel")
    wheel = wheels[0]
    if not wheel.name.endswith("-py3-none-any.whl"):
        raise TrialBuildError("trial wheel is not tagged py3-none-any")
    return wheel


def _verify_wheel(
    wheel: Path,
    identity: TrialIdentity,
    generated_files: Mapping[str, bytes],
    staging_manifest: ReleaseSourceManifest,
) -> bytes:
    """Validate one stable wheel read and return its sealed bytes for publish."""
    wheel_bytes = _read_stable_regular_file_bytes(wheel, "trial wheel")
    _verify_wheel_bytes(wheel_bytes, identity, generated_files, staging_manifest)
    return wheel_bytes


def _assert_wheel_path_matches_bytes(wheel: Path, expected_bytes: bytes) -> None:
    """Reject a build artifact modified after validation but before sealing."""
    if _read_stable_regular_file_bytes(wheel, "trial wheel") != expected_bytes:
        raise TrialBuildError("trial wheel changed after verification")


def _read_stable_regular_file_bytes(path: Path, label: str) -> bytes:
    """Read one no-follow file and reject a mutation during the read itself."""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | _no_follow_flag() | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise TrialBuildError(f"{label} cannot be opened safely") from exc
    try:
        initial_status = os.fstat(descriptor)
        if not stat.S_ISREG(initial_status.st_mode):
            raise TrialBuildError(f"{label} is not a regular file")
        content = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            content.extend(chunk)
        if not _same_status(initial_status, os.fstat(descriptor)):
            raise TrialBuildError(f"{label} changed while being read")
        return bytes(content)
    finally:
        os.close(descriptor)


def _verify_wheel_bytes(
    wheel_bytes: bytes,
    identity: TrialIdentity,
    generated_files: Mapping[str, bytes],
    staging_manifest: ReleaseSourceManifest,
) -> None:
    """Bind every executable wheel member to one approved staging manifest."""
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            member_names = archive.namelist()
            if len(member_names) != len(set(member_names)):
                raise TrialBuildError("trial wheel contains duplicate archive members")
            _verify_wheel_archive_entries(archive)
            names = set(member_names)
            wheel_metadata = _single_archive_member(names, ".dist-info/WHEEL")
            metadata = _single_archive_member(names, ".dist-info/METADATA")
            metadata_directory = metadata.rsplit("/", 1)[0]
            if wheel_metadata.rsplit("/", 1)[0] != metadata_directory:
                raise TrialBuildError(
                    "trial wheel WHEEL and METADATA use different dist-info directories"
                )
            expected_directory = f"gwexpy_studio-{identity.version}.dist-info"
            if metadata_directory != expected_directory:
                raise TrialBuildError("trial wheel dist-info directory is unexpected")
            wheel_fields = _metadata_fields(
                archive.read(wheel_metadata),
                critical_fields=frozenset({"Root-Is-Purelib", "Tag"}),
            )
            metadata_bytes = archive.read(metadata)
            package_fields = _metadata_fields(
                metadata_bytes,
                critical_fields=frozenset({"Name", "Version"}),
            )
            expected_paths = tuple(path.as_posix() for path in _GENERATED_PATHS)
            if tuple(generated_files) != expected_paths:
                raise TrialBuildError(
                    "generated wheel members do not match staging delta"
                )
            for generated_path, expected_bytes in generated_files.items():
                wheel_path = Path(generated_path).relative_to("src").as_posix()
                if wheel_path not in names:
                    raise TrialBuildError(
                        f"trial wheel omitted generated file: {wheel_path}"
                    )
                if archive.read(wheel_path) != expected_bytes:
                    raise TrialBuildError(
                        f"trial wheel generated file mismatch: {wheel_path}"
                    )
            _verify_trial_wheel_metadata(metadata_bytes, identity)
            _verify_wheel_payload(
                archive,
                names=names,
                metadata_directory=metadata_directory,
                staging_manifest=staging_manifest,
            )
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise TrialBuildError("trial wheel archive is unreadable") from exc
    if wheel_fields.get("Root-Is-Purelib") != "true":
        raise TrialBuildError("trial wheel is not purelib")
    if wheel_fields.get("Tag") != "py3-none-any":
        raise TrialBuildError("trial wheel tag is not py3-none-any")
    if package_fields.get("Name") != "gwexpy-studio":
        raise TrialBuildError("trial wheel package name is unexpected")
    if package_fields.get("Version") != identity.version:
        raise TrialBuildError("trial wheel version does not match its build identity")


def _verify_wheel_archive_entries(archive: zipfile.ZipFile) -> None:
    """Reject directory or symlink entries before interpreting wheel contents."""
    for entry in archive.infolist():
        if entry.is_dir():
            raise TrialBuildError("trial wheel must not contain directory members")
        mode = entry.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise TrialBuildError("trial wheel must not contain symlink members")


def _verify_wheel_payload(
    archive: zipfile.ZipFile,
    *,
    names: set[str],
    metadata_directory: str,
    staging_manifest: ReleaseSourceManifest,
) -> None:
    """Require wheel code, launcher, license, and RECORD to derive from staging."""
    expected_package = _expected_wheel_package_members(staging_manifest)
    actual_package = {
        name for name in names if not name.startswith(f"{metadata_directory}/")
    }
    if actual_package != set(expected_package):
        raise TrialBuildError("trial wheel payload does not match staged package files")
    for name, expected_sha256 in expected_package.items():
        if _sha256(archive.read(name)) != expected_sha256:
            raise TrialBuildError("trial wheel payload content does not match staging")

    license_member = f"{metadata_directory}/licenses/LICENSE"
    record_member = f"{metadata_directory}/RECORD"
    expected_metadata = {
        f"{metadata_directory}/METADATA",
        f"{metadata_directory}/WHEEL",
        f"{metadata_directory}/entry_points.txt",
        f"{metadata_directory}/top_level.txt",
        license_member,
        record_member,
    }
    actual_metadata = {
        name for name in names if name.startswith(f"{metadata_directory}/")
    }
    if actual_metadata != expected_metadata:
        raise TrialBuildError("trial wheel dist-info payload is not the approved set")
    if archive.read(f"{metadata_directory}/entry_points.txt") != _ENTRY_POINTS_BYTES:
        raise TrialBuildError("trial wheel entry point is not the approved launcher")
    if archive.read(f"{metadata_directory}/top_level.txt") != _TOP_LEVEL_BYTES:
        raise TrialBuildError("trial wheel top-level package record is invalid")
    license_entry = _staging_file_entry(staging_manifest, _SOURCE_LICENSE_PATH)
    if _sha256(archive.read(license_member)) != license_entry.sha256:
        raise TrialBuildError("trial wheel license does not match staged source")
    _verify_wheel_record(archive, names=names, record_member=record_member)


def _expected_wheel_package_members(
    staging_manifest: ReleaseSourceManifest,
) -> dict[str, str]:
    """Project staged source package files onto exact pure-wheel members."""
    members: dict[str, str] = {}
    for entry in staging_manifest.entries:
        if entry.file_type != "file" or not entry.path.startswith(
            _PACKAGE_SOURCE_PREFIX
        ):
            continue
        relative_path = entry.path.removeprefix(_PACKAGE_SOURCE_PREFIX)
        wheel_path = f"{_PACKAGE_WHEEL_PREFIX}{relative_path}"
        if wheel_path in members:
            raise TrialBuildError("staging manifest has duplicate package payload")
        members[wheel_path] = entry.sha256
    if not members:
        raise TrialBuildError("staging manifest has no package payload")
    return members


def _staging_file_entry(
    staging_manifest: ReleaseSourceManifest, path: str
) -> ManifestEntry:
    """Return one unique regular file entry needed by wheel metadata."""
    matches = tuple(
        entry
        for entry in staging_manifest.entries
        if entry.path == path and entry.file_type == "file"
    )
    if len(matches) != 1:
        raise TrialBuildError(f"staging manifest is missing required file: {path}")
    return matches[0]


def _verify_wheel_record(
    archive: zipfile.ZipFile,
    *,
    names: set[str],
    record_member: str,
) -> None:
    """Require RECORD to cover exactly every wheel member with canonical hashes."""
    try:
        text = archive.read(record_member).decode("utf-8")
        rows = tuple(csv.reader(io.StringIO(text, newline="")))
    except (UnicodeError, csv.Error) as exc:
        raise TrialBuildError("trial wheel RECORD is unreadable") from exc
    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or not row[0] or row[0] in records:
            raise TrialBuildError("trial wheel RECORD is malformed")
        records[row[0]] = (row[1], row[2])
    if set(records) != names:
        raise TrialBuildError("trial wheel RECORD does not cover its payload")
    for name in sorted(names):
        digest, size = records[name]
        if name == record_member:
            if digest or size:
                raise TrialBuildError("trial wheel RECORD self-entry is invalid")
            continue
        content = archive.read(name)
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(content).digest()
        ).rstrip(b"=").decode("ascii")
        if digest != f"sha256={expected_digest}" or size != str(len(content)):
            raise TrialBuildError("trial wheel RECORD does not match its payload")


def _verify_trial_wheel_metadata(content: bytes, identity: TrialIdentity) -> None:
    """Reject dependency metadata not prescribed by the reviewed M2 policy."""
    headers = _metadata_headers(content)
    if headers.get("name") != [_TRIAL_PROJECT_NAME] or headers.get("version") != [
        identity.version
    ]:
        raise TrialBuildError("trial wheel metadata identity is not approved")
    requires_python = headers.get("requires-python")
    if (
        requires_python is None
        or len(requires_python) != 1
        or _normalized_requires_python(requires_python[0])
        != _normalized_requires_python(_TRIAL_PROJECT_REQUIRES_PYTHON)
    ):
        raise TrialBuildError("trial wheel Requires-Python metadata is not approved")
    actual_requirements = tuple(
        sorted(
            _normalized_requirement(value)
            for value in headers.get("requires-dist", [])
        )
    )
    if actual_requirements != _expected_trial_requirements():
        raise TrialBuildError("trial wheel Requires-Dist metadata is not approved")
    if headers.get("provides-extra") != sorted(
        _TRIAL_PROJECT_OPTIONAL_DEPENDENCIES
    ):
        raise TrialBuildError("trial wheel Provides-Extra metadata is not approved")
    if headers.get("dynamic") != ["license-file"]:
        raise TrialBuildError("trial wheel Dynamic metadata is not approved")
    if headers.get("license-file") != ["LICENSE"]:
        raise TrialBuildError("trial wheel License-File metadata is not approved")


def _metadata_headers(content: bytes) -> dict[str, list[str]]:
    """Collect dependency-relevant METADATA headers case-insensitively."""
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise TrialBuildError("trial wheel metadata is not valid UTF-8") from exc
    headers: dict[str, list[str]] = {}
    previous = ""
    for line in lines:
        if line.startswith((" ", "\t")):
            if previous in _RELEVANT_METADATA_FIELDS:
                raise TrialBuildError("trial wheel metadata has a folded policy field")
            continue
        if ":" not in line:
            previous = ""
            continue
        raw_name, raw_value = line.split(":", 1)
        name = raw_name.strip().lower()
        previous = name
        if name in _RELEVANT_METADATA_FIELDS:
            headers.setdefault(name, []).append(raw_value.strip())
    return headers


def _expected_trial_requirements(
) -> tuple[tuple[str, tuple[tuple[str, str], ...], str], ...]:
    """Return the complete runtime and optional-dependency policy multiset."""
    requirements = [
        _normalized_requirement(value) for value in _TRIAL_PROJECT_DEPENDENCIES
    ]
    for extra, values in _TRIAL_PROJECT_OPTIONAL_DEPENDENCIES.items():
        requirements.extend(
            _normalized_requirement(f'{value}; extra == "{extra}"')
            for value in values
        )
    return tuple(sorted(requirements))


def _normalized_requires_python(value: str) -> tuple[tuple[str, str], ...]:
    """Normalize the restricted specifier grammar used by the M2 Python range."""
    compact = "".join(value.split())
    if not compact:
        raise TrialBuildError("trial wheel Requires-Python metadata is malformed")
    parsed: list[tuple[str, str]] = []
    for item in compact.split(","):
        match = _REQUIREMENT_SPECIFIER.fullmatch(item)
        if match is None:
            raise TrialBuildError("trial wheel Requires-Python metadata is malformed")
        parsed.append((match.group(1), match.group(2)))
    return tuple(sorted(parsed))


def _normalized_requirement(
    value: str,
) -> tuple[str, tuple[tuple[str, str], ...], str]:
    """Normalize the simple PEP 508 subset prescribed by the M2 project policy."""
    requirement, marker_separator, raw_marker = value.partition(";")
    if marker_separator and ";" in raw_marker:
        raise TrialBuildError("trial wheel Requires-Dist metadata is malformed")
    compact = "".join(requirement.split())
    name_match = _REQUIREMENT_NAME.match(compact)
    if name_match is None:
        raise TrialBuildError("trial wheel Requires-Dist metadata is malformed")
    name = name_match.group(0)
    remainder = compact[len(name) :]
    specifiers: list[tuple[str, str]] = []
    if remainder:
        for item in remainder.split(","):
            match = _REQUIREMENT_SPECIFIER.fullmatch(item)
            if match is None:
                raise TrialBuildError("trial wheel Requires-Dist metadata is malformed")
            specifiers.append((match.group(1), match.group(2)))
    marker = ""
    if marker_separator:
        marker = re.sub(r"\s+", "", raw_marker).replace("'", '"')
        if _SIMPLE_MARKER.fullmatch(marker) is None:
            raise TrialBuildError("trial wheel Requires-Dist metadata is malformed")
    normalized_name = re.sub(r"[-_.]+", "-", name).lower()
    return normalized_name, tuple(sorted(specifiers)), marker


def _single_archive_member(names: set[str], suffix: str) -> str:
    matches = tuple(sorted(name for name in names if name.endswith(suffix)))
    if len(matches) != 1:
        raise TrialBuildError(f"trial wheel has ambiguous {suffix} metadata")
    return matches[0]


def _metadata_fields(
    content: bytes,
    *,
    critical_fields: frozenset[str],
) -> dict[str, str]:
    """Parse metadata while rejecting duplicate security-relevant claims."""
    fields: dict[str, str] = {}
    for raw_line in content.decode("utf-8").splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        if key in critical_fields and key in fields:
            raise TrialBuildError(f"duplicate critical wheel metadata field: {key}")
        fields.setdefault(key, value.strip())
    return fields


def _trial_manifest_bytes(
    *,
    identity: TrialIdentity,
    source_manifest: ReleaseSourceManifest,
    staging_manifest: ReleaseSourceManifest,
    generated_files: Mapping[str, bytes],
    wheel: Path,
    wheel_sha256: str,
) -> bytes:
    return _canonical_json(
        {
            "build_id": identity.build_id,
            "generated_files": [
                {"path": path, "sha256": _sha256(content)}
                for path, content in generated_files.items()
            ],
            "schema": 1,
            "source_manifest_sha256": manifest_digest(source_manifest),
            "source_sha": identity.source_sha,
            "staging_manifest_sha256": manifest_digest(staging_manifest),
            "version": identity.version,
            "wheel_filename": wheel.name,
            "wheel_sha256": wheel_sha256,
        }
    )


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--utc-date", required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build a trial wheel without accepting a handwritten build identifier."""
    arguments = _parser().parse_args(argv)
    try:
        result = build_trial_wheel(
            source_root=arguments.source,
            output_directory=arguments.output,
            source_sha=arguments.source_sha,
            utc_date=arguments.utc_date,
            run=arguments.run,
            attempt=arguments.attempt,
        )
    except (
        OSError,
        ReleaseSourceError,
        TrialBuildError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"trial-wheel: error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "build_id": result.identity.build_id,
                "version": result.identity.version,
                "wheel_filename": result.wheel_filename,
                "wheel_sha256": result.wheel_sha256,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
