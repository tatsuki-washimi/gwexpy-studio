"""Assemble one immutable, self-verifying M2 trial asset bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

try:  # Support both ``python scripts/...`` and ``import scripts...``.
    from . import capture_trial_resolution as _trial_resolution
    from .build_trial_wheel import (
        TrialBuildError,
        TrialIdentity,
        _assert_output_parent_path_matches_descriptor,
        _assert_output_stage_bound,
        _assert_published_artifacts,
        _cleanup_output_stage,
        _create_output_stage,
        _directory_open_flags,
        _remove_empty_output_stage,
        _rename_no_replace,
        _verify_wheel_bytes,
        _write_artifacts_to_stage,
        verify_staging_delta,
    )
    from .release_source_manifest import (
        ManifestEntry,
        ReleaseSourceManifest,
        manifest_digest,
    )
    from .run_trial_technical_gate import GateError, read_gate_result
except ImportError:  # pragma: no cover - exercised by direct CLI invocation.
    import capture_trial_resolution as _trial_resolution  # type: ignore[no-redef]
    from build_trial_wheel import (  # type: ignore[no-redef]
        TrialBuildError,
        TrialIdentity,
        _assert_output_parent_path_matches_descriptor,
        _assert_output_stage_bound,
        _assert_published_artifacts,
        _cleanup_output_stage,
        _create_output_stage,
        _directory_open_flags,
        _remove_empty_output_stage,
        _rename_no_replace,
        _verify_wheel_bytes,
        _write_artifacts_to_stage,
        verify_staging_delta,
    )
    from release_source_manifest import (  # type: ignore[no-redef]
        ManifestEntry,
        ReleaseSourceManifest,
        manifest_digest,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        GateError,
        read_gate_result,
    )


class TrialBundleError(RuntimeError):
    """Raised when a trial bundle cannot prove a complete M2 identity."""


@dataclass(frozen=True, slots=True)
class _TrialInputs:
    """Validated schema-1 wheel build inputs retained only during assembly."""

    build_id: str
    generated_files: dict[str, bytes]
    preliminary_trial_manifest_sha256: str
    source_manifest: ReleaseSourceManifest
    source_manifest_sha256: str
    source_sha: str
    staging_manifest_sha256: str
    version: str
    wheel_bytes: bytes
    wheel_filename: str
    wheel_sha256: str


_ASSET_NAMES = frozenset(
    {
        "constraints-ubuntu24-aarch64.txt",
        "constraints-ubuntu24-x86_64.txt",
        "Quick-Start.ja.md",
        "Quick-Start.md",
        "SHA256SUMS",
        "SOURCE-MANIFEST.json",
        "TRIAL-MANIFEST.json",
        "resolution-ubuntu24-aarch64.json",
        "resolution-ubuntu24-x86_64.json",
    }
)
_ARCHITECTURES = ("aarch64", "x86_64")
_BUILD_ID = re.compile(
    r"P-(?P<sha>[0-9a-f]{7})-(?P<date>[0-9]{8})-"
    r"r(?P<run>[1-9][0-9]*)-a(?P<attempt>[1-9][0-9]*)"
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_MACHINE = re.compile(r"(?:aarch64|x86_64)")
_PYTHON_VERSION = re.compile(r"3\.12\.[0-9]+")
_TRIAL_VERSION = re.compile(
    r"0\.1\.0a1\+trial\.p\.(?P<sha>[0-9a-f]{7})\."
    r"(?P<date>[0-9]{8})\.r(?P<run>[1-9][0-9]*)\.a(?P<attempt>[1-9][0-9]*)"
)
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*")
_PACKAGE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_GENERATED_PATHS = (
    "src/gwexpy_studio/_version.py",
    "src/gwexpy_studio/assets/trial-build.json",
    "src/gwexpy_studio/assets/io-capabilities.json",
)
_GENERATED_MEMBERS = tuple(path.removeprefix("src/") for path in _GENERATED_PATHS)
_POLICY_INPUT_PATH = "packaging/trial-io-capabilities.json"
_PRELIMINARY_FIELDS = {
    "build_id",
    "generated_files",
    "schema",
    "source_manifest_sha256",
    "source_sha",
    "staging_manifest_sha256",
    "version",
    "wheel_filename",
    "wheel_sha256",
}
_FINAL_FIELDS = {
    "architectures",
    "build",
    "preliminary_trial_manifest_sha256",
    "quick_start",
    "schema",
    "source",
    "staging",
    "wheel",
}
_RESOLUTION_FIELDS = {
    "architecture",
    "build_id",
    "constraints_sha256",
    "glibc_version",
    "os_id",
    "os_version",
    "os_runtime",
    "phase_one",
    "phase_two",
    "pip_version",
    "python_version",
    "runtime_artifacts",
    "schema",
    "source_manifest_sha256",
    "source_sha",
    "staging_manifest_sha256",
    "technical_gate",
    "version",
    "wheel",
}


def assemble_trial_bundle(
    *,
    output_directory: Path,
    wheel: Path,
    preliminary_trial_manifest: Path,
    source_manifest: Path,
    staging_manifest: Path,
    constraints_x86_64: Path,
    resolution_x86_64: Path,
    constraints_aarch64: Path,
    resolution_aarch64: Path,
    quick_start: Path,
    quick_start_ja: Path,
    repository: str,
    build_workflow_path: str,
    build_run_id: int,
    build_run_number: int,
    build_run_attempt: int,
) -> Path:
    """Bind one build and two native resolutions into a fresh bundle directory."""
    inputs = _load_trial_inputs(
        wheel=wheel,
        preliminary_trial_manifest=preliminary_trial_manifest,
        source_manifest=source_manifest,
        staging_manifest=staging_manifest,
    )
    workflow = _workflow_identity(
        repository=repository,
        workflow_path=build_workflow_path,
        run_id=build_run_id,
        run_number=build_run_number,
        run_attempt=build_run_attempt,
        build_id=inputs.build_id,
    )
    quick_start_bytes = _read_regular_bytes(quick_start, "English Quick Start")
    quick_start_ja_bytes = _read_regular_bytes(quick_start_ja, "Japanese Quick Start")
    _verify_quick_start_source(
        inputs.source_manifest,
        "docs/Quick-Start.md",
        quick_start_bytes,
    )
    _verify_quick_start_source(
        inputs.source_manifest,
        "docs/Quick-Start.ja.md",
        quick_start_ja_bytes,
    )
    architecture_records = {
        "x86_64": _architecture_record(
            architecture="x86_64",
            constraints=constraints_x86_64,
            resolution=resolution_x86_64,
            inputs=inputs,
        ),
        "aarch64": _architecture_record(
            architecture="aarch64",
            constraints=constraints_aarch64,
            resolution=resolution_aarch64,
            inputs=inputs,
        ),
    }
    final_manifest = {
        "architectures": architecture_records,
        "build": {
            "id": inputs.build_id,
            "source_sha": inputs.source_sha,
            "version": inputs.version,
            "workflow": workflow,
        },
        "preliminary_trial_manifest_sha256": (inputs.preliminary_trial_manifest_sha256),
        "quick_start": {
            "en": {
                "filename": "Quick-Start.md",
                "sha256": _sha256(quick_start_bytes),
                "source_path": "docs/Quick-Start.md",
            },
            "ja": {
                "filename": "Quick-Start.ja.md",
                "sha256": _sha256(quick_start_ja_bytes),
                "source_path": "docs/Quick-Start.ja.md",
            },
        },
        "schema": 2,
        "source": {
            "manifest_filename": "SOURCE-MANIFEST.json",
            "manifest_sha256": inputs.source_manifest_sha256,
        },
        "staging": {
            "generated_files": _generated_records(inputs.generated_files),
            "manifest_sha256": inputs.staging_manifest_sha256,
        },
        "wheel": {
            "filename": inputs.wheel_filename,
            "sha256": inputs.wheel_sha256,
        },
    }
    files = {
        inputs.wheel_filename: inputs.wheel_bytes,
        "constraints-ubuntu24-x86_64.txt": _read_regular_bytes(
            constraints_x86_64, "x86_64 constraints"
        ),
        "constraints-ubuntu24-aarch64.txt": _read_regular_bytes(
            constraints_aarch64, "aarch64 constraints"
        ),
        "resolution-ubuntu24-x86_64.json": _read_regular_bytes(
            resolution_x86_64, "x86_64 resolution"
        ),
        "resolution-ubuntu24-aarch64.json": _read_regular_bytes(
            resolution_aarch64, "aarch64 resolution"
        ),
        "SOURCE-MANIFEST.json": _manifest_bytes(inputs.source_manifest),
        "TRIAL-MANIFEST.json": _canonical_json(final_manifest),
        "Quick-Start.md": quick_start_bytes,
        "Quick-Start.ja.md": quick_start_ja_bytes,
    }
    files["SHA256SUMS"] = _checksums(files)
    verify_trial_bundle_bytes(files)
    _publish_bundle(files, output_directory)
    return output_directory


def verify_trial_bundle_bytes(files: Mapping[str, bytes]) -> dict[str, object]:
    """Verify all final bundle bytes without a checkout, network, or discovery."""
    material = _validated_file_mapping(files)
    trial = _canonical_object(material["TRIAL-MANIFEST.json"], "trial manifest")
    if (
        not isinstance(trial, dict)
        or set(trial) != _FINAL_FIELDS
        or trial["schema"] != 2
    ):
        raise TrialBundleError("final trial manifest schema is invalid")
    build = _mapping(trial["build"], "final build")
    _require_keys(build, {"id", "source_sha", "version", "workflow"}, "final build")
    build_id = _required_string(build["id"], "build ID")
    source_sha = _required_source_sha(build["source_sha"], "source SHA")
    version = _required_string(build["version"], "trial version")
    run, attempt = _validate_identity(build_id, source_sha, version)
    workflow = _mapping(build["workflow"], "build workflow")
    _require_keys(
        workflow,
        {"repository", "run_attempt", "run_id", "run_number", "workflow_path"},
        "build workflow",
    )
    _validate_workflow_record(workflow, run=run, attempt=attempt)

    source = _mapping(trial["source"], "source record")
    _require_keys(source, {"manifest_filename", "manifest_sha256"}, "source record")
    if source["manifest_filename"] != "SOURCE-MANIFEST.json":
        raise TrialBundleError("source manifest filename is invalid")
    source_manifest = _manifest_from_bytes(material["SOURCE-MANIFEST.json"])
    source_manifest_sha256 = manifest_digest(source_manifest)
    if (
        _required_sha(source["manifest_sha256"], "source manifest")
        != source_manifest_sha256
    ):
        raise TrialBundleError("source manifest digest does not match M")

    wheel = _mapping(trial["wheel"], "wheel record")
    _require_keys(wheel, {"filename", "sha256"}, "wheel record")
    wheel_filename = _required_filename(wheel["filename"], "wheel filename")
    wheel_bytes = material.get(wheel_filename)
    if wheel_bytes is None or not wheel_filename.endswith("-py3-none-any.whl"):
        raise TrialBundleError("bundle wheel is missing or not pure Python")
    wheel_sha256 = _required_sha(wheel["sha256"], "wheel")
    if _sha256(wheel_bytes) != wheel_sha256:
        raise TrialBundleError("bundle wheel digest does not match its manifest")

    staging = _mapping(trial["staging"], "staging record")
    _require_keys(staging, {"generated_files", "manifest_sha256"}, "staging record")
    generated = _generated_wheel_files(
        wheel_bytes,
        generated_records=staging["generated_files"],
    )
    reconstructed_staging = _reconstruct_staging_manifest(source_manifest, generated)
    identity = TrialIdentity(source_sha=source_sha, build_id=build_id, version=version)
    try:
        _verify_wheel_bytes(
            wheel_bytes,
            identity,
            generated,
            reconstructed_staging,
        )
        _verify_generated_trial_semantics(
            wheel_bytes=wheel_bytes,
            generated_files=generated,
            identity=identity,
            source_manifest=source_manifest,
        )
    except (TrialBuildError, _trial_resolution.ResolutionError) as exc:
        raise TrialBundleError(
            "bundle wheel payload or metadata does not match its trial identity"
        ) from exc
    staging_manifest_sha256 = manifest_digest(reconstructed_staging)
    if _required_sha(staging["manifest_sha256"], "staging manifest") != (
        staging_manifest_sha256
    ):
        raise TrialBundleError("staging manifest cannot be reconstructed from M")

    preliminary_sha256 = _required_sha(
        trial["preliminary_trial_manifest_sha256"], "preliminary trial manifest"
    )
    if preliminary_sha256 != _sha256(
        _preliminary_manifest_bytes(
            build_id=build_id,
            generated_files=generated,
            source_manifest_sha256=source_manifest_sha256,
            source_sha=source_sha,
            staging_manifest_sha256=staging_manifest_sha256,
            version=version,
            wheel_filename=wheel_filename,
            wheel_sha256=wheel_sha256,
        )
    ):
        raise TrialBundleError(
            "final manifest cannot reconstruct the preliminary record"
        )

    architectures = _mapping(trial["architectures"], "architecture records")
    if set(architectures) != set(_ARCHITECTURES):
        raise TrialBundleError("final manifest must contain both native architectures")
    for architecture in _ARCHITECTURES:
        _verify_architecture_bundle_record(
            architecture=architecture,
            record=architectures[architecture],
            files=material,
            build_id=build_id,
            source_manifest_sha256=source_manifest_sha256,
            source_sha=source_sha,
            staging_manifest_sha256=staging_manifest_sha256,
            version=version,
            wheel_filename=wheel_filename,
            wheel_sha256=wheel_sha256,
        )
    _verify_quick_start_records(trial["quick_start"], source_manifest, material)
    expected_checksums = _checksums(
        {name: content for name, content in material.items() if name != "SHA256SUMS"}
    )
    if material["SHA256SUMS"] != expected_checksums:
        raise TrialBundleError("SHA256SUMS is not canonical or does not match assets")
    return trial


def _load_trial_inputs(
    *,
    wheel: Path,
    preliminary_trial_manifest: Path,
    source_manifest: Path,
    staging_manifest: Path,
) -> _TrialInputs:
    """Read and validate all schema-1 inputs before creating an output stage."""
    wheel_bytes = _read_regular_bytes(wheel, "trial wheel")
    preliminary_bytes = _read_regular_bytes(
        preliminary_trial_manifest, "preliminary trial manifest"
    )
    source_bytes = _read_regular_bytes(source_manifest, "source manifest")
    staging_bytes = _read_regular_bytes(staging_manifest, "staging manifest")
    source = _manifest_from_bytes(source_bytes)
    staging = _manifest_from_bytes(staging_bytes)
    preliminary = _canonical_object(preliminary_bytes, "preliminary trial manifest")
    if (
        not isinstance(preliminary, dict)
        or set(preliminary) != _PRELIMINARY_FIELDS
        or preliminary["schema"] != 1
    ):
        raise TrialBundleError("preliminary trial manifest schema is invalid")
    build_id = _required_string(preliminary["build_id"], "build ID")
    source_sha = _required_source_sha(preliminary["source_sha"], "source SHA")
    version = _required_string(preliminary["version"], "trial version")
    _validate_identity(build_id, source_sha, version)
    source_manifest_sha256 = _required_sha(
        preliminary["source_manifest_sha256"], "source manifest"
    )
    if source_manifest_sha256 != manifest_digest(source):
        raise TrialBundleError("preliminary source manifest digest does not match M")
    staging_manifest_sha256 = _required_sha(
        preliminary["staging_manifest_sha256"], "staging manifest"
    )
    if staging_manifest_sha256 != manifest_digest(staging):
        raise TrialBundleError("preliminary staging manifest digest does not match")
    wheel_filename = _required_filename(preliminary["wheel_filename"], "wheel filename")
    if wheel.name != wheel_filename or not wheel_filename.endswith("-py3-none-any.whl"):
        raise TrialBundleError("preliminary wheel filename is invalid")
    wheel_sha256 = _required_sha(preliminary["wheel_sha256"], "wheel")
    if _sha256(wheel_bytes) != wheel_sha256:
        raise TrialBundleError("preliminary wheel digest does not match")
    generated = _generated_wheel_files(
        wheel_bytes,
        generated_records=preliminary["generated_files"],
    )
    identity = TrialIdentity(
        source_sha=source_sha,
        build_id=build_id,
        version=version,
    )
    try:
        _verify_generated_trial_semantics(
            wheel_bytes=wheel_bytes,
            generated_files=generated,
            identity=identity,
            source_manifest=source,
        )
    except _trial_resolution.ResolutionError as exc:
        raise TrialBundleError(
            "generated trial files are not semantically valid"
        ) from exc
    try:
        verify_staging_delta(
            source_manifest=source,
            staging_manifest=staging,
            generated_files=generated,
        )
    except TrialBuildError as exc:
        raise TrialBundleError("preliminary trial delta is not approved") from exc
    try:
        _verify_wheel_bytes(wheel_bytes, identity, generated, staging)
    except TrialBuildError as exc:
        raise TrialBundleError(
            "preliminary trial wheel payload or metadata is invalid"
        ) from exc
    return _TrialInputs(
        build_id=build_id,
        generated_files=generated,
        preliminary_trial_manifest_sha256=_sha256(preliminary_bytes),
        source_manifest=source,
        source_manifest_sha256=source_manifest_sha256,
        source_sha=source_sha,
        staging_manifest_sha256=staging_manifest_sha256,
        version=version,
        wheel_bytes=wheel_bytes,
        wheel_filename=wheel_filename,
        wheel_sha256=wheel_sha256,
    )


def _architecture_record(
    *,
    architecture: str,
    constraints: Path,
    resolution: Path,
    inputs: _TrialInputs,
) -> dict[str, object]:
    constraints_bytes = _read_regular_bytes(constraints, f"{architecture} constraints")
    resolution_bytes = _read_regular_bytes(resolution, f"{architecture} resolution")
    expected_constraints = f"constraints-ubuntu24-{architecture}.txt"
    expected_resolution = f"resolution-ubuntu24-{architecture}.json"
    if (
        constraints.name != expected_constraints
        or resolution.name != expected_resolution
    ):
        raise TrialBundleError("architecture evidence filename is invalid")
    gate = _validate_resolution(
        resolution_bytes=resolution_bytes,
        constraints_bytes=constraints_bytes,
        architecture=architecture,
        inputs=inputs,
    )
    return {
        "constraints": {
            "filename": expected_constraints,
            "sha256": _sha256(constraints_bytes),
        },
        "resolution": {
            "filename": expected_resolution,
            "sha256": _sha256(resolution_bytes),
        },
        "technical_gate": gate,
    }


def _validate_resolution(
    *,
    resolution_bytes: bytes,
    constraints_bytes: bytes,
    architecture: str,
    inputs: _TrialInputs,
) -> dict[str, object]:
    document = _canonical_object(resolution_bytes, f"{architecture} resolution")
    if not isinstance(document, dict) or set(document) != _RESOLUTION_FIELDS:
        raise TrialBundleError("resolution schema is invalid")
    return _validate_resolution_document(
        document=document,
        constraints_bytes=constraints_bytes,
        architecture=architecture,
        build_id=inputs.build_id,
        source_manifest_sha256=inputs.source_manifest_sha256,
        source_sha=inputs.source_sha,
        staging_manifest_sha256=inputs.staging_manifest_sha256,
        version=inputs.version,
        wheel_filename=inputs.wheel_filename,
        wheel_sha256=inputs.wheel_sha256,
    )


def _validate_resolution_document(
    *,
    document: Mapping[str, object],
    constraints_bytes: bytes,
    architecture: str,
    build_id: str,
    source_manifest_sha256: str,
    source_sha: str,
    staging_manifest_sha256: str,
    version: str,
    wheel_filename: str,
    wheel_sha256: str,
) -> dict[str, object]:
    """Bind one captured native closure to the final wheel identity."""
    if document.get("schema") != 2 or document.get("architecture") != architecture:
        raise TrialBundleError("resolution architecture is invalid")
    if architecture not in _ARCHITECTURES:
        raise TrialBundleError("resolution architecture is unsupported")
    identities = {
        "build_id": build_id,
        "source_manifest_sha256": source_manifest_sha256,
        "source_sha": source_sha,
        "staging_manifest_sha256": staging_manifest_sha256,
        "version": version,
    }
    for field, expected in identities.items():
        if document.get(field) != expected:
            raise TrialBundleError("resolution identity does not match the wheel")
    if document.get("os_id") != "ubuntu" or document.get("os_version") != "24.04":
        raise TrialBundleError("resolution host is not Ubuntu 24.04")
    try:
        _trial_resolution.validate_os_runtime(
            document.get("os_runtime"), machine=architecture
        )
    except _trial_resolution.ResolutionError as exc:
        raise TrialBundleError("resolution OS runtime is invalid") from exc
    glibc_version = document.get("glibc_version")
    if not isinstance(glibc_version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+", glibc_version
    ):
        raise TrialBundleError("resolution glibc version is invalid")
    python_version = document.get("python_version")
    if (
        not isinstance(python_version, str)
        or _PYTHON_VERSION.fullmatch(python_version) is None
    ):
        raise TrialBundleError("resolution Python version is invalid")
    pip_version = document.get("pip_version")
    if not isinstance(pip_version, str) or _VERSION.fullmatch(pip_version) is None:
        raise TrialBundleError("resolution pip version is invalid")
    if _required_sha(document.get("constraints_sha256"), "constraints") != _sha256(
        constraints_bytes
    ):
        raise TrialBundleError("resolution constraints digest does not match")
    wheel = _mapping(document.get("wheel"), "resolution wheel")
    _require_keys(wheel, {"filename", "sha256"}, "resolution wheel")
    if wheel.get("filename") != wheel_filename or wheel.get("sha256") != wheel_sha256:
        raise TrialBundleError("resolution wheel does not match the trial wheel")
    phase_artifacts = _verify_resolution_phases(document)
    runtime_artifacts = _runtime_artifact_records(document.get("runtime_artifacts"))
    expected_phase_artifacts = sorted(
        [
            *runtime_artifacts,
            {
                "filename": wheel_filename,
                "name": "gwexpy-studio",
                "sha256": wheel_sha256,
                "version": version,
            },
        ],
        key=lambda record: record["name"].encode("utf-8"),
    )
    if phase_artifacts != expected_phase_artifacts:
        raise TrialBundleError(
            "resolution phase closures do not match the runtime artifacts"
        )
    expected_constraints = _constraints_from_runtime_artifacts(runtime_artifacts)
    if constraints_bytes != expected_constraints:
        raise TrialBundleError("constraints do not match the resolved runtime closure")
    gate = document.get("technical_gate")
    if not isinstance(gate, Mapping):
        raise TrialBundleError("resolution technical gate is invalid")
    try:
        validated_gate = read_gate_result(_canonical_json(gate))
    except GateError as exc:
        raise TrialBundleError("resolution technical gate is invalid") from exc
    installed = validated_gate["installed"]
    if (
        validated_gate["status"] != "passed"
        or validated_gate["architecture"] != architecture
        or validated_gate["python_version"] != python_version
        or not isinstance(installed, Mapping)
        or installed.get("build_id") != build_id
        or installed.get("source_sha") != source_sha
        or installed.get("version") != version
    ):
        raise TrialBundleError("resolution technical gate does not match the wheel")
    return validated_gate


def _verify_resolution_phases(
    document: Mapping[str, object],
) -> list[dict[str, str]]:
    """Require the two fresh installs to describe one identical closure."""
    projections: list[tuple[list[dict[str, str]], dict[str, list[dict[str, str]]]]] = []
    for name in ("phase_one", "phase_two"):
        phase = _mapping(document.get(name), f"resolution {name}")
        _require_keys(
            phase,
            {
                "artifacts",
                "inspect",
                "pip_inspect_sha256",
                "pip_report_sha256",
                "report",
            },
            f"resolution {name}",
        )
        artifacts = _artifact_records(
            phase["artifacts"], f"resolution {name} artifact", allow_studio=True
        )
        inspect = _inspect_projection(phase["inspect"], f"resolution {name} inspect")
        for digest_name in ("pip_inspect_sha256", "pip_report_sha256"):
            _required_sha(phase[digest_name], f"resolution {digest_name}")
        if phase["report"] != {"artifacts": artifacts}:
            raise TrialBundleError("resolution phase report is invalid")
        if _sha256(_canonical_json(phase["report"])) != phase["pip_report_sha256"]:
            raise TrialBundleError("resolution phase report digest is invalid")
        if _sha256(_canonical_json(phase["inspect"])) != phase["pip_inspect_sha256"]:
            raise TrialBundleError("resolution phase inspect digest is invalid")
        projections.append((artifacts, inspect))

    phase_one_artifacts, phase_one_inspect = projections[0]
    phase_two_artifacts, phase_two_inspect = projections[1]
    if (
        phase_one_artifacts != phase_two_artifacts
        or phase_one_inspect != phase_two_inspect
    ):
        raise TrialBundleError(
            "resolution phase closures differ between fresh installs"
        )
    expected_installed = {
        (record["name"], record["version"]) for record in phase_one_artifacts
    }
    installed = {
        (record["name"], record["version"]) for record in phase_one_inspect["installed"]
    }
    if not expected_installed.issubset(installed):
        raise TrialBundleError("resolution phase closures omit a resolved artifact")
    return phase_one_artifacts


def _artifact_records(
    value: object, label: str, *, allow_studio: bool
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise TrialBundleError(f"{label}s are invalid")
    records: list[dict[str, str]] = []
    names: list[str] = []
    for item in value:
        record = _mapping(item, label)
        _require_keys(record, {"filename", "name", "sha256", "version"}, label)
        name = _required_string(record["name"], f"{label} name")
        version = _required_string(record["version"], f"{label} version")
        if (
            _PACKAGE_NAME.fullmatch(name) is None
            or _VERSION.fullmatch(version) is None
            or (not allow_studio and name == "gwexpy-studio")
        ):
            raise TrialBundleError(f"{label} is invalid")
        filename = _required_filename(record["filename"], f"{label} filename")
        if not filename.endswith(".whl"):
            raise TrialBundleError(f"{label} is not a wheel")
        records.append(
            {
                "filename": filename,
                "name": name,
                "sha256": _required_sha(record["sha256"], label),
                "version": version,
            }
        )
        names.append(name)
    if names != sorted(names, key=lambda name: name.encode("utf-8")) or len(
        names
    ) != len(set(names)):
        raise TrialBundleError(f"{label}s are not uniquely sorted")
    return records


def _inspect_projection(value: object, label: str) -> dict[str, list[dict[str, str]]]:
    inspect = _mapping(value, label)
    _require_keys(inspect, {"installed"}, label)
    installed_value = inspect["installed"]
    if not isinstance(installed_value, list):
        raise TrialBundleError(f"{label} installed packages are invalid")
    installed: list[dict[str, str]] = []
    names: list[str] = []
    for item in installed_value:
        package = _mapping(item, f"{label} package")
        _require_keys(package, {"name", "version"}, f"{label} package")
        name = _required_string(package["name"], f"{label} package name")
        version = _required_string(package["version"], f"{label} package version")
        if _PACKAGE_NAME.fullmatch(name) is None or _VERSION.fullmatch(version) is None:
            raise TrialBundleError(f"{label} package is invalid")
        installed.append({"name": name, "version": version})
        names.append(name)
    if names != sorted(names, key=lambda name: name.encode("utf-8")) or len(
        names
    ) != len(set(names)):
        raise TrialBundleError(f"{label} packages are not uniquely sorted")
    return {"installed": installed}


def _constraints_from_runtime_artifacts(value: object) -> bytes:
    records = _runtime_artifact_records(value)
    return "".join(
        f"{record['name']}=={record['version']}\n" for record in records
    ).encode("utf-8")


def _runtime_artifact_records(value: object) -> list[dict[str, str]]:
    return _artifact_records(value, "runtime artifact", allow_studio=False)


def _verify_architecture_bundle_record(
    *,
    architecture: str,
    record: object,
    files: Mapping[str, bytes],
    build_id: str,
    source_manifest_sha256: str,
    source_sha: str,
    staging_manifest_sha256: str,
    version: str,
    wheel_filename: str,
    wheel_sha256: str,
) -> None:
    payload = _mapping(record, f"{architecture} bundle record")
    _require_keys(
        payload, {"constraints", "resolution", "technical_gate"}, "bundle record"
    )
    constraints = _mapping(payload["constraints"], "bundle constraints")
    resolution = _mapping(payload["resolution"], "bundle resolution")
    _require_keys(constraints, {"filename", "sha256"}, "bundle constraints")
    _require_keys(resolution, {"filename", "sha256"}, "bundle resolution")
    expected_constraints = f"constraints-ubuntu24-{architecture}.txt"
    expected_resolution = f"resolution-ubuntu24-{architecture}.json"
    if (
        constraints.get("filename") != expected_constraints
        or resolution.get("filename") != expected_resolution
    ):
        raise TrialBundleError("bundle architecture filenames are invalid")
    constraints_bytes = files.get(expected_constraints)
    resolution_bytes = files.get(expected_resolution)
    if constraints_bytes is None or resolution_bytes is None:
        raise TrialBundleError("bundle architecture evidence is missing")
    if _required_sha(constraints["sha256"], "bundle constraints") != _sha256(
        constraints_bytes
    ):
        raise TrialBundleError("bundle constraints digest is invalid")
    if _required_sha(resolution["sha256"], "bundle resolution") != _sha256(
        resolution_bytes
    ):
        raise TrialBundleError("bundle resolution digest is invalid")
    document = _canonical_object(resolution_bytes, "bundle resolution")
    if not isinstance(document, dict) or set(document) != _RESOLUTION_FIELDS:
        raise TrialBundleError("bundle resolution schema is invalid")
    gate = _validate_resolution_document(
        document=document,
        constraints_bytes=constraints_bytes,
        architecture=architecture,
        build_id=build_id,
        source_manifest_sha256=source_manifest_sha256,
        source_sha=source_sha,
        staging_manifest_sha256=staging_manifest_sha256,
        version=version,
        wheel_filename=wheel_filename,
        wheel_sha256=wheel_sha256,
    )
    if payload["technical_gate"] != gate:
        raise TrialBundleError("bundle technical gate is not the resolution gate")


def _verify_quick_start_records(
    value: object,
    source_manifest: ReleaseSourceManifest,
    files: Mapping[str, bytes],
) -> None:
    quick_start = _mapping(value, "Quick Start record")
    if set(quick_start) != {"en", "ja"}:
        raise TrialBundleError("Quick Start records are incomplete")
    expected = {
        "en": ("Quick-Start.md", "docs/Quick-Start.md"),
        "ja": ("Quick-Start.ja.md", "docs/Quick-Start.ja.md"),
    }
    for language, (filename, source_path) in expected.items():
        record = _mapping(quick_start[language], f"{language} Quick Start")
        _require_keys(record, {"filename", "sha256", "source_path"}, "Quick Start")
        if (
            record.get("filename") != filename
            or record.get("source_path") != source_path
        ):
            raise TrialBundleError("Quick Start record has an unexpected path")
        content = files.get(filename)
        if content is None or not content:
            raise TrialBundleError("Quick Start asset is missing or empty")
        if _required_sha(record["sha256"], "Quick Start") != _sha256(content):
            raise TrialBundleError("Quick Start digest is invalid")
        _verify_quick_start_source(source_manifest, source_path, content)


def _verify_quick_start_source(
    source_manifest: ReleaseSourceManifest, source_path: str, content: bytes
) -> None:
    entries = {entry.path: entry for entry in source_manifest.entries}
    entry = entries.get(source_path)
    if entry is None or entry.file_type != "file" or entry.sha256 != _sha256(content):
        raise TrialBundleError("Quick Start bytes do not come from canonical source")


def _generated_wheel_files(
    wheel_bytes: bytes, *, generated_records: object
) -> dict[str, bytes]:
    if not isinstance(generated_records, list) or len(generated_records) != len(
        _GENERATED_PATHS
    ):
        raise TrialBundleError("generated trial files are invalid")
    paths: list[str] = []
    expected_hashes: dict[str, str] = {}
    for record_value in generated_records:
        record = _mapping(record_value, "generated trial file")
        _require_keys(record, {"path", "sha256"}, "generated trial file")
        path = _required_string(record["path"], "generated trial file path")
        paths.append(path)
        expected_hashes[path] = _required_sha(record["sha256"], "generated trial file")
    if tuple(paths) != _GENERATED_PATHS:
        raise TrialBundleError("generated trial files do not match the approved delta")
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise TrialBundleError("trial wheel contains duplicate members")
            result = {
                path: archive.read(member)
                for path, member in zip(
                    _GENERATED_PATHS, _GENERATED_MEMBERS, strict=True
                )
            }
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise TrialBundleError("trial wheel is missing generated files") from exc
    if any(_sha256(result[path]) != expected_hashes[path] for path in _GENERATED_PATHS):
        raise TrialBundleError("generated trial file digest does not match the wheel")
    return result


def _verify_generated_trial_semantics(
    *,
    wheel_bytes: bytes,
    generated_files: Mapping[str, bytes],
    identity: TrialIdentity,
    source_manifest: ReleaseSourceManifest,
) -> None:
    """Require installed identity and static policy bytes to agree with final TRIAL."""
    _trial_resolution._verify_wheel_stamp(
        wheel_bytes,
        build_id=identity.build_id,
        source_manifest_sha256=manifest_digest(source_manifest),
        source_sha=identity.source_sha,
        version=identity.version,
    )
    _trial_resolution._verify_generated_file_semantics(
        generated_files, identity.version
    )
    source_entries = {entry.path: entry for entry in source_manifest.entries}
    policy_entry = source_entries.get(_POLICY_INPUT_PATH)
    if (
        policy_entry is None
        or policy_entry.file_type != "file"
        or policy_entry.sha256 != _sha256(generated_files[_GENERATED_PATHS[2]])
    ):
        raise _trial_resolution.ResolutionError(
            "generated I/O capability policy does not match source M"
        )


def _reconstruct_staging_manifest(
    source_manifest: ReleaseSourceManifest, generated_files: Mapping[str, bytes]
) -> ReleaseSourceManifest:
    entries = {entry.path: entry for entry in source_manifest.entries}
    version = entries.get(_GENERATED_PATHS[0])
    if version is None or version.file_type != "file":
        raise TrialBundleError("source manifest is missing the package version file")
    for path, content in generated_files.items():
        existing = entries.get(path)
        if path != _GENERATED_PATHS[0] and existing is not None:
            raise TrialBundleError("source contains a generated trial file")
        entries[path] = ManifestEntry(
            path=path,
            file_type="file",
            mode=version.mode if path == _GENERATED_PATHS[0] else "0644",
            sha256=_sha256(content),
        )
    return ReleaseSourceManifest(
        entries=tuple(
            sorted(entries.values(), key=lambda entry: entry.path.encode("utf-8"))
        )
    )


def _preliminary_manifest_bytes(
    *,
    build_id: str,
    generated_files: Mapping[str, bytes],
    source_manifest_sha256: str,
    source_sha: str,
    staging_manifest_sha256: str,
    version: str,
    wheel_filename: str,
    wheel_sha256: str,
) -> bytes:
    return _canonical_json(
        {
            "build_id": build_id,
            "generated_files": _generated_records(generated_files),
            "schema": 1,
            "source_manifest_sha256": source_manifest_sha256,
            "source_sha": source_sha,
            "staging_manifest_sha256": staging_manifest_sha256,
            "version": version,
            "wheel_filename": wheel_filename,
            "wheel_sha256": wheel_sha256,
        }
    )


def _generated_records(generated_files: Mapping[str, bytes]) -> list[dict[str, str]]:
    if tuple(generated_files) != _GENERATED_PATHS:
        raise TrialBundleError("generated trial files are not in canonical order")
    return [
        {"path": path, "sha256": _sha256(generated_files[path])}
        for path in _GENERATED_PATHS
    ]


def _workflow_identity(
    *,
    repository: str,
    workflow_path: str,
    run_id: int,
    run_number: int,
    run_attempt: int,
    build_id: str,
) -> dict[str, object]:
    if (
        type(repository) is not str
        or _REPOSITORY.fullmatch(repository) is None
        or workflow_path != ".github/workflows/build-trial-wheel.yml"
        or any(
            type(value) is not int or value < 1
            for value in (run_id, run_number, run_attempt)
        )
    ):
        raise TrialBundleError("build workflow identity is invalid")
    match = _BUILD_ID.fullmatch(build_id)
    if (
        match is None
        or int(match["run"]) != run_number
        or int(match["attempt"]) != run_attempt
    ):
        raise TrialBundleError("build workflow run does not match the build ID")
    return {
        "repository": repository,
        "run_attempt": run_attempt,
        "run_id": run_id,
        "run_number": run_number,
        "workflow_path": workflow_path,
    }


def _validate_workflow_record(
    workflow: Mapping[str, object], *, run: int, attempt: int
) -> None:
    _workflow_identity(
        repository=_required_string(workflow["repository"], "workflow repository"),
        workflow_path=_required_string(workflow["workflow_path"], "workflow path"),
        run_id=_required_positive_integer(workflow["run_id"], "workflow run ID"),
        run_number=_required_positive_integer(
            workflow["run_number"], "workflow run number"
        ),
        run_attempt=_required_positive_integer(
            workflow["run_attempt"], "workflow run attempt"
        ),
        build_id=f"P-{'0' * 7}-20000101-r{run}-a{attempt}",
    )


def _validate_identity(build_id: str, source_sha: str, version: str) -> tuple[int, int]:
    build_match = _BUILD_ID.fullmatch(build_id)
    version_match = _TRIAL_VERSION.fullmatch(version)
    if (
        build_match is None
        or version_match is None
        or _SOURCE_SHA.fullmatch(source_sha) is None
    ):
        raise TrialBundleError("trial identity is invalid")
    try:
        datetime.strptime(build_match["date"], "%Y%m%d")
    except ValueError as exc:
        raise TrialBundleError("trial build date is invalid") from exc
    if (
        build_match["sha"] != source_sha[:7]
        or version_match["sha"] != build_match["sha"]
        or version_match["date"] != build_match["date"]
        or version_match["run"] != build_match["run"]
        or version_match["attempt"] != build_match["attempt"]
    ):
        raise TrialBundleError("trial identity fields disagree")
    return int(build_match["run"]), int(build_match["attempt"])


def _manifest_from_bytes(content: bytes) -> ReleaseSourceManifest:
    document = _canonical_object(content, "source manifest")
    if not isinstance(document, dict) or set(document) != {"entries", "schema"}:
        raise TrialBundleError("source manifest schema is invalid")
    if document["schema"] != 1 or not isinstance(document["entries"], list):
        raise TrialBundleError("source manifest schema is invalid")
    entries: list[ManifestEntry] = []
    paths: list[str] = []
    for value in document["entries"]:
        record = _mapping(value, "source manifest entry")
        _require_keys(
            record,
            {"file_type", "mode", "path", "sha256", "symlink_target"},
            "source manifest entry",
        )
        path = _required_string(record["path"], "source manifest path")
        _validate_relative_path(path)
        file_type = record["file_type"]
        mode = record["mode"]
        digest = record["sha256"]
        target = record["symlink_target"]
        if (
            file_type not in {"directory", "file"}
            or mode not in {"0644", "0755"}
            or not isinstance(digest, str)
            or target != ""
        ):
            raise TrialBundleError("source manifest entry is invalid")
        if (file_type == "file" and _HEX_SHA256.fullmatch(digest) is None) or (
            file_type == "directory" and digest != ""
        ):
            raise TrialBundleError("source manifest entry digest is invalid")
        entries.append(
            ManifestEntry(
                path=path,
                file_type=file_type,
                mode=mode,
                sha256=digest,
                symlink_target="",
            )
        )
        paths.append(path)
    if paths != sorted(paths, key=lambda path: path.encode("utf-8")) or len(
        paths
    ) != len(set(paths)):
        raise TrialBundleError("source manifest entries are not canonical")
    manifest = ReleaseSourceManifest(tuple(entries))
    if manifest.to_bytes() != content:
        raise TrialBundleError("source manifest is not canonical")
    return manifest


def _manifest_bytes(manifest: ReleaseSourceManifest) -> bytes:
    return manifest.to_bytes()


def _canonical_object(content: bytes, label: str) -> object:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise TrialBundleError(f"{label} is not valid JSON") from exc
    if _canonical_json(value) != content:
        raise TrialBundleError(f"{label} is not canonical JSON")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON field")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value!r}")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _checksums(files: Mapping[str, bytes]) -> bytes:
    if not files or "SHA256SUMS" in files:
        raise TrialBundleError("checksum inputs are invalid")
    return b"".join(
        f"{_sha256(files[name])}  {name}\n".encode("ascii")
        for name in sorted(files, key=lambda name: name.encode("utf-8"))
    )


def _validated_file_mapping(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(files, Mapping):
        raise TrialBundleError("bundle assets are invalid")
    result = dict(files)
    if any(
        not isinstance(name, str) or not isinstance(content, bytes)
        for name, content in result.items()
    ):
        raise TrialBundleError("bundle assets are invalid")
    if any(not name or Path(name).name != name for name in result):
        raise TrialBundleError("bundle asset name is unsafe")
    trial = result.get("TRIAL-MANIFEST.json")
    if trial is None:
        raise TrialBundleError("bundle trial manifest is missing")
    document = _canonical_object(trial, "trial manifest")
    if not isinstance(document, Mapping):
        raise TrialBundleError("bundle trial manifest is invalid")
    wheel = document.get("wheel")
    if not isinstance(wheel, Mapping) or not isinstance(wheel.get("filename"), str):
        raise TrialBundleError("bundle wheel record is invalid")
    expected = set(_ASSET_NAMES) | {wheel["filename"]}
    if set(result) != expected:
        raise TrialBundleError("bundle has missing or unexpected assets")
    return result


def _read_regular_bytes(path: Path, label: str) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise TrialBundleError(f"{label} cannot be opened safely") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise TrialBundleError(f"{label} is not a regular file")
        content = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            content.extend(chunk)
        final = os.fstat(descriptor)
        if (
            initial.st_dev != final.st_dev
            or initial.st_ino != final.st_ino
            or initial.st_size != final.st_size
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
        ):
            raise TrialBundleError(f"{label} changed while being read")
        return bytes(content)
    finally:
        os.close(descriptor)


def _publish_bundle(files: Mapping[str, bytes], output_directory: Path) -> None:
    output = Path(os.path.abspath(output_directory))
    if not output.name or output.name in {".", ".."}:
        raise TrialBundleError("bundle output directory is invalid")
    parent_descriptor = _open_bundle_parent(output)
    stage_name = ""
    stage_descriptor: int | None = None
    published = False
    published_status: os.stat_result | None = None
    try:
        stage_name, stage_descriptor = _create_output_stage(
            parent_descriptor, output.name
        )
        _assert_output_parent_path_matches_descriptor(output.parent, parent_descriptor)
        published_status = _write_artifacts_to_stage(files, stage_descriptor)
        _assert_output_parent_path_matches_descriptor(output.parent, parent_descriptor)
        _assert_output_stage_bound(parent_descriptor, stage_name, stage_descriptor)
        _rename_no_replace(
            stage_descriptor, "artifacts", parent_descriptor, output.name
        )
        published = True
        _assert_output_parent_path_matches_descriptor(output.parent, parent_descriptor)
        _assert_published_artifacts(
            parent_descriptor,
            output.name,
            published_status,
            set(files),
        )
    except (OSError, TrialBuildError) as exc:
        raise TrialBundleError("trial bundle could not be published") from exc
    finally:
        if stage_descriptor is not None:
            if published:
                _remove_empty_output_stage(
                    parent_descriptor, stage_name, stage_descriptor
                )
            else:
                _cleanup_output_stage(parent_descriptor, stage_name, stage_descriptor)
            os.close(stage_descriptor)
        os.close(parent_descriptor)


def _open_bundle_parent(output: Path) -> int:
    try:
        descriptor = os.open(output.parent, _directory_open_flags())
    except (OSError, TrialBuildError) as exc:
        raise TrialBundleError("bundle output parent cannot be opened safely") from exc
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            raise TrialBundleError("bundle output parent is not a directory")
        try:
            os.stat(output.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return descriptor
        raise TrialBundleError("bundle output already exists")
    except Exception:
        os.close(descriptor)
        raise


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TrialBundleError(f"{label} is invalid")
    return value


def _require_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise TrialBundleError(f"{label} schema is invalid")


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TrialBundleError(f"{label} is invalid")
    return value


def _required_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None:
        raise TrialBundleError(f"{label} digest is invalid")
    return value


def _required_source_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SOURCE_SHA.fullmatch(value) is None:
        raise TrialBundleError(f"{label} is invalid")
    return value


def _required_filename(value: object, label: str) -> str:
    filename = _required_string(value, label)
    if Path(filename).name != filename or filename in {".", ".."}:
        raise TrialBundleError(f"{label} is unsafe")
    return filename


def _required_positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise TrialBundleError(f"{label} is invalid")
    return value


def _validate_relative_path(path: str) -> None:
    candidate = Path(path)
    if (
        not path
        or "\\" in path
        or candidate.is_absolute()
        or candidate.as_posix() != path
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise TrialBundleError("source manifest path is unsafe")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--trial-manifest", required=True, type=Path)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--staging-manifest", required=True, type=Path)
    parser.add_argument("--constraints-x86-64", required=True, type=Path)
    parser.add_argument("--resolution-x86-64", required=True, type=Path)
    parser.add_argument("--constraints-aarch64", required=True, type=Path)
    parser.add_argument("--resolution-aarch64", required=True, type=Path)
    parser.add_argument("--quick-start", required=True, type=Path)
    parser.add_argument("--quick-start-ja", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--build-workflow-path", required=True)
    parser.add_argument("--build-run-id", required=True, type=int)
    parser.add_argument("--build-run-number", required=True, type=int)
    parser.add_argument("--build-run-attempt", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Assemble exactly one fresh final bundle from explicit input files."""
    arguments = _parser().parse_args(argv)
    try:
        result = assemble_trial_bundle(
            output_directory=arguments.output,
            wheel=arguments.wheel,
            preliminary_trial_manifest=arguments.trial_manifest,
            source_manifest=arguments.source_manifest,
            staging_manifest=arguments.staging_manifest,
            constraints_x86_64=arguments.constraints_x86_64,
            resolution_x86_64=arguments.resolution_x86_64,
            constraints_aarch64=arguments.constraints_aarch64,
            resolution_aarch64=arguments.resolution_aarch64,
            quick_start=arguments.quick_start,
            quick_start_ja=arguments.quick_start_ja,
            repository=arguments.repository,
            build_workflow_path=arguments.build_workflow_path,
            build_run_id=arguments.build_run_id,
            build_run_number=arguments.build_run_number,
            build_run_attempt=arguments.build_run_attempt,
        )
    except (OSError, TrialBundleError) as exc:
        print(f"assemble_trial_bundle: error: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover - direct CLI invocation.
    raise SystemExit(main())
