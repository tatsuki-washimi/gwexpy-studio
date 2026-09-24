"""Source-bound conda toolchain contracts for trial distribution builds."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

try:
    from .trial_targets import TrialTargetError, trial_target
except ImportError:  # pragma: no cover - direct public-script invocation.
    from trial_targets import TrialTargetError, trial_target  # type: ignore[no-redef]


class ToolchainError(ValueError):
    """Raised when a target or toolchain record is outside the contract."""


_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*")
_SUBDIRS = {
    "x86_64": "linux-64",
    "aarch64": "linux-aarch64",
    "arm64": "osx-arm64",
}
_MINIFORGE_KEYS = ("Linux-aarch64", "Linux-x86_64", "MacOSX-arm64")
_CONDA_PACKAGE_FIELDS = {"build", "name", "version"}
_CONDA_PACKAGE_NAME = re.compile(r"_?[a-z0-9][a-z0-9_.+-]*")
_CONDA_BUILD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
_DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1] / "packaging" / "trial-toolchain.json"
)


def load_toolchain_manifest(path: Path | None = None) -> dict[str, object]:
    """Load and validate the public, checksum-pinned toolchain manifest."""
    selected = _DEFAULT_MANIFEST if path is None else Path(path)
    try:
        document = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolchainError("toolchain manifest cannot be read") from exc
    if not isinstance(document, dict) or set(document) != {
        "debian13_container",
        "miniforge",
        "schema",
    }:
        raise ToolchainError("toolchain manifest schema is invalid")
    if document["schema"] != 1:
        raise ToolchainError("toolchain manifest schema is unsupported")
    miniforge = document["miniforge"]
    if not isinstance(miniforge, dict) or set(miniforge) != {"artifacts", "release"}:
        raise ToolchainError("Miniforge toolchain record is invalid")
    if miniforge["release"] != "26.7.2-0":
        raise ToolchainError("Miniforge release is not pinned")
    artifacts = miniforge["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(_MINIFORGE_KEYS):
        raise ToolchainError("Miniforge artifact set is invalid")
    for platform_key in _MINIFORGE_KEYS:
        artifact = artifacts[platform_key]
        if not isinstance(artifact, dict) or set(artifact) != {"filename", "sha256"}:
            raise ToolchainError("Miniforge artifact record is invalid")
        filename = artifact["filename"]
        sha256 = artifact["sha256"]
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".sh")
            or not isinstance(sha256, str)
            or _HEX_SHA256.fullmatch(sha256) is None
        ):
            raise ToolchainError("Miniforge artifact identity is invalid")
    container = document["debian13_container"]
    if not isinstance(container, dict) or set(container) != {
        "digest",
        "image",
        "platform",
    }:
        raise ToolchainError("Debian container record is invalid")
    if (
        container["image"] != "debian:13-slim"
        or container["platform"] != "linux/amd64"
        or not isinstance(container["digest"], str)
        or not container["digest"].startswith("sha256:")
        or _HEX_SHA256.fullmatch(container["digest"][len("sha256:") :]) is None
    ):
        raise ToolchainError("Debian container is not digest pinned")
    return cast(dict[str, object], document)


def miniforge_artifact(
    *, system: str, architecture: str, manifest: Mapping[str, object] | None = None
) -> dict[str, str]:
    """Return the pinned Miniforge filename and digest for one host."""
    platform_keys = {
        ("linux", "x86_64"): "Linux-x86_64",
        ("linux", "aarch64"): "Linux-aarch64",
        ("darwin", "arm64"): "MacOSX-arm64",
    }
    try:
        platform_key = platform_keys[(system, architecture)]
    except KeyError as exc:
        raise ToolchainError("Miniforge host platform is unsupported") from exc
    selected = load_toolchain_manifest() if manifest is None else manifest
    miniforge = selected.get("miniforge")
    if not isinstance(miniforge, Mapping):
        raise ToolchainError("Miniforge toolchain record is invalid")
    artifacts = miniforge.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ToolchainError("Miniforge artifact set is invalid")
    artifact = artifacts.get(platform_key)
    if not isinstance(artifact, Mapping):
        raise ToolchainError("Miniforge artifact is missing")
    filename = artifact.get("filename")
    sha256 = artifact.get("sha256")
    if not isinstance(filename, str) or not isinstance(sha256, str):
        raise ToolchainError("Miniforge artifact identity is invalid")
    return {"filename": filename, "sha256": sha256}


def miniforge_download_url(
    *, system: str, architecture: str, manifest: Mapping[str, object] | None = None
) -> str:
    """Return the official URL for a pinned Miniforge installer."""
    selected = load_toolchain_manifest() if manifest is None else manifest
    miniforge = selected.get("miniforge")
    if not isinstance(miniforge, Mapping):
        raise ToolchainError("Miniforge toolchain record is invalid")
    release = miniforge.get("release")
    if not isinstance(release, str) or _VERSION.fullmatch(release) is None:
        raise ToolchainError("Miniforge release is invalid")
    artifact = miniforge_artifact(
        system=system, architecture=architecture, manifest=selected
    )
    return (
        "https://github.com/conda-forge/miniforge/releases/download/"
        f"{release}/{artifact['filename']}"
    )


def debian_container_reference(
    manifest: Mapping[str, object] | None = None,
) -> str:
    """Return the digest-bound Debian container reference."""
    selected = load_toolchain_manifest() if manifest is None else manifest
    container = selected.get("debian13_container")
    if not isinstance(container, Mapping):
        raise ToolchainError("Debian container record is invalid")
    image = container.get("image")
    digest = container.get("digest")
    platform = container.get("platform")
    if (
        not isinstance(image, str)
        or not isinstance(digest, str)
        or platform != "linux/amd64"
    ):
        raise ToolchainError("Debian container record is invalid")
    return f"{image}@{digest}"


def conda_subdir(target_id: str, architecture: str) -> str:
    """Return the conda subdir bound to a closed target architecture."""
    try:
        target = trial_target(target_id)
        target.require_architecture(architecture)
    except TrialTargetError as exc:
        raise ToolchainError(str(exc)) from exc
    try:
        return _SUBDIRS[architecture]
    except KeyError as exc:
        raise ToolchainError("architecture has no conda subdir") from exc


def fresh_conda_create_command(
    *,
    conda_executable: Path,
    target_id: str,
    architecture: str,
    prefix: Path,
    package_records: Sequence[Mapping[str, object]] | None = None,
) -> tuple[str, ...]:
    """Build the only supported fresh environment constructor command."""
    conda_subdir(target_id, architecture)
    if not isinstance(conda_executable, Path) or not str(conda_executable):
        raise ToolchainError("conda executable is required")
    if not isinstance(prefix, Path) or not str(prefix):
        raise ToolchainError("conda prefix is required")
    specs = (
        ("python=3.12", "pip")
        if package_records is None
        else conda_package_matchspecs(package_records)
    )
    if not specs:
        raise ToolchainError("conda package closure is empty")
    return (
        str(conda_executable),
        "create",
        "--yes",
        "--quiet",
        "--override-channels",
        "--channel",
        "conda-forge",
        "--prefix",
        str(prefix),
        *specs,
    )


def conda_environment_manager_record(
    *, target_id: str, architecture: str, version: str
) -> dict[str, object]:
    """Return the path-free schema-4 conda manager record."""
    subdir = conda_subdir(target_id, architecture)
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ToolchainError("conda version is invalid")
    return {
        "kind": "conda",
        "requested_specs": ["python=3.12", "pip"],
        "subdir": subdir,
        "version": version,
    }


def conda_environment_manager_record_from_info(
    *, target_id: str, architecture: str, info: Mapping[str, object]
) -> dict[str, object]:
    """Build the manager record from the fields emitted by ``conda info``.

    Conda 26 reports the target subdir as ``platform``.  It does not expose a
    ``subdir`` key in this JSON document, so the persisted schema field is
    derived from that observed value rather than from an assumed key name.
    """
    if not isinstance(info, Mapping):
        raise ToolchainError("conda info is invalid")
    expected_subdir = conda_subdir(target_id, architecture)
    if info.get("platform") != expected_subdir:
        raise ToolchainError("conda info platform does not match target subdir")
    version = info.get("conda_version")
    if not isinstance(version, str):
        raise ToolchainError("conda info version is missing")
    return conda_environment_manager_record(
        target_id=target_id,
        architecture=architecture,
        version=version,
    )


def conda_package_matchspecs(
    packages: Sequence[Mapping[str, object]],
) -> tuple[str, ...]:
    """Convert canonical conda identities into exact name=version=build specs."""
    if isinstance(packages, (str, bytes)):
        raise ToolchainError("conda package closure is invalid")
    records: list[tuple[str, str, str]] = []
    for package in packages:
        if not isinstance(package, Mapping) or set(package) != _CONDA_PACKAGE_FIELDS:
            raise ToolchainError("conda package record is invalid")
        name = package.get("name")
        version = package.get("version")
        build = package.get("build")
        if not isinstance(name, str) or _CONDA_PACKAGE_NAME.fullmatch(name) is None:
            raise ToolchainError("conda package name is invalid")
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            raise ToolchainError("conda package version is invalid")
        if not isinstance(build, str) or _CONDA_BUILD.fullmatch(build) is None:
            raise ToolchainError("conda package build is invalid")
        records.append((name, version, build))
    records.sort(key=lambda item: item[0].encode("utf-8"))
    names = [record[0] for record in records]
    if not records:
        raise ToolchainError("conda package closure is empty")
    if len(names) != len(set(names)):
        raise ToolchainError("conda package closure contains a package more than once")
    return tuple(f"{name}={version}={build}" for name, version, build in records)


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for a bootstrap artifact."""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ToolchainError("toolchain artifact cannot be read") from exc
    return digest.hexdigest()
