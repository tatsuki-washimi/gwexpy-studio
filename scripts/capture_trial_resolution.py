"""Capture one architecture's reproducible binary-only trial runtime closure."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import venv
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

try:
    # Reuse Task2's retained-FD publication primitives so both M2 artifacts
    # share the same Linux no-replace boundary rather than drifting apart.
    from .build_trial_wheel import (
        _TRIAL_CAPABILITY_POLICY as _APPROVED_TRIAL_CAPABILITY_POLICY,
    )
    from .build_trial_wheel import (
        TrialBuildError as _TrialBuildError,
    )
    from .build_trial_wheel import (
        TrialIdentity as _TrialIdentity,
    )
    from .build_trial_wheel import (
        _assert_output_parent_path_matches_descriptor,
        _assert_output_stage_bound,
        _assert_published_artifacts,
        _cleanup_output_stage,
        _create_output_stage,
        _open_output_parent,
        _remove_empty_output_stage,
        _rename_no_replace,
        _write_artifacts_to_stage,
        verify_staging_delta,
    )
    from .build_trial_wheel import (
        _external_temporary_parent as _trial_temporary_parent,
    )
    from .build_trial_wheel import _git_bytes as _trial_git_bytes
    from .build_trial_wheel import (
        _output_directory as _trial_output_directory,
    )
    from .build_trial_wheel import (
        _source_policy as _trial_source_policy,
    )
    from .build_trial_wheel import (
        _source_root as _trial_source_root,
    )
    from .build_trial_wheel import (
        _verify_selected_source_allowlist as _verify_trial_source_allowlist,
    )
    from .build_trial_wheel import (
        _verify_source_git_identity as _verify_trial_source_git_identity,
    )
    from .build_trial_wheel import _verify_wheel_bytes as _verify_trial_wheel_bytes
    from .release_source_manifest import (
        ReleaseSourceError,
        manifest_digest,
        read_manifest,
    )
    from .run_trial_technical_gate import (
        _CHECK_NAMES as _CURRENT_GATE_CHECK_NAMES,
    )
    from .run_trial_technical_gate import (
        GateError as _GateError,
    )
    from .run_trial_technical_gate import _new_shm_run_prefix
    from .run_trial_technical_gate import (
        gate_environment as _technical_gate_environment,
    )
    from .run_trial_technical_gate import (
        read_gate_result as _read_gate_result,
    )
    from .run_trial_technical_gate import read_gate_stage as _read_gate_stage
    from .run_trial_technical_gate import (
        technical_gate_command as _technical_gate_command,
    )
    from .trial_targets import TrialTargetError as _TrialTargetError
    from .trial_targets import target_ids as _target_ids
    from .trial_targets import trial_target as _trial_target
    from .trial_toolchain import (
        ToolchainError as _ToolchainError,
    )
    from .trial_toolchain import (
        conda_environment_manager_record as _conda_environment_manager_record,
    )
    from .trial_toolchain import (
        conda_environment_manager_record_from_info as _conda_manager_from_info,
    )
    from .trial_toolchain import (
        conda_subdir as _conda_subdir,
    )
    from .trial_toolchain import (
        fresh_conda_create_command as _fresh_conda_create_command,
    )
    from .verify_public_source import scan_public_checkout
except ImportError:  # pragma: no cover - direct public-script invocation.
    # mypy analyzes both import surfaces; each fallback binds the same public
    # symbol only when the package-relative import above is unavailable.
    from build_trial_wheel import (  # type: ignore[no-redef]
        _TRIAL_CAPABILITY_POLICY as _APPROVED_TRIAL_CAPABILITY_POLICY,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        TrialBuildError as _TrialBuildError,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        TrialIdentity as _TrialIdentity,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _assert_output_parent_path_matches_descriptor,
        _assert_output_stage_bound,
        _assert_published_artifacts,
        _cleanup_output_stage,
        _create_output_stage,
        _open_output_parent,
        _remove_empty_output_stage,
        _rename_no_replace,
        _write_artifacts_to_stage,
        verify_staging_delta,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _external_temporary_parent as _trial_temporary_parent,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _git_bytes as _trial_git_bytes,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _output_directory as _trial_output_directory,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _source_policy as _trial_source_policy,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _source_root as _trial_source_root,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _verify_selected_source_allowlist as _verify_trial_source_allowlist,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _verify_source_git_identity as _verify_trial_source_git_identity,
    )
    from build_trial_wheel import (  # type: ignore[no-redef]
        _verify_wheel_bytes as _verify_trial_wheel_bytes,
    )
    from release_source_manifest import (  # type: ignore[no-redef]
        ReleaseSourceError,
        manifest_digest,
        read_manifest,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        _CHECK_NAMES as _CURRENT_GATE_CHECK_NAMES,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        GateError as _GateError,
    )
    from run_trial_technical_gate import _new_shm_run_prefix
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        gate_environment as _technical_gate_environment,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        read_gate_result as _read_gate_result,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        read_gate_stage as _read_gate_stage,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        technical_gate_command as _technical_gate_command,
    )
    from trial_targets import (  # type: ignore[no-redef]
        TrialTargetError as _TrialTargetError,
    )
    from trial_targets import (
        target_ids as _target_ids,
    )
    from trial_targets import (
        trial_target as _trial_target,
    )
    from trial_toolchain import (  # type: ignore[no-redef]
        ToolchainError as _ToolchainError,
    )
    from trial_toolchain import (
        conda_environment_manager_record as _conda_environment_manager_record,
    )
    from trial_toolchain import (
        conda_environment_manager_record_from_info as _conda_manager_from_info,
    )
    from trial_toolchain import (
        conda_subdir as _conda_subdir,
    )
    from trial_toolchain import (
        fresh_conda_create_command as _fresh_conda_create_command,
    )
    from verify_public_source import scan_public_checkout  # type: ignore[no-redef]


class ResolutionError(RuntimeError):
    """Raised when a trial runtime closure cannot be bound safely."""


@dataclass(frozen=True, slots=True)
class TrialArtifact:
    """The wheel identity jointly vouched for by M and the trial manifest."""

    build_id: str
    source_manifest_sha256: str
    staging_manifest_sha256: str
    source_sha: str
    trial_manifest_sha256: str
    version: str
    wheel_filename: str
    wheel_sha256: str
    wheel_bytes: bytes


@dataclass(frozen=True, slots=True)
class RuntimeArtifact:
    """One binary selected by pip, stripped to reproducible public identity."""

    name: str
    version: str
    filename: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        """Return the bounded persisted representation."""
        return {
            "filename": self.filename,
            "name": self.name,
            "sha256": self.sha256,
            "version": self.version,
        }


_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}")
_BUILD_ID = re.compile(
    r"P-(?P<sha>[0-9a-f]{7})-(?P<date>[0-9]{8})-"
    r"r(?P<run>[1-9][0-9]*)-a(?P<attempt>[1-9][0-9]*)"
)
_TRIAL_VERSION = re.compile(
    r"0\.1\.0a1\+trial\.p\.g(?P<sha>[0-9a-f]{7})\."
    r"(?P<date>[0-9]{8})\.r(?P<run>[1-9][0-9]*)\.a(?P<attempt>[1-9][0-9]*)"
)
_WHEEL_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*\.whl")
_PACKAGE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*")
_PYTHON_VERSION = re.compile(r"3\.12\.[0-9]+")
_GLIBC_VERSION = re.compile(r"[0-9]+\.[0-9]+")
_DEBIAN_VERSION = re.compile(r"[0-9][A-Za-z0-9.+:~_-]*")
_MACHINES = frozenset({"x86_64", "aarch64", "arm64"})
_REQUIRED_OS_ID = "ubuntu"
_REQUIRED_OS_VERSION = "24.04"
_STUDIO_NAME = "gwexpy-studio"
_OS_RUNTIME_MANAGER = "dpkg"
_OS_RUNTIME_PACKAGES = ("libegl1", "libgl1")
_SCHEMA4_OS_RUNTIME_PACKAGES = (
    "libegl1",
    "libgl1",
    "libfontconfig1",
    "libglib2.0-0t64",
    "libdbus-1-3",
    "libxkbcommon0",
    "libzstd1",
)
_OS_RUNTIME_STATUS = "installed"
_DPKG_ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}
_REQUIRED_QT_GL_LIBRARIES = ("libEGL.so.1", "libGL.so.1")
_DPKG_QUERY_FORMAT = (
    "${db:Status-Abbrev}\\t${Package}\\t${Version}\\t${Architecture}\\n"
)
_GENERATED_WHEEL_MEMBERS = (
    ("src/gwexpy_studio/_version.py", "gwexpy_studio/_version.py"),
    (
        "src/gwexpy_studio/assets/trial-build.json",
        "gwexpy_studio/assets/trial-build.json",
    ),
    (
        "src/gwexpy_studio/assets/io-capabilities.json",
        "gwexpy_studio/assets/io-capabilities.json",
    ),
)
_GATE_SCRIPT_GIT_PATH = "scripts/run_trial_technical_gate.py"
_SEALED_GATE_SCRIPT_NAME = "installed-trial-technical-gate.py"
_PRODUCER_GATE_STAGES = frozenset(
    {
        "bootstrap",
        "launcher",
        "welcome",
        "sample-availability",
        "try-sample",
        "sample-catalog",
        "sample-inspection",
        "sample-read",
        "crop-dialog",
        "crop",
        "asd-dialog",
        "asd",
        "save-project",
        "post-save-input",
        "post-save-crop-dialog",
        "recovery-checkpoint",
        "export-reference",
        "python-export",
        "intentional-crash",
    }
)
_CONSUMER_GATE_STAGES = frozenset(
    {
        "bootstrap",
        "launcher",
        "project-reopen",
        "recovery-review",
        "recovery-candidate-visible",
        "recovery-candidate-selected",
        "unsaved-project-visible",
        "unsaved-project-discarded",
        "recovery-review-trigger",
        "recovery-review-requested",
        "recovery-list-dispatch-requested",
        "recovery-list-dispatch-accepted",
        "recovery-list-result-received",
        "recovery-list-result-succeeded",
        "recovery-candidates-received",
        "recovery-restored",
        "data-restore",
        "recovery-consumption",
        "restored-project-save",
        "worker-exit",
        "complete",
        "recovery-dialog-boundary-entered",
        "recovery-dialog-instance-bound",
        "recovery-dialog-poll-entered",
        "recovery-dialog-button-resolved",
        "recovery-dialog-modal-returned",
    }
)
_NORMAL_GATE_STAGES = frozenset(
    {
        "bootstrap",
        "launcher",
        "welcome",
        "worker-ready",
        "io-catalog",
        "io-catalog-ready",
        "io-inspection",
        "io-read",
        "io-read-settled",
        "save-project",
        "save-after-refusal",
        "close-project",
        "empty-workspace",
        "open-project",
        "reopened-project",
        "restore-review",
        "restore-review-handled",
        "restored-project",
        "restored-state-settled",
        "io-unavailable",
        "io-refusal",
        "worker-exit",
        "complete",
    }
)
_ALLOWED_GATE_STAGE_PAIRS = frozenset(
    {
        *(f"producer/{stage}" for stage in _PRODUCER_GATE_STAGES),
        *(f"consumer/{stage}" for stage in _CONSUMER_GATE_STAGES),
        *(f"normal/{stage}" for stage in _NORMAL_GATE_STAGES),
    }
)
_SCHEMA4_COMMON_FIELDS = {
    "architecture",
    "build_id",
    "constraints_sha256",
    "environment_manager",
    "phase_one",
    "phase_replay",
    "phase_two",
    "pip_version",
    "python_version",
    "runtime_artifacts",
    "schema",
    "source_manifest_sha256",
    "source_sha",
    "staging_manifest_sha256",
    "target_id",
    "technical_gate",
    "version",
    "wheel",
}
_SCHEMA4_LINUX_FIELDS = _SCHEMA4_COMMON_FIELDS | {
    "glibc_version",
    "os_id",
    "os_runtime",
    "os_version",
}
_SCHEMA4_MACOS_FIELDS = _SCHEMA4_COMMON_FIELDS | {"platform"}
_CONDA_MANAGER_FIELDS = {
    "kind",
    "requested_specs",
    "subdir",
    "version",
}
_CONDA_PACKAGE_FIELDS = {"build", "name", "version"}
_CONDA_PACKAGE_NAME = re.compile(r"_?[a-z0-9][a-z0-9_.+-]*")
_CONDA_BUILD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
_CONDA_CHANNEL = "conda-forge"
_CONDA_PYPI_MARKERS = ("pypi", "pypi", "pypi_0")


def load_trial_artifact(
    *,
    wheel: Path,
    trial_manifest: Path,
    source_manifest: Path,
    staging_manifest: Path | None = None,
) -> TrialArtifact:
    """Verify that wheel, generated stamp, trial record, and M form one unit."""
    source = read_manifest(source_manifest)
    source_digest = manifest_digest(source)
    trial_bytes = _read_regular_bytes(trial_manifest, "trial manifest")
    trial = _parse_json_bytes(trial_bytes, "trial manifest")
    _require_exact_keys(
        trial,
        {
            "build_id",
            "generated_files",
            "schema",
            "source_manifest_sha256",
            "source_sha",
            "staging_manifest_sha256",
            "version",
            "wheel_filename",
            "wheel_sha256",
        },
        "trial manifest",
    )
    if trial["schema"] != 1:
        raise ResolutionError("trial manifest schema is unsupported")
    source_manifest_sha256 = _required_sha256(
        trial["source_manifest_sha256"], "trial source manifest"
    )
    if source_manifest_sha256 != source_digest:
        raise ResolutionError("trial source manifest digest does not match M")
    staging = read_manifest(
        trial_manifest.with_name("STAGING-MANIFEST.json")
        if staging_manifest is None
        else staging_manifest
    )
    staging_manifest_sha256 = _required_sha256(
        trial["staging_manifest_sha256"], "trial staging manifest"
    )
    if staging_manifest_sha256 != manifest_digest(staging):
        raise ResolutionError("trial staging manifest digest does not match staging")
    source_sha = _required_source_sha(trial["source_sha"])
    build_id = _required_string(trial["build_id"], "trial build ID")
    version = _required_string(trial["version"], "trial version")
    _validate_trial_identity(build_id, source_sha, version)
    wheel_filename = _required_wheel_filename(trial["wheel_filename"])
    if wheel.name != wheel_filename:
        raise ResolutionError("trial wheel filename does not match its manifest")
    if not wheel_filename.endswith("-py3-none-any.whl"):
        raise ResolutionError("trial wheel filename must be py3-none-any")
    wheel_bytes = _read_regular_bytes(wheel, "trial wheel")
    wheel_sha256 = _required_sha256(trial["wheel_sha256"], "trial wheel")
    if _sha256(wheel_bytes) != wheel_sha256:
        raise ResolutionError("trial wheel digest does not match its manifest")
    _verify_wheel_stamp(
        wheel_bytes,
        build_id=build_id,
        source_manifest_sha256=source_manifest_sha256,
        source_sha=source_sha,
        version=version,
    )
    generated_files = _verify_generated_wheel_members(
        wheel_bytes, trial["generated_files"]
    )
    _verify_generated_file_semantics(generated_files, version)
    try:
        verify_staging_delta(
            source_manifest=source,
            staging_manifest=staging,
            generated_files=generated_files,
        )
    except _TrialBuildError as exc:
        raise ResolutionError("trial staging delta is not approved") from exc
    try:
        _verify_trial_wheel_bytes(
            wheel_bytes,
            _TrialIdentity(
                source_sha=source_sha,
                build_id=build_id,
                version=version,
            ),
            generated_files,
            staging,
        )
    except _TrialBuildError as exc:
        raise ResolutionError(
            "trial wheel payload or metadata is not approved"
        ) from exc
    return TrialArtifact(
        build_id=build_id,
        source_manifest_sha256=source_manifest_sha256,
        staging_manifest_sha256=staging_manifest_sha256,
        source_sha=source_sha,
        trial_manifest_sha256=_sha256(trial_bytes),
        version=version,
        wheel_filename=wheel_filename,
        wheel_sha256=wheel_sha256,
        wheel_bytes=wheel_bytes,
    )


def runtime_constraints(
    report: Mapping[str, object], *, artifact: TrialArtifact
) -> str:
    """Render sorted exact pins for every resolved runtime dependency but Studio."""
    selected = _selected_artifacts(report, artifact=artifact)
    runtime = tuple(item for item in selected if item.name != _STUDIO_NAME)
    return "".join(f"{item.name}=={item.version}\n" for item in runtime)


def run_installed_technical_gate(
    *,
    python: Path,
    gate_fd: int,
    checkout_root: Path,
    work_root: Path,
    native_qt: bool = False,
    replay_python: Path | None = None,
    legacy: bool = False,
) -> dict[str, object]:
    """Run the normal launcher path from phase two through a sealed FD only."""
    try:
        if work_root.is_symlink() or not work_root.is_dir():
            raise ResolutionError("technical-gate work root is unsafe")
        if type(gate_fd) is not int or gate_fd < 3:
            raise ResolutionError("technical-gate descriptor is unsafe")
        descriptor_status = os.fstat(gate_fd)
        if not stat.S_ISREG(descriptor_status.st_mode):
            raise ResolutionError("technical-gate descriptor is unsafe")
        result_path = work_root / "technical-gate.json"
        if result_path.exists() or result_path.is_symlink():
            raise ResolutionError("technical-gate result path is not fresh")
        environment = _technical_gate_environment(
            work_root,
            _subprocess_environment(),
            _new_shm_run_prefix(),
            native_qt=native_qt,
        )
        completed = subprocess.run(
            _technical_gate_command(
                python=python,
                gate_fd=gate_fd,
                checkout_root=checkout_root,
                work_root=work_root,
                result_path=result_path,
                replay_python=replay_python,
                native_qt=native_qt,
                legacy=legacy,
            ),
            cwd=work_root,
            capture_output=True,
            check=False,
            text=False,
            timeout=300,
            env=environment,
            pass_fds=(gate_fd,),
        )
    except (OSError, _GateError) as exc:
        raise ResolutionError("installed technical gate could not run") from exc
    if completed.returncode != 0:
        try:
            stage = _collect_gate_stage(work_root)
        except _GateError:
            raise ResolutionError("installed technical gate failed") from None
        raise ResolutionError(f"installed technical gate failed at {stage}")
    try:
        result = _read_gate_result(
            _read_regular_bytes(result_path, "technical-gate result")
        )
    except _GateError as exc:
        raise ResolutionError("installed technical gate result is invalid") from exc
    if result["status"] != "passed":
        raise ResolutionError("installed technical gate failed")
    return result


def _collect_gate_stage(work_root: Path) -> str:
    """Collect only a canonical stage pair from the sealed gate workspace."""
    stage = _read_gate_stage(work_root)
    if stage not in _ALLOWED_GATE_STAGE_PAIRS:
        raise _GateError("technical-gate diagnostic stage is invalid")
    return stage


def verify_capture_checkout(checkout: Path, artifact: TrialArtifact) -> None:
    """Require the resolver's checkout to remain the exact canonical P of a wheel.

    The wheel and detached sidecars establish their own mutual identity.  This
    second check binds the caller-supplied checkout used by the installed GUI
    gate to that same immutable public source before any fresh environment is
    created.  Git identity is checked both sides of the canonical scan so a
    mutable worktree never becomes evidence for P.
    """
    try:
        _verify_trial_source_git_identity(checkout, artifact.source_sha)
        policy, allowlist_bytes = _trial_source_policy(checkout)
        current = scan_public_checkout(checkout, policy)
        _verify_trial_source_allowlist(
            {entry.path: entry for entry in current.entries}, allowlist_bytes
        )
        _verify_trial_source_git_identity(checkout, artifact.source_sha)
    except (_TrialBuildError, ReleaseSourceError, OSError) as exc:
        raise ResolutionError("capture checkout identity is invalid") from exc
    if manifest_digest(current) != artifact.source_manifest_sha256:
        raise ResolutionError("capture checkout source manifest does not match M")


def pip_install_command(
    *,
    python: str,
    wheel: Path,
    report_path: Path,
    constraints_path: Path | None,
) -> tuple[str, ...]:
    """Return the strict command shared by both fresh-install phases."""
    command = [
        python,
        "-m",
        "pip",
        "install",
        "--isolated",
        "--no-input",
        "--only-binary=:all:",
        "--disable-pip-version-check",
        "--report",
        str(report_path),
    ]
    if constraints_path is not None:
        command.extend(("-c", str(constraints_path)))
    command.append(str(wheel))
    return tuple(command)


def pip_runtime_install_command(
    *,
    python: Path,
    package_names: Sequence[str],
    constraints: Path,
    report_path: Path,
) -> tuple[str, ...]:
    """Install only the phase-one runtime closure into the replay prefix."""
    names: list[str] = []
    for package_name in package_names:
        if not isinstance(package_name, str):
            raise ResolutionError("replay package name is invalid")
        normalized = _normalized_package_name(package_name)
        if normalized == _STUDIO_NAME:
            raise ResolutionError("replay prefix must not install Studio")
        names.append(normalized)
    if not names or len(names) != len(set(names)):
        raise ResolutionError("replay package set is invalid")
    return (
        str(python),
        "-m",
        "pip",
        "install",
        "--isolated",
        "--no-input",
        "--only-binary=:all:",
        "--disable-pip-version-check",
        "--report",
        str(report_path),
        "-c",
        str(constraints),
        *sorted(names),
    )


def build_resolution_projection(
    *,
    artifact: TrialArtifact,
    machine: str,
    glibc_version: str,
    os_runtime: Mapping[str, object],
    python_version: str,
    pip_version: str,
    phase_one_report: Mapping[str, object],
    phase_one_inspect: Mapping[str, object],
    phase_two_report: Mapping[str, object],
    phase_two_inspect: Mapping[str, object],
    phase_one_report_bytes: bytes | None = None,
    phase_one_inspect_bytes: bytes | None = None,
    phase_two_report_bytes: bytes | None = None,
    phase_two_inspect_bytes: bytes | None = None,
    technical_gate: Mapping[str, object] | None = None,
    os_id: str = _REQUIRED_OS_ID,
    os_version: str = _REQUIRED_OS_VERSION,
) -> dict[str, object]:
    """Make the public, path-free architecture evidence from ephemeral pip data."""
    if machine not in _MACHINES:
        raise ResolutionError("unsupported architecture")
    if _GLIBC_VERSION.fullmatch(glibc_version) is None:
        raise ResolutionError("glibc version is invalid")
    if _PYTHON_VERSION.fullmatch(python_version) is None:
        raise ResolutionError("Python version must be CPython 3.12 with a patch")
    if os_id != _REQUIRED_OS_ID or os_version != _REQUIRED_OS_VERSION:
        raise ResolutionError("resolution host must be Ubuntu 24.04")
    pip_version = _required_version(pip_version, "pip version")
    validated_os_runtime = validate_os_runtime(os_runtime, machine=machine)
    phase_one = _phase_projection(
        report=phase_one_report,
        inspect=phase_one_inspect,
        artifact=artifact,
        expected_pip_version=pip_version,
        report_bytes=phase_one_report_bytes,
        inspect_bytes=phase_one_inspect_bytes,
    )
    phase_two = _phase_projection(
        report=phase_two_report,
        inspect=phase_two_inspect,
        artifact=artifact,
        expected_pip_version=pip_version,
        report_bytes=phase_two_report_bytes,
        inspect_bytes=phase_two_inspect_bytes,
    )
    if phase_one["artifacts"] != phase_two["artifacts"]:
        raise ResolutionError("fresh re-install selected a different runtime closure")
    phase_one_inspect_projection = phase_one["inspect"]
    phase_two_inspect_projection = phase_two["inspect"]
    if not isinstance(phase_one_inspect_projection, Mapping) or not isinstance(
        phase_two_inspect_projection, Mapping
    ):
        raise ResolutionError("phase inspect projection is invalid")
    phase_one_installed = phase_one_inspect_projection.get("installed")
    phase_two_installed = phase_two_inspect_projection.get("installed")
    if not isinstance(phase_one_installed, list) or not isinstance(
        phase_two_installed, list
    ):
        raise ResolutionError("phase inspect installed projection is invalid")
    if phase_one_installed != phase_two_installed:
        raise ResolutionError("fresh re-install has a different installed environment")
    artifacts = phase_one["artifacts"]
    if not isinstance(artifacts, list):
        raise ResolutionError("phase one artifacts are invalid")
    runtime = [item for item in artifacts if item["name"] != _STUDIO_NAME]
    constraints = "".join(f"{item['name']}=={item['version']}\n" for item in runtime)
    projection: dict[str, object] = {
        "architecture": machine,
        "build_id": artifact.build_id,
        "constraints_sha256": _sha256(constraints.encode("utf-8")),
        "glibc_version": glibc_version,
        "os_id": os_id,
        "os_version": os_version,
        "os_runtime": validated_os_runtime,
        "phase_one": phase_one,
        "phase_two": phase_two,
        "pip_version": pip_version,
        "python_version": python_version,
        "runtime_artifacts": runtime,
        "schema": 2,
        "source_manifest_sha256": artifact.source_manifest_sha256,
        "source_sha": artifact.source_sha,
        "staging_manifest_sha256": artifact.staging_manifest_sha256,
        "version": artifact.version,
        "wheel": {
            "filename": artifact.wheel_filename,
            "sha256": artifact.wheel_sha256,
        },
    }
    if technical_gate is not None:
        try:
            validated_gate = _read_gate_result(_canonical_json(technical_gate))
        except _GateError as exc:
            raise ResolutionError("installed technical gate result is invalid") from exc
        if validated_gate["status"] != "passed":
            raise ResolutionError("installed technical gate failed")
        installed = validated_gate["installed"]
        if (
            validated_gate["architecture"] != machine
            or validated_gate["python_version"] != python_version
            or not isinstance(installed, Mapping)
            or installed.get("build_id") != artifact.build_id
            or installed.get("source_sha") != artifact.source_sha
            or installed.get("version") != artifact.version
        ):
            raise ResolutionError("installed technical gate identity disagrees")
        projection["technical_gate"] = validated_gate
    return projection


def build_conda_resolution_projection(
    *,
    artifact: TrialArtifact,
    target_id: str,
    machine: str,
    environment_manager: Mapping[str, object],
    python_version: str,
    pip_version: str,
    phase_one_report: Mapping[str, object],
    phase_one_inspect: Mapping[str, object],
    phase_two_report: Mapping[str, object],
    phase_two_inspect: Mapping[str, object],
    phase_one_conda_packages: Sequence[Mapping[str, object]],
    phase_two_conda_packages: Sequence[Mapping[str, object]],
    phase_replay_report: Mapping[str, object] | None = None,
    phase_replay_inspect: Mapping[str, object] | None = None,
    phase_replay_conda_packages: Sequence[Mapping[str, object]] | None = None,
    phase_one_import_isolation: Mapping[str, object] | None = None,
    phase_two_import_isolation: Mapping[str, object] | None = None,
    phase_replay_import_isolation: Mapping[str, object] | None = None,
    os_id: str | None = None,
    os_version: str | None = None,
    glibc_version: str | None = None,
    os_runtime: Mapping[str, object] | None = None,
    platform_record: Mapping[str, object] | None = None,
    phase_one_report_bytes: bytes | None = None,
    phase_one_inspect_bytes: bytes | None = None,
    phase_two_report_bytes: bytes | None = None,
    phase_two_inspect_bytes: bytes | None = None,
    phase_replay_report_bytes: bytes | None = None,
    phase_replay_inspect_bytes: bytes | None = None,
    technical_gate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Make schema-4 evidence from two Studio phases and one replay phase.

    The schema-2 and schema-3 builders above intentionally remain the
    historical read/write paths for old artifacts.  This builder is the only
    path that emits schema 4 and therefore requires an explicit closed target,
    architecture, and conda manager record.
    """
    try:
        target = _trial_target(target_id)
        target.require_architecture(machine)
        expected_subdir = _conda_subdir(target_id, machine)
    except (_TrialTargetError, _ToolchainError) as exc:
        raise ResolutionError("resolution target or architecture is invalid") from exc
    try:
        _require_exact_keys(
            environment_manager,
            _CONDA_MANAGER_FIELDS,
            "conda environment manager",
        )
    except ResolutionError:
        raise
    manager_version = _required_version(
        environment_manager.get("version"), "conda version"
    )
    try:
        expected_manager = _conda_environment_manager_record(
            target_id=target_id,
            architecture=machine,
            version=manager_version,
        )
    except _ToolchainError as exc:
        raise ResolutionError("conda environment manager is invalid") from exc
    if dict(environment_manager) != expected_manager:
        raise ResolutionError(
            f"conda environment manager does not match target {target_id!r}"
        )
    if environment_manager.get("subdir") != expected_subdir:
        raise ResolutionError("conda subdir does not match target")
    if _PYTHON_VERSION.fullmatch(python_version) is None:
        raise ResolutionError("Python version must be CPython 3.12 with a patch")
    pip_version = _required_version(pip_version, "pip version")

    phase_one_conda_packages = validate_conda_package_records(
        phase_one_conda_packages,
        python_version=python_version,
        pip_version=pip_version,
    )
    phase_two_conda_packages = validate_conda_package_records(
        phase_two_conda_packages,
        python_version=python_version,
        pip_version=pip_version,
    )
    if any(
        value is None
        for value in (
            phase_replay_report,
            phase_replay_inspect,
            phase_replay_conda_packages,
            phase_one_import_isolation,
            phase_two_import_isolation,
            phase_replay_import_isolation,
        )
    ):
        raise ResolutionError(
            "schema-4 resolution requires the complete replay phase evidence"
        )
    assert phase_replay_report is not None
    assert phase_replay_inspect is not None
    assert phase_replay_conda_packages is not None
    assert phase_one_import_isolation is not None
    assert phase_two_import_isolation is not None
    assert phase_replay_import_isolation is not None
    phase_one = {
        **_phase_projection(
            report=phase_one_report,
            inspect=phase_one_inspect,
            artifact=artifact,
            expected_pip_version=pip_version,
            report_bytes=phase_one_report_bytes,
            inspect_bytes=phase_one_inspect_bytes,
        ),
        "conda_packages": phase_one_conda_packages,
    }
    phase_two = {
        **_phase_projection(
            report=phase_two_report,
            inspect=phase_two_inspect,
            artifact=artifact,
            expected_pip_version=pip_version,
            report_bytes=phase_two_report_bytes,
            inspect_bytes=phase_two_inspect_bytes,
        ),
        "conda_packages": phase_two_conda_packages,
    }
    phase_replay_conda_packages = validate_conda_package_records(
        phase_replay_conda_packages,
        python_version=python_version,
        pip_version=pip_version,
    )
    phase_replay = {
        **_phase_projection(
            report=phase_replay_report,
            inspect=phase_replay_inspect,
            artifact=artifact,
            expected_pip_version=pip_version,
            report_bytes=phase_replay_report_bytes,
            inspect_bytes=phase_replay_inspect_bytes,
            require_studio=False,
        ),
        "conda_packages": phase_replay_conda_packages,
    }
    phase_import_isolation = {
        "phase_one": validate_import_isolation(
            phase_one_import_isolation, require_studio=True
        ),
        "phase_two": validate_import_isolation(
            phase_two_import_isolation, require_studio=True
        ),
        "phase_replay": validate_import_isolation(
            phase_replay_import_isolation, require_studio=False
        ),
    }
    phase_one["import_isolation"] = phase_import_isolation["phase_one"]
    phase_two["import_isolation"] = phase_import_isolation["phase_two"]
    phase_replay["import_isolation"] = phase_import_isolation["phase_replay"]
    if phase_one["artifacts"] != phase_two["artifacts"]:
        raise ResolutionError("fresh conda re-install selected a different pip closure")
    if phase_one["conda_packages"] != phase_two["conda_packages"]:
        raise ResolutionError(
            "fresh conda re-install selected a different conda package closure"
        )
    if phase_replay["artifacts"] != [
        item for item in phase_one["artifacts"] if item["name"] != _STUDIO_NAME
    ]:
        raise ResolutionError("replay phase selected a different pip runtime closure")
    first_replay_inspect = phase_replay["inspect"]
    first_runtime_inspect = [
        item
        for item in phase_one["inspect"]["installed"]
        if item["name"] != _STUDIO_NAME
    ]
    if first_replay_inspect["installed"] != first_runtime_inspect:
        raise ResolutionError("replay phase has a different pip environment")
    if phase_replay["conda_packages"] != phase_one["conda_packages"]:
        raise ResolutionError("replay phase selected a different conda closure")
    first_inspect = phase_one.get("inspect")
    second_inspect = phase_two.get("inspect")
    if (
        not isinstance(first_inspect, Mapping)
        or not isinstance(second_inspect, Mapping)
        or first_inspect.get("installed") != second_inspect.get("installed")
    ):
        raise ResolutionError("fresh conda re-install has a different pip environment")
    artifacts = phase_one["artifacts"]
    if not isinstance(artifacts, list):
        raise ResolutionError("phase one artifacts are invalid")
    runtime = [item for item in artifacts if item["name"] != _STUDIO_NAME]
    constraints = "".join(f"{item['name']}=={item['version']}\n" for item in runtime)
    projection: dict[str, object] = {
        "architecture": machine,
        "build_id": artifact.build_id,
        "constraints_sha256": _sha256(constraints.encode("utf-8")),
        "environment_manager": dict(environment_manager),
        "phase_one": phase_one,
        "phase_replay": phase_replay,
        "phase_two": phase_two,
        "pip_version": pip_version,
        "python_version": python_version,
        "runtime_artifacts": runtime,
        "schema": 4,
        "source_manifest_sha256": artifact.source_manifest_sha256,
        "source_sha": artifact.source_sha,
        "staging_manifest_sha256": artifact.staging_manifest_sha256,
        "target_id": target.id,
        "technical_gate": None,
        "version": artifact.version,
        "wheel": {
            "filename": artifact.wheel_filename,
            "sha256": artifact.wheel_sha256,
        },
    }
    if target.os_id == "macos":
        if (
            os_id is not None
            or os_version is not None
            or glibc_version is not None
            or os_runtime is not None
            or platform_record is None
        ):
            raise ResolutionError("macOS resolution requires native platform evidence")
        platform_value = _validate_schema4_macos_platform(platform_record, machine)
        projection["platform"] = platform_value
    else:
        if (
            os_id != target.os_id
            or os_version != target.os_version
            or glibc_version is None
            or os_runtime is None
            or platform_record is not None
        ):
            raise ResolutionError("resolution OS evidence does not match target")
        if _GLIBC_VERSION.fullmatch(glibc_version) is None:
            raise ResolutionError("glibc version is invalid")
        projection.update(
            {
                "glibc_version": glibc_version,
                "os_id": os_id,
                "os_runtime": validate_os_runtime(
                    os_runtime,
                    machine=machine,
                    package_names=_SCHEMA4_OS_RUNTIME_PACKAGES,
                ),
                "os_version": os_version,
            }
    )
    if technical_gate is None:
        raise ResolutionError(
            "schema-4 resolution requires the installed technical gate"
        )
    try:
        validated_gate = _read_gate_result(_canonical_json(technical_gate))
    except _GateError as exc:
        raise ResolutionError("installed technical gate result is invalid") from exc
    if (
        validated_gate["schema"] != 3
        or set(validated_gate["checks"]) != set(_CURRENT_GATE_CHECK_NAMES)
        or validated_gate["status"] != "passed"
    ):
        raise ResolutionError(
            "schema-4 resolution requires the current technical-gate checks"
        )
    installed = validated_gate["installed"]
    if (
        validated_gate["architecture"] != machine
        or validated_gate["python_version"] != python_version
        or not isinstance(installed, Mapping)
        or installed.get("build_id") != artifact.build_id
        or installed.get("source_sha") != artifact.source_sha
        or installed.get("version") != artifact.version
    ):
        raise ResolutionError("installed technical gate identity disagrees")
    projection["technical_gate"] = validated_gate
    return projection


def read_resolution_document(
    value: bytes | Path | Mapping[str, object],
) -> dict[str, object]:
    """Read schema 2/3 legacy evidence or validate a schema-4 document.

    Legacy documents are deliberately returned without normalization or
    rewriting.  They remain readable for historical bundle verification but
    cannot be emitted by :func:`build_conda_resolution_projection`.
    """
    if isinstance(value, bytes):
        document = _parse_json_bytes(value, "resolution")
    elif isinstance(value, Path):
        document = _read_json_object(value, "resolution")
    elif isinstance(value, Mapping):
        document = dict(value)
    else:
        raise ResolutionError("resolution must be JSON bytes, a path, or an object")
    schema = document.get("schema")
    if schema in {2, 3}:
        return document
    if schema != 4:
        raise ResolutionError("resolution schema is unsupported")
    target_id = document.get("target_id")
    if not isinstance(target_id, str):
        raise ResolutionError("schema-4 resolution target is invalid")
    try:
        target = _trial_target(target_id)
    except _TrialTargetError as exc:
        raise ResolutionError("schema-4 resolution target is invalid") from exc
    expected_fields = (
        _SCHEMA4_MACOS_FIELDS if target.os_id == "macos" else _SCHEMA4_LINUX_FIELDS
    )
    if set(document) != expected_fields:
        raise ResolutionError("schema-4 resolution fields are invalid")
    return document


def _conda_package_records(
    packages: Sequence[Mapping[str, object]], *, require_base: bool = False
) -> list[dict[str, str]]:
    """Validate and canonically sort the conda package closure records."""
    if isinstance(packages, (str, bytes)):
        raise ResolutionError("conda package closure is invalid")
    records: list[dict[str, str]] = []
    for package in packages:
        if not isinstance(package, Mapping) or set(package) != _CONDA_PACKAGE_FIELDS:
            raise ResolutionError("conda package record is invalid")
        name = _required_string(package.get("name"), "conda package name")
        if _CONDA_PACKAGE_NAME.fullmatch(name) is None:
            raise ResolutionError("conda package name is invalid")
        version = _required_version(package.get("version"), "conda package version")
        build = package.get("build")
        if not isinstance(build, str) or _CONDA_BUILD.fullmatch(build) is None:
            raise ResolutionError("conda package build is invalid")
        records.append({"build": build, "name": name, "version": version})
    records.sort(key=lambda item: item["name"].encode("utf-8"))
    if not records:
        raise ResolutionError("conda package closure is empty")
    if len({item["name"] for item in records}) != len(records):
        raise ResolutionError("conda package closure contains a package more than once")
    if require_base and not {"python", "pip"} <= {
        item["name"] for item in records
    }:
        raise ResolutionError("conda package closure must contain python and pip")
    return records


def validate_conda_package_records(
    value: object,
    *,
    python_version: str | None = None,
    pip_version: str | None = None,
) -> list[dict[str, str]]:
    """Validate persisted conda package identities without retaining extra fields."""
    if not isinstance(value, list):
        raise ResolutionError("conda package closure is invalid")
    if (python_version is None) != (pip_version is None):
        raise ResolutionError("conda package top-level versions are incomplete")
    records = _conda_package_records(value, require_base=True)
    if python_version is None or pip_version is None:
        return records
    by_name = {item["name"]: item["version"] for item in records}
    if by_name["python"] != python_version:
        raise ResolutionError("conda python package version disagrees with resolution")
    if by_name["pip"] != pip_version:
        raise ResolutionError("conda pip package version disagrees with resolution")
    return records


def _validate_schema4_macos_platform(
    value: Mapping[str, object], machine: str
) -> dict[str, object]:
    if machine != "arm64":
        raise ResolutionError("macOS resolution requires ARM64")
    _require_exact_keys(
        value,
        {
            "architecture",
            "id",
            "qt_opengl_context",
            "qt_platform",
            "qt_version",
            "version",
        },
        "macOS platform",
    )
    return macos_platform_record(
        machine=_required_string(value.get("architecture"), "macOS architecture"),
        os_version=_required_string(value.get("version"), "macOS version"),
        qt_platform=_required_string(value.get("qt_platform"), "Qt platform"),
        qt_version=_required_string(value.get("qt_version"), "Qt version"),
        opengl_context=_required_bool(
            value.get("qt_opengl_context"), "Qt OpenGL context"
        ),
    )


def macos_platform_record(
    *,
    machine: str,
    os_version: str,
    qt_platform: str,
    qt_version: str,
    opengl_context: bool,
) -> dict[str, object]:
    """Validate and return the path-free macOS 15 ARM64 runtime record."""
    version_pattern = r"[0-9]+(?:\.[0-9]+){1,3}"
    if (
        machine != "arm64"
        or re.fullmatch(version_pattern, os_version) is None
        or int(os_version.split(".", 1)[0]) < 15
        or qt_platform != "cocoa"
        or re.fullmatch(version_pattern, qt_version) is None
        or opengl_context is not True
    ):
        raise ResolutionError("macOS platform evidence is invalid")
    return {
        "architecture": "arm64",
        "id": "macos",
        "qt_opengl_context": True,
        "qt_platform": "cocoa",
        "qt_version": qt_version,
        "version": os_version,
    }


def build_macos_resolution_projection(
    *,
    artifact: TrialArtifact,
    platform_record: Mapping[str, object],
    python_version: str,
    pip_version: str,
    phase_one_report: Mapping[str, object],
    phase_one_inspect: Mapping[str, object],
    phase_two_report: Mapping[str, object],
    phase_two_inspect: Mapping[str, object],
    phase_one_report_bytes: bytes | None = None,
    phase_one_inspect_bytes: bytes | None = None,
    phase_two_report_bytes: bytes | None = None,
    phase_two_inspect_bytes: bytes | None = None,
    technical_gate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Make schema-3 macOS ARM64 dependency and technical-gate evidence."""
    _require_exact_keys(
        platform_record,
        {
            "architecture",
            "id",
            "qt_opengl_context",
            "qt_platform",
            "qt_version",
            "version",
        },
        "macOS platform",
    )
    validated_platform = macos_platform_record(
        machine=_required_string(
            platform_record.get("architecture"), "macOS architecture"
        ),
        os_version=_required_string(platform_record.get("version"), "macOS version"),
        qt_platform=_required_string(platform_record.get("qt_platform"), "Qt platform"),
        qt_version=_required_string(platform_record.get("qt_version"), "Qt version"),
        opengl_context=_required_bool(
            platform_record.get("qt_opengl_context"), "Qt OpenGL context"
        ),
    )
    if _PYTHON_VERSION.fullmatch(python_version) is None:
        raise ResolutionError("Python version must be CPython 3.12 with a patch")
    pip_version = _required_version(pip_version, "pip version")
    phase_one = _phase_projection(
        report=phase_one_report,
        inspect=phase_one_inspect,
        artifact=artifact,
        expected_pip_version=pip_version,
        report_bytes=phase_one_report_bytes,
        inspect_bytes=phase_one_inspect_bytes,
    )
    phase_two = _phase_projection(
        report=phase_two_report,
        inspect=phase_two_inspect,
        artifact=artifact,
        expected_pip_version=pip_version,
        report_bytes=phase_two_report_bytes,
        inspect_bytes=phase_two_inspect_bytes,
    )
    if phase_one["artifacts"] != phase_two["artifacts"]:
        raise ResolutionError("fresh re-install selected a different runtime closure")
    first_inspect = phase_one.get("inspect")
    second_inspect = phase_two.get("inspect")
    if (
        not isinstance(first_inspect, Mapping)
        or not isinstance(second_inspect, Mapping)
        or first_inspect.get("installed") != second_inspect.get("installed")
    ):
        raise ResolutionError("fresh re-install has a different installed environment")
    artifacts = phase_one["artifacts"]
    if not isinstance(artifacts, list):
        raise ResolutionError("phase one artifacts are invalid")
    runtime = [item for item in artifacts if item["name"] != _STUDIO_NAME]
    constraints = "".join(f"{item['name']}=={item['version']}\n" for item in runtime)
    if technical_gate is None:
        raise ResolutionError("macOS resolution requires the installed technical gate")
    try:
        validated_gate = _read_gate_result(_canonical_json(technical_gate))
    except _GateError as exc:
        raise ResolutionError("installed technical gate result is invalid") from exc
    installed = validated_gate["installed"]
    if (
        validated_gate["status"] != "passed"
        or validated_gate["architecture"] != "arm64"
        or validated_gate["python_version"] != python_version
        or not isinstance(installed, Mapping)
        or installed.get("build_id") != artifact.build_id
        or installed.get("source_sha") != artifact.source_sha
        or installed.get("version") != artifact.version
    ):
        raise ResolutionError("installed technical gate identity disagrees")
    return {
        "architecture": "arm64",
        "build_id": artifact.build_id,
        "constraints_sha256": _sha256(constraints.encode("utf-8")),
        "phase_one": phase_one,
        "phase_two": phase_two,
        "pip_version": pip_version,
        "platform": validated_platform,
        "python_version": python_version,
        "runtime_artifacts": runtime,
        "schema": 3,
        "source_manifest_sha256": artifact.source_manifest_sha256,
        "source_sha": artifact.source_sha,
        "staging_manifest_sha256": artifact.staging_manifest_sha256,
        "technical_gate": validated_gate,
        "version": artifact.version,
        "wheel": {
            "filename": artifact.wheel_filename,
            "sha256": artifact.wheel_sha256,
        },
    }


_MACOS_QT_PROBE = r"""
import json
import platform
import sys

from PySide6.QtCore import qVersion
from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext

app = QGuiApplication([])
surface = QOffscreenSurface()
surface.create()
context = QOpenGLContext()
created = context.create()
current = created and surface.isValid() and context.makeCurrent(surface)
record = {
    "machine": platform.machine().lower(),
    "opengl_context": bool(current),
    "os_version": platform.mac_ver()[0],
    "qt_platform": app.platformName(),
    "qt_version": qVersion(),
}
if current:
    context.doneCurrent()
print(json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
sys.exit(0 if app.primaryScreen() is not None else 1)
"""


def capture_macos_platform(python: Path, *, cwd: Path) -> dict[str, object]:
    """Probe native Cocoa, a real screen, and one usable Qt OpenGL context."""
    environment = _subprocess_environment()
    environment.pop("QT_QPA_PLATFORM", None)
    try:
        completed = subprocess.run(
            [str(python), "-I", "-c", _MACOS_QT_PROBE],
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolutionError("macOS Qt platform probe could not run") from exc
    if completed.returncode != 0:
        raise ResolutionError("macOS Qt platform probe failed")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ResolutionError("macOS Qt platform probe is invalid") from exc
    if not isinstance(document, Mapping) or set(document) != {
        "machine",
        "opengl_context",
        "os_version",
        "qt_platform",
        "qt_version",
    }:
        raise ResolutionError("macOS Qt platform probe is invalid")
    return macos_platform_record(
        machine=_required_string(document.get("machine"), "macOS architecture"),
        os_version=_required_string(document.get("os_version"), "macOS version"),
        qt_platform=_required_string(document.get("qt_platform"), "Qt platform"),
        qt_version=_required_string(document.get("qt_version"), "Qt version"),
        opengl_context=_required_bool(
            document.get("opengl_context"), "Qt OpenGL context"
        ),
    )


def capture_trial_resolution(
    *,
    wheel: Path,
    trial_manifest: Path,
    source_manifest: Path,
    staging_manifest: Path,
    checkout_root: Path,
    output_directory: Path,
    trial_target_id: str | None = None,
    environment_backend: str | None = None,
    conda_executable: Path | None = None,
) -> tuple[Path, Path]:
    """Resolve an exact legacy venv or explicitly selected conda runtime closure."""
    if environment_backend not in {None, "conda"}:
        raise ResolutionError("resolution environment backend must be conda")
    if (environment_backend is None) != (conda_executable is None):
        raise ResolutionError("conda backend requires an explicit conda executable")
    if trial_target_id is not None and environment_backend != "conda":
        raise ResolutionError("new target resolution requires conda backend")
    if trial_target_id is None and environment_backend == "conda":
        raise ResolutionError("conda backend requires an explicit trial target")
    target_name = trial_target_id or "wsl2-ubuntu24"
    try:
        target = _trial_target(target_name)
    except _TrialTargetError as exc:
        raise ResolutionError("unsupported trial target") from exc
    artifact = load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
        staging_manifest=staging_manifest,
    )
    machine = _machine()
    try:
        target.require_architecture(machine)
    except _TrialTargetError as exc:
        raise ResolutionError("resolution target architecture is unsupported") from exc
    expected_subdir: str | None = None
    if environment_backend == "conda":
        try:
            expected_subdir = _conda_subdir(target_name, machine)
        except _ToolchainError as exc:
            raise ResolutionError("resolution target conda subdir is invalid") from exc
    if target.os_id == "macos":
        if sys.platform != "darwin" or machine != "arm64":
            raise ResolutionError("macOS target requires a native Darwin ARM64 host")
        glibc_version = None
        os_release = None
    else:
        if sys.platform != "linux" or machine not in {"x86_64", "aarch64"}:
            raise ResolutionError("Linux target requires a native Linux host")
        glibc_version = _glibc_version()
        os_release = _target_os_release(target.os_id)
    try:
        checkout = _trial_source_root(checkout_root)
        verify_capture_checkout(checkout, artifact)
        output = _trial_output_directory(output_directory, checkout)
        temporary_parent = _trial_temporary_parent(checkout)
    except _TrialBuildError as exc:
        raise ResolutionError("resolution output location is unsafe") from exc
    if target.os_id == "macos":
        os_runtime = None
    elif environment_backend == "conda":
        os_runtime = capture_os_runtime(
            machine,
            cwd=checkout,
            package_names=_SCHEMA4_OS_RUNTIME_PACKAGES,
        )
    else:
        os_runtime = capture_os_runtime(machine, cwd=checkout)
    with tempfile.TemporaryDirectory(
        prefix=".gwexpy-studio-resolution-",
        dir=temporary_parent,
    ) as name:
        workspace = Path(name)
        sealed_wheel = _seal_wheel(workspace, artifact)
        conda_manager: dict[str, object] | None = None
        if environment_backend == "conda":
            assert conda_executable is not None
            conda_manager = _conda_environment_manager(
                conda_executable=conda_executable,
                target_id=target_name,
                architecture=machine,
                cwd=workspace,
            )
            phase_one = _fresh_conda_phase(
                workspace / "phase-one",
                conda_executable=conda_executable,
                target_id=target_name,
                architecture=machine,
            )
        else:
            phase_one = _fresh_phase(workspace / "phase-one")
        phase_one_report_path = workspace / "phase-one-report.json"
        _run(
            pip_install_command(
                python=str(phase_one.python),
                wheel=sealed_wheel,
                report_path=phase_one_report_path,
                constraints_path=None,
            ),
            cwd=workspace,
        )
        _run_pip_check(phase_one.python, workspace)
        phase_one_report_bytes = _read_regular_bytes(
            phase_one_report_path, "phase-one pip report"
        )
        phase_one_inspect_bytes = _pip_inspect(phase_one.python, workspace)
        phase_one_report = _parse_json_bytes(phase_one_report_bytes, "phase-one report")
        phase_one_inspect = _parse_json_bytes(
            phase_one_inspect_bytes, "phase-one inspect"
        )
        phase_one_import_isolation: dict[str, object] | None = None
        if environment_backend == "conda":
            phase_one_import_isolation = _verify_runtime_imports(
                phase_one.python,
                cwd=workspace,
                checkout_root=checkout,
                require_studio=True,
            )
        phase_one_conda_packages = (
            _conda_phase_packages(
                conda_executable=conda_executable,
                prefix=workspace / "phase-one",
                cwd=workspace,
                expected_subdir=expected_subdir,
            )
            if environment_backend == "conda"
            else None
        )
        constraints_bytes = runtime_constraints(
            phase_one_report, artifact=artifact
        ).encode("utf-8")
        constraints_path = workspace / _constraints_filename(
            machine, target_name
        )
        constraints_path.write_bytes(constraints_bytes)

        if environment_backend == "conda":
            assert conda_executable is not None
            assert phase_one_conda_packages is not None
            phase_two = _fresh_conda_phase(
                workspace / "phase-two",
                conda_executable=conda_executable,
                target_id=target_name,
                architecture=machine,
                package_records=phase_one_conda_packages,
            )
        else:
            phase_two = _fresh_phase(workspace / "phase-two")
        phase_two_report_path = workspace / "phase-two-report.json"
        _run(
            pip_install_command(
                python=str(phase_two.python),
                wheel=sealed_wheel,
                report_path=phase_two_report_path,
                constraints_path=constraints_path,
            ),
            cwd=workspace,
        )
        _run_pip_check(phase_two.python, workspace)
        phase_two_report_bytes = _read_regular_bytes(
            phase_two_report_path, "phase-two pip report"
        )
        phase_two_inspect_bytes = _pip_inspect(phase_two.python, workspace)
        phase_two_report = _parse_json_bytes(phase_two_report_bytes, "phase-two report")
        phase_two_inspect = _parse_json_bytes(
            phase_two_inspect_bytes, "phase-two inspect"
        )
        phase_two_import_isolation: dict[str, object] | None = None
        if environment_backend == "conda":
            phase_two_import_isolation = _verify_runtime_imports(
                phase_two.python,
                cwd=workspace,
                checkout_root=checkout,
                require_studio=True,
            )
        phase_two_conda_packages = (
            _conda_phase_packages(
                conda_executable=conda_executable,
                prefix=workspace / "phase-two",
                cwd=workspace,
                expected_subdir=expected_subdir,
            )
            if environment_backend == "conda"
            else None
        )
        replay_phase: _FreshPhase | None = None
        replay_report: Mapping[str, object] | None = None
        replay_inspect: Mapping[str, object] | None = None
        replay_report_bytes: bytes | None = None
        replay_inspect_bytes: bytes | None = None
        replay_conda_packages: list[dict[str, str]] | None = None
        replay_import_isolation: dict[str, object] | None = None
        if environment_backend == "conda":
            assert conda_executable is not None
            assert phase_one_conda_packages is not None
            replay_phase = _fresh_conda_phase(
                workspace / "phase-replay",
                conda_executable=conda_executable,
                target_id=target_name,
                architecture=machine,
                package_records=phase_one_conda_packages,
            )
            replay_report_path = workspace / "phase-replay-report.json"
            replay_names = tuple(
                item.name
                for item in _selected_artifacts(phase_one_report, artifact=artifact)
                if item.name != _STUDIO_NAME
            )
            _run(
                pip_runtime_install_command(
                    python=replay_phase.python,
                    package_names=replay_names,
                    constraints=constraints_path,
                    report_path=replay_report_path,
                ),
                cwd=workspace,
            )
            _run_pip_check(replay_phase.python, workspace)
            replay_report_bytes = _read_regular_bytes(
                replay_report_path, "phase-replay pip report"
            )
            replay_inspect_bytes = _pip_inspect(replay_phase.python, workspace)
            replay_report = _parse_json_bytes(
                replay_report_bytes, "phase-replay report"
            )
            replay_inspect = _parse_json_bytes(
                replay_inspect_bytes, "phase-replay inspect"
            )
            replay_installed = _inspect_packages(replay_inspect)
            phase_one_runtime = [
                item
                for item in _inspect_packages(phase_one_inspect)
                if item["name"] != _STUDIO_NAME
            ]
            if replay_installed != phase_one_runtime:
                raise ResolutionError(
                    "replay prefix has a different runtime pip environment"
                )
            replay_conda_packages = _conda_phase_packages(
                conda_executable=conda_executable,
                prefix=workspace / "phase-replay",
                cwd=workspace,
                expected_subdir=expected_subdir,
            )
            if replay_conda_packages != phase_one_conda_packages:
                raise ResolutionError(
                    "replay prefix has a different conda package closure"
                )
            replay_import_isolation = _verify_runtime_imports(
                replay_phase.python,
                cwd=workspace,
                checkout_root=checkout,
                require_studio=False,
            )
        with seal_technical_gate_script(
            checkout=checkout,
            source_sha=artifact.source_sha,
            workspace=workspace,
        ) as gate_fd:
            technical_gate = run_installed_technical_gate(
                python=phase_two.python,
                gate_fd=gate_fd,
                checkout_root=checkout,
                work_root=workspace,
                native_qt=target.os_id == "macos",
                replay_python=replay_phase.python if replay_phase is not None else None,
                legacy=environment_backend is None,
            )
        pip_version = _pip_version(phase_two.python, workspace)
        python_version = _python_version(phase_two.python, workspace)
        if environment_backend == "conda":
            assert conda_manager is not None
            assert phase_one_conda_packages is not None
            assert phase_two_conda_packages is not None
            assert phase_one_import_isolation is not None
            assert phase_two_import_isolation is not None
            assert replay_report is not None
            assert replay_inspect is not None
            assert replay_report_bytes is not None
            assert replay_inspect_bytes is not None
            assert replay_conda_packages is not None
            assert replay_import_isolation is not None
            if target.os_id == "macos":
                projection = build_conda_resolution_projection(
                    artifact=artifact,
                    target_id=target_name,
                    machine=machine,
                    environment_manager=conda_manager,
                    python_version=python_version,
                    pip_version=pip_version,
                    phase_one_report=phase_one_report,
                    phase_one_inspect=phase_one_inspect,
                    phase_two_report=phase_two_report,
                    phase_two_inspect=phase_two_inspect,
                    phase_one_conda_packages=phase_one_conda_packages,
                    phase_two_conda_packages=phase_two_conda_packages,
                    phase_replay_report=replay_report,
                    phase_replay_inspect=replay_inspect,
                    phase_replay_conda_packages=replay_conda_packages,
                    phase_one_import_isolation=phase_one_import_isolation,
                    phase_two_import_isolation=phase_two_import_isolation,
                    phase_replay_import_isolation=replay_import_isolation,
                    phase_one_report_bytes=phase_one_report_bytes,
                    phase_one_inspect_bytes=phase_one_inspect_bytes,
                    phase_two_report_bytes=phase_two_report_bytes,
                    phase_two_inspect_bytes=phase_two_inspect_bytes,
                    phase_replay_report_bytes=replay_report_bytes,
                    phase_replay_inspect_bytes=replay_inspect_bytes,
                    technical_gate=technical_gate,
                    platform_record=capture_macos_platform(
                        phase_two.python, cwd=workspace
                    ),
                )
            else:
                assert glibc_version is not None
                assert os_runtime is not None
                assert os_release is not None
                projection = build_conda_resolution_projection(
                    artifact=artifact,
                    target_id=target_name,
                    machine=machine,
                    environment_manager=conda_manager,
                    python_version=python_version,
                    pip_version=pip_version,
                    phase_one_report=phase_one_report,
                    phase_one_inspect=phase_one_inspect,
                    phase_two_report=phase_two_report,
                    phase_two_inspect=phase_two_inspect,
                    phase_one_conda_packages=phase_one_conda_packages,
                    phase_two_conda_packages=phase_two_conda_packages,
                    phase_replay_report=replay_report,
                    phase_replay_inspect=replay_inspect,
                    phase_replay_conda_packages=replay_conda_packages,
                    phase_one_import_isolation=phase_one_import_isolation,
                    phase_two_import_isolation=phase_two_import_isolation,
                    phase_replay_import_isolation=replay_import_isolation,
                    os_id=os_release[0],
                    os_version=os_release[1],
                    glibc_version=glibc_version,
                    os_runtime=os_runtime,
                    phase_one_report_bytes=phase_one_report_bytes,
                    phase_one_inspect_bytes=phase_one_inspect_bytes,
                    phase_two_report_bytes=phase_two_report_bytes,
                    phase_two_inspect_bytes=phase_two_inspect_bytes,
                    phase_replay_report_bytes=replay_report_bytes,
                    phase_replay_inspect_bytes=replay_inspect_bytes,
                    technical_gate=technical_gate,
                )
        elif target_name == "macos15-arm64":
            projection = build_macos_resolution_projection(
                artifact=artifact,
                python_version=python_version,
                pip_version=pip_version,
                phase_one_report=phase_one_report,
                phase_one_inspect=phase_one_inspect,
                phase_two_report=phase_two_report,
                phase_two_inspect=phase_two_inspect,
                phase_one_report_bytes=phase_one_report_bytes,
                phase_one_inspect_bytes=phase_one_inspect_bytes,
                phase_two_report_bytes=phase_two_report_bytes,
                phase_two_inspect_bytes=phase_two_inspect_bytes,
                technical_gate=technical_gate,
                platform_record=capture_macos_platform(phase_two.python, cwd=workspace),
            )
        else:
            assert glibc_version is not None
            assert os_runtime is not None
            assert os_release is not None
            projection = build_resolution_projection(
                artifact=artifact,
                machine=machine,
                python_version=python_version,
                pip_version=pip_version,
                glibc_version=glibc_version,
                os_runtime=os_runtime,
                os_id=os_release[0],
                os_version=os_release[1],
                phase_one_report=phase_one_report,
                phase_one_inspect=phase_one_inspect,
                phase_two_report=phase_two_report,
                phase_two_inspect=phase_two_inspect,
                phase_one_report_bytes=phase_one_report_bytes,
                phase_one_inspect_bytes=phase_one_inspect_bytes,
                phase_two_report_bytes=phase_two_report_bytes,
                phase_two_inspect_bytes=phase_two_inspect_bytes,
                technical_gate=technical_gate,
            )
        if projection["constraints_sha256"] != _sha256(constraints_bytes):
            raise ResolutionError("generated constraints digest does not match closure")
        constraints_name = _constraints_filename(machine, target_name)
        resolution_name = _resolution_filename(machine, target_name)
        _publish_evidence(
            {
                constraints_name: constraints_bytes,
                resolution_name: _canonical_json(projection),
            },
            output,
            checkout,
        )
    return output / constraints_name, output / resolution_name


@dataclass(frozen=True, slots=True)
class _FreshPhase:
    python: Path


@contextmanager
def seal_technical_gate_script(
    *, checkout: Path, source_sha: str, workspace: Path
) -> Iterator[int]:
    """Yield a read-only, unlinked descriptor containing exact P gate bytes.

    The subprocess must not read either the mutable checkout or a re-openable
    temporary path after P verification.  ``git show <P>:path`` supplies the
    immutable object; it is verified, unlinked, reopened read-only through its
    retained descriptor, and inherited explicitly by each child process.
    """
    source_sha = _required_source_sha(source_sha)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ResolutionError("technical-gate workspace is unsafe")
    try:
        content = _trial_git_bytes(
            checkout, "show", f"{source_sha}:{_GATE_SCRIPT_GIT_PATH}"
        )
    except _TrialBuildError as exc:
        raise ResolutionError("technical-gate script is unavailable from P") from exc
    if not content:
        raise ResolutionError("technical-gate script is empty")
    destination = workspace / _SEALED_GATE_SCRIPT_NAME
    descriptor: int | None = None
    readonly_descriptor: int | None = None
    try:
        descriptor = os.open(
            destination,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as exc:
        raise ResolutionError("technical-gate script could not be sealed") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ResolutionError("technical-gate script seal is not regular")
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise ResolutionError("technical-gate script could not be sealed")
            remaining = remaining[written:]
        os.fsync(descriptor)
        sealed = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        verified = bytearray()
        while chunk := os.read(descriptor, 65536):
            verified.extend(chunk)
        if (
            not stat.S_ISREG(sealed.st_mode)
            or sealed.st_size != len(content)
            or _sha256(content) != _sha256(bytes(verified))
        ):
            raise ResolutionError("technical-gate script seal does not match P")
        named = os.stat(destination, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (
            sealed.st_dev,
            sealed.st_ino,
        ):
            raise ResolutionError("technical-gate script seal name changed")
        os.unlink(destination)
        descriptor_path = (
            f"/proc/self/fd/{descriptor}"
            if Path("/proc/self/fd").is_dir()
            else f"/dev/fd/{descriptor}"
        )
        readonly_descriptor = os.open(
            descriptor_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        )
        readonly = os.fstat(readonly_descriptor)
        if not stat.S_ISREG(readonly.st_mode) or (readonly.st_dev, readonly.st_ino) != (
            sealed.st_dev,
            sealed.st_ino,
        ):
            raise ResolutionError("technical-gate sealed descriptor changed")
        os.lseek(readonly_descriptor, 0, os.SEEK_SET)
        readonly_bytes = bytearray()
        while chunk := os.read(readonly_descriptor, 65536):
            readonly_bytes.extend(chunk)
        if _sha256(content) != _sha256(bytes(readonly_bytes)):
            raise ResolutionError("technical-gate sealed descriptor does not match P")
        os.lseek(readonly_descriptor, 0, os.SEEK_SET)
        os.close(descriptor)
        descriptor = None
        yield readonly_descriptor
    except OSError as exc:
        raise ResolutionError("technical-gate script could not be sealed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if readonly_descriptor is not None:
            os.close(readonly_descriptor)


def _seal_wheel(workspace: Path, artifact: TrialArtifact) -> Path:
    """Seal the already-verified wheel bytes before any pip process can read it."""
    destination = workspace / artifact.wheel_filename
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError as exc:
        raise ResolutionError("verified wheel could not be sealed") from exc
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ResolutionError("verified wheel seal is not a regular file")
        remaining = memoryview(artifact.wheel_bytes)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise ResolutionError("verified wheel could not be sealed")
            remaining = remaining[written:]
        os.fsync(descriptor)
        sealed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(sealed.st_mode)
            or sealed.st_size != len(artifact.wheel_bytes)
            or _sha256(artifact.wheel_bytes) != artifact.wheel_sha256
        ):
            raise ResolutionError("verified wheel seal does not match trial identity")
    except OSError as exc:
        raise ResolutionError("verified wheel could not be sealed") from exc
    finally:
        os.close(descriptor)
    return destination


def _fresh_phase(directory: Path) -> _FreshPhase:
    if directory.exists() or directory.is_symlink():
        raise ResolutionError("fresh environment directory already exists")
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(directory)
    except Exception as exc:
        raise ResolutionError("fresh virtual environment creation failed") from exc
    python = directory / "bin" / "python"
    if not python.is_file():
        raise ResolutionError("fresh virtual environment has no Python executable")
    return _FreshPhase(python=python)


def _fresh_conda_phase(
    directory: Path,
    *,
    conda_executable: Path,
    target_id: str,
    architecture: str,
    package_records: Sequence[Mapping[str, object]] | None = None,
) -> _FreshPhase:
    """Create one fresh target-bound conda prefix without a venv fallback."""
    if directory.exists() or directory.is_symlink():
        raise ResolutionError("fresh conda environment directory already exists")
    try:
        command = _fresh_conda_create_command(
            conda_executable=conda_executable,
            target_id=target_id,
            architecture=architecture,
            prefix=directory,
            package_records=package_records,
        )
        _run(command, cwd=directory.parent)
    except (_ToolchainError, ResolutionError) as exc:
        raise ResolutionError("fresh conda environment creation failed") from exc
    python = directory / "bin" / "python"
    if not python.is_file():
        raise ResolutionError("fresh conda environment has no Python executable")
    try:
        resolved_python = python.resolve(strict=True)
        resolved_prefix = directory.resolve(strict=True)
        resolved_python.relative_to(resolved_prefix)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResolutionError(
            "fresh conda Python executable must resolve inside prefix"
        ) from exc
    try:
        python_status = resolved_python.stat()
    except OSError as exc:
        raise ResolutionError(
            "fresh conda Python executable cannot be inspected"
        ) from exc
    if not stat.S_ISREG(python_status.st_mode) or not os.access(
        resolved_python, os.X_OK
    ):
        raise ResolutionError("fresh conda environment has no executable Python")
    _verify_python_isolation(python, cwd=directory.parent)
    return _FreshPhase(python=python)


def _conda_environment_manager(
    *, conda_executable: Path, target_id: str, architecture: str, cwd: Path
) -> dict[str, object]:
    """Read the fixed conda executable's version and target platform."""
    try:
        completed = subprocess.run(
            (str(conda_executable), "info", "--json"),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=False,
            timeout=120,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolutionError("conda info query failed") from exc
    if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
        raise ResolutionError("conda info query failed")
    info = _parse_json_bytes(completed.stdout, "conda info")
    try:
        return _conda_manager_from_info(
            target_id=target_id,
            architecture=architecture,
            info=info,
        )
    except _ToolchainError as exc:
        raise ResolutionError("conda info does not match the target") from exc


def _conda_phase_packages(
    *, conda_executable: Path, prefix: Path, cwd: Path, expected_subdir: str
) -> list[dict[str, str]]:
    """Capture only the path-free conda package identity for one prefix."""
    if not isinstance(expected_subdir, str) or not expected_subdir:
        raise ResolutionError("expected conda subdir is invalid")
    try:
        completed = subprocess.run(
            (
                str(conda_executable),
                "list",
                "--json",
                "--no-pip",
                "--prefix",
                str(prefix),
            ),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=False,
            timeout=120,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolutionError("conda package list query failed") from exc
    if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
        raise ResolutionError("conda package list query failed")
    value = _parse_json_value(completed.stdout, "conda package list")
    if not isinstance(value, list):
        raise ResolutionError("conda package list is not an array")
    projected: list[dict[str, object]] = []
    for package in value:
        if not isinstance(package, Mapping):
            raise ResolutionError("conda package list record is invalid")
        channel = package.get("channel")
        platform_name = package.get("platform")
        build_string = package.get("build_string")
        if not all(
            isinstance(marker, str)
            for marker in (channel, platform_name, build_string)
        ):
            raise ResolutionError("conda package list record markers are invalid")
        name = package.get("name")
        version = package.get("version")
        if (
            not isinstance(name, str)
            or _CONDA_PACKAGE_NAME.fullmatch(name) is None
            or not isinstance(version, str)
            or _VERSION.fullmatch(version) is None
        ):
            raise ResolutionError("conda package list record identity is invalid")
        if (channel, platform_name, build_string) == _CONDA_PYPI_MARKERS:
            continue
        if build_string == "pypi_0":
            raise ResolutionError("conda package list record markers are inconsistent")
        if channel != _CONDA_CHANNEL:
            raise ResolutionError("conda package list record channel is invalid")
        if platform_name not in {expected_subdir, "noarch"}:
            raise ResolutionError("conda package list record platform is invalid")
        projected.append(
            {
                "build": build_string,
                "name": name,
                "version": version,
            }
        )
    return _conda_package_records(projected, require_base=True)


def _phase_projection(
    *,
    report: Mapping[str, object],
    inspect: Mapping[str, object],
    artifact: TrialArtifact,
    expected_pip_version: str,
    report_bytes: bytes | None,
    inspect_bytes: bytes | None,
    require_studio: bool = True,
) -> dict[str, object]:
    if report_bytes is not None and _parse_json_bytes(
        report_bytes, "pip report bytes"
    ) != dict(report):
        raise ResolutionError("pip report bytes disagree with parsed evidence")
    if inspect_bytes is not None and _parse_json_bytes(
        inspect_bytes, "pip inspect bytes"
    ) != dict(inspect):
        raise ResolutionError("pip inspect bytes disagree with parsed evidence")
    report_pip_version = _required_version(
        _object_value(report, "pip_version", "pip report"), "pip report version"
    )
    inspect_pip_version = _required_version(
        _object_value(inspect, "pip_version", "pip inspect"), "pip inspect version"
    )
    if (
        report_pip_version != expected_pip_version
        or inspect_pip_version != expected_pip_version
    ):
        raise ResolutionError(
            "pip evidence version disagrees with the phase interpreter"
        )
    artifacts = _selected_artifacts(
        report, artifact=artifact, require_studio=require_studio
    )
    installed = _inspect_packages(inspect)
    expected_packages = {(item.name, item.version) for item in artifacts}
    installed_packages = {(item["name"], item["version"]) for item in installed}
    if not expected_packages.issubset(installed_packages):
        raise ResolutionError("pip inspect is missing a resolved runtime package")
    projected_artifacts = [item.as_dict() for item in artifacts]
    return {
        "artifacts": projected_artifacts,
        "inspect": {"installed": installed},
        "pip_inspect_sha256": _sha256(_canonical_json({"installed": installed})),
        "pip_report_sha256": _sha256(
            _canonical_json({"artifacts": projected_artifacts})
        ),
        "report": {"artifacts": projected_artifacts},
    }


def _selected_artifacts(
    report: Mapping[str, object],
    *,
    artifact: TrialArtifact,
    require_studio: bool = True,
) -> tuple[RuntimeArtifact, ...]:
    _validate_pip_schema(report, "pip report")
    install = _object_value(report, "install", "pip report")
    if not isinstance(install, list) or not install:
        raise ResolutionError("pip report has no selected install artifacts")
    selected: list[RuntimeArtifact] = []
    for item in install:
        if not isinstance(item, Mapping):
            raise ResolutionError("pip report install entry is invalid")
        metadata = _mapping_value(item, "metadata", "pip report install entry")
        name = _normalized_package_name(
            _required_string(
                _object_value(metadata, "name", "metadata"), "package name"
            )
        )
        version = _required_version(
            _object_value(metadata, "version", "metadata"), "package version"
        )
        is_direct = _required_bool(
            _object_value(item, "is_direct", "pip report install entry"),
            "pip report artifact direct flag",
        )
        is_yanked = _required_bool(
            _object_value(item, "is_yanked", "pip report install entry"),
            "pip report artifact yanked flag",
        )
        if is_yanked:
            raise ResolutionError("pip report selected a yanked runtime artifact")
        if name == _STUDIO_NAME:
            if not is_direct:
                raise ResolutionError("trial Studio wheel must be a direct artifact")
        elif is_direct:
            raise ResolutionError("runtime dependency must not be a direct artifact")
        download = _mapping_value(item, "download_info", "pip report install entry")
        url = _required_string(
            _object_value(download, "url", "download information"), "artifact URL"
        )
        filename = _filename_from_url(url)
        archive_info = _mapping_value(download, "archive_info", "download information")
        sha256 = _archive_sha256(archive_info)
        selected.append(
            RuntimeArtifact(
                name=name,
                version=version,
                filename=filename,
                sha256=sha256,
            )
        )
    selected.sort(key=lambda item: item.name.encode("utf-8"))
    names = [item.name for item in selected]
    if len(names) != len(set(names)):
        raise ResolutionError("pip report selected a package more than once")
    studio = next((item for item in selected if item.name == _STUDIO_NAME), None)
    if require_studio:
        if studio is None:
            raise ResolutionError("pip report omitted the trial Studio wheel")
        if (
            studio.filename != artifact.wheel_filename
            or studio.sha256 != artifact.wheel_sha256
            or studio.version != artifact.version
        ):
            raise ResolutionError(
                "pip report Studio wheel does not match trial identity"
            )
    elif studio is not None:
        raise ResolutionError(
            "replay pip report must not contain the trial Studio wheel"
        )
    if not all(item.filename.endswith(".whl") for item in selected):
        raise ResolutionError("pip report selected a non-wheel artifact")
    return tuple(selected)


def _inspect_packages(inspect: Mapping[str, object]) -> list[dict[str, str]]:
    _validate_pip_schema(inspect, "pip inspect")
    installed = _object_value(inspect, "installed", "pip inspect")
    if not isinstance(installed, list):
        raise ResolutionError("pip inspect installed list is invalid")
    result: list[dict[str, str]] = []
    for item in installed:
        if not isinstance(item, Mapping):
            raise ResolutionError("pip inspect installed entry is invalid")
        metadata = _mapping_value(item, "metadata", "pip inspect installed entry")
        name = _normalized_package_name(
            _required_string(
                _object_value(metadata, "name", "metadata"), "package name"
            )
        )
        version = _required_version(
            _object_value(metadata, "version", "metadata"), "package version"
        )
        result.append({"name": name, "version": version})
    result.sort(key=lambda item: item["name"].encode("utf-8"))
    if len({item["name"] for item in result}) != len(result):
        raise ResolutionError("pip inspect selected a package more than once")
    return result


def _verify_wheel_stamp(
    wheel_bytes: bytes,
    *,
    build_id: str,
    source_manifest_sha256: str,
    source_sha: str,
    version: str,
) -> None:
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(wheel_bytes)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ResolutionError("trial wheel has duplicate archive members")
            stamp = _parse_json_bytes(
                archive.read("gwexpy_studio/assets/trial-build.json"),
                "wheel trial-build stamp",
            )
            _require_exact_keys(
                stamp,
                {
                    "build_id",
                    "schema",
                    "source_manifest_sha256",
                    "source_sha",
                    "version",
                },
                "wheel trial-build stamp",
            )
            if stamp["schema"] != 1:
                raise ResolutionError("wheel trial-build stamp schema is unsupported")
            expected = {
                "build_id": build_id,
                "source_manifest_sha256": source_manifest_sha256,
                "source_sha": source_sha,
                "version": version,
            }
            if any(stamp[key] != value for key, value in expected.items()):
                raise ResolutionError("wheel trial-build stamp disagrees with manifest")
            metadata_name = _one_name(names, ".dist-info/METADATA")
            wheel_name = _one_name(names, ".dist-info/WHEEL")
            if wheel_name.rsplit("/", 1)[0] != metadata_name.rsplit("/", 1)[0]:
                raise ResolutionError("wheel metadata has inconsistent dist-info paths")
            fields = _metadata_fields(
                archive.read(metadata_name),
                critical_fields=frozenset({"Name", "Version"}),
            )
            wheel_fields = _metadata_fields(
                archive.read(wheel_name),
                critical_fields=frozenset({"Root-Is-Purelib", "Tag"}),
            )
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ResolutionError("trial wheel cannot be inspected") from exc
    if fields.get("Name") != _STUDIO_NAME or fields.get("Version") != version:
        raise ResolutionError("wheel metadata disagrees with trial manifest")
    if wheel_fields.get("Root-Is-Purelib") != "true":
        raise ResolutionError("trial wheel is not purelib")
    if wheel_fields.get("Tag") != "py3-none-any":
        raise ResolutionError("trial wheel tag is not py3-none-any")


def _verify_generated_wheel_members(
    wheel_bytes: bytes, value: object
) -> dict[str, bytes]:
    """Bind every Task2-approved staging delta member to the final wheel bytes."""
    if not isinstance(value, list) or len(value) != len(_GENERATED_WHEEL_MEMBERS):
        raise ResolutionError("trial generated file list is invalid")
    expected_paths = tuple(path for path, _member in _GENERATED_WHEEL_MEMBERS)
    actual: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
            raise ResolutionError("trial generated file entry is invalid")
        path = item["path"]
        sha256 = item["sha256"]
        if not isinstance(path, str):
            raise ResolutionError("trial generated file path is invalid")
        actual.append((path, _required_sha256(sha256, "trial generated file")))
    if tuple(path for path, _sha in actual) != expected_paths:
        raise ResolutionError("trial generated files do not match the approved delta")
    generated_files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(wheel_bytes)) as archive:
            for (source_path, member), (_path, expected_sha256) in zip(
                _GENERATED_WHEEL_MEMBERS, actual, strict=True
            ):
                content = archive.read(member)
                if _sha256(content) != expected_sha256:
                    raise ResolutionError(
                        "trial generated wheel member does not match its manifest"
                    )
                generated_files[source_path] = content
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ResolutionError("trial generated wheel member is unavailable") from exc
    return generated_files


def _verify_generated_file_semantics(
    generated_files: Mapping[str, bytes], version: str
) -> None:
    """Check that the approved delta carries the same identity as TRIAL."""
    version_bytes = generated_files["src/gwexpy_studio/_version.py"]
    assignments = tuple(
        re.finditer(rb'^__version__ = "([^"\r\n]+)"$', version_bytes, re.MULTILINE)
    )
    if len(assignments) != 1 or assignments[0].group(1) != version.encode("ascii"):
        raise ResolutionError("generated version file disagrees with trial identity")
    policy = _parse_json_bytes(
        generated_files["src/gwexpy_studio/assets/io-capabilities.json"],
        "generated I/O capability policy",
    )
    if policy != _APPROVED_TRIAL_CAPABILITY_POLICY:
        raise ResolutionError("generated I/O capability policy is not approved")


def _one_name(names: Sequence[str], suffix: str) -> str:
    matches = tuple(name for name in names if name.endswith(suffix))
    if len(matches) != 1:
        raise ResolutionError(f"trial wheel has ambiguous {suffix} metadata")
    return matches[0]


def _metadata_fields(
    content: bytes, *, critical_fields: frozenset[str]
) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in content.decode("utf-8").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in critical_fields and key in fields:
            raise ResolutionError("wheel metadata has duplicate identity field")
        fields.setdefault(key, value.strip())
    return fields


def _filename_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    filename = Path(urllib.parse.unquote(parsed.path)).name
    if _WHEEL_FILENAME.fullmatch(filename) is None:
        raise ResolutionError("pip report artifact filename is not a safe wheel name")
    return filename


def _archive_sha256(archive_info: Mapping[str, object]) -> str:
    hashes = archive_info.get("hashes")
    if not isinstance(hashes, Mapping):
        raise ResolutionError("pip report artifact has no SHA-256 hash")
    sha256 = _required_sha256(hashes.get("sha256"), "pip report artifact")
    legacy = archive_info.get("hash")
    if legacy is None:
        return sha256
    if not isinstance(legacy, str) or not legacy.startswith("sha256="):
        raise ResolutionError("pip report artifact legacy hash is invalid")
    legacy_sha256 = _required_sha256(
        legacy.removeprefix("sha256="), "pip report artifact legacy"
    )
    if legacy_sha256 != sha256:
        raise ResolutionError("pip report artifact hashes disagree")
    return sha256


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    return _parse_json_bytes(_read_regular_bytes(path, label), label)


def _parse_json_bytes(content: bytes, label: str) -> dict[str, object]:
    value = _parse_json_value(content, label)
    if not isinstance(value, dict):
        raise ResolutionError(f"{label} must be a JSON object")
    return value


def _parse_json_value(content: bytes, label: str) -> object:
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ResolutionError(f"{label} is not valid JSON") from exc
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON field")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value!r}")


def _read_regular_bytes(path: Path, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise ResolutionError(f"{label} must be a regular file")
        return path.read_bytes()
    except OSError as exc:
        raise ResolutionError(f"{label} cannot be read") from exc


def _validate_trial_identity(build_id: str, source_sha: str, version: str) -> None:
    build_match = _BUILD_ID.fullmatch(build_id)
    if build_match is None:
        raise ResolutionError("trial build ID is invalid")
    version_match = _TRIAL_VERSION.fullmatch(version)
    if version_match is None:
        raise ResolutionError("trial version is invalid")
    if build_match["sha"] != source_sha[:7] or version_match["sha"] != source_sha[:7]:
        raise ResolutionError("trial identity does not bind the source SHA")
    if build_match.groupdict() != version_match.groupdict():
        raise ResolutionError("trial build ID and version disagree")
    try:
        datetime.strptime(build_match["date"], "%Y%m%d")
    except ValueError as exc:
        raise ResolutionError("trial build ID date is invalid") from exc


def _require_exact_keys(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise ResolutionError(f"{label} fields are invalid")


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None:
        raise ResolutionError(f"{label} SHA-256 is invalid")
    return value


def _required_source_sha(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_SHA.fullmatch(value) is None:
        raise ResolutionError("trial source SHA is invalid")
    return value


def _required_wheel_filename(value: object) -> str:
    if not isinstance(value, str) or _WHEEL_FILENAME.fullmatch(value) is None:
        raise ResolutionError("trial wheel filename is invalid")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ResolutionError(f"{label} is invalid")
    return value


def _required_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ResolutionError(f"{label} is invalid")
    return value


def _required_version(value: object, label: str) -> str:
    version = _required_string(value, label)
    if _VERSION.fullmatch(version) is None:
        raise ResolutionError(f"{label} is unsafe")
    return version


def _validate_pip_schema(value: Mapping[str, object], label: str) -> None:
    if _object_value(value, "version", label) != "1":
        raise ResolutionError(f"{label} schema is unsupported")


def _normalized_package_name(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value).lower()
    if _PACKAGE_NAME.fullmatch(normalized) is None:
        raise ResolutionError("package name is invalid")
    return normalized


def _object_value(value: Mapping[str, object], key: str, label: str) -> object:
    try:
        return value[key]
    except KeyError as exc:
        raise ResolutionError(f"{label} is missing {key}") from exc


def _mapping_value(
    value: Mapping[str, object], key: str, label: str
) -> Mapping[str, object]:
    selected = _object_value(value, key, label)
    if not isinstance(selected, Mapping):
        raise ResolutionError(f"{label} {key} is invalid")
    return selected


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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


def capture_os_runtime(
    machine: str,
    *,
    cwd: Path,
    package_names: Sequence[str] = _OS_RUNTIME_PACKAGES,
) -> dict[str, object]:
    """Capture the direct OS packages and SONAMEs required by Qt's GL runtime."""
    expected_architecture = _dpkg_architecture(machine)
    package_names = tuple(package_names)
    if not package_names or len(package_names) != len(set(package_names)):
        raise ResolutionError("Qt GL runtime package list is invalid")
    queried_names = tuple(f"{name}:{expected_architecture}" for name in package_names)
    try:
        completed = subprocess.run(
            (
                "dpkg-query",
                "--show",
                f"--showformat={_DPKG_QUERY_FORMAT}",
                *queried_names,
            ),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolutionError("Qt GL runtime package query failed") from exc
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        raise ResolutionError("Qt GL runtime package query failed")

    packages: list[dict[str, str]] = []
    lines = completed.stdout.splitlines()
    records: dict[str, tuple[str, str, str]] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 4:
            raise ResolutionError("Qt GL runtime package query is malformed")
        status, name, version, architecture = fields
        if name not in package_names or architecture != expected_architecture:
            raise ResolutionError("Qt GL runtime package identity is invalid")
        if name in records:
            raise ResolutionError("Qt GL runtime package identity is invalid")
        if status != "ii ":
            raise ResolutionError("Qt GL runtime package is not installed")
        records[name] = (status, version, architecture)

    if set(records) != set(package_names):
        raise ResolutionError("Qt GL runtime package query is incomplete")
    for expected_name in package_names:
        _status, version, architecture = records[expected_name]
        packages.append(
            {
                "architecture": architecture,
                "name": expected_name,
                "status": _OS_RUNTIME_STATUS,
                "version": _required_debian_version(
                    version, "Qt GL runtime package version"
                ),
            }
        )

    runtime = validate_os_runtime(
        {
            "manager": _OS_RUNTIME_MANAGER,
            "packages": packages,
            "required_libraries": list(_REQUIRED_QT_GL_LIBRARIES),
        },
        machine=machine,
        package_names=package_names,
    )
    for library in _REQUIRED_QT_GL_LIBRARIES:
        try:
            ctypes.CDLL(library)
        except OSError as exc:
            raise ResolutionError("Qt GL runtime library cannot load") from exc
    return runtime


def validate_os_runtime(
    value: object,
    *,
    machine: str,
    package_names: Sequence[str] = _OS_RUNTIME_PACKAGES,
) -> dict[str, object]:
    """Validate the path-free direct Qt GL runtime evidence for one architecture."""
    if not isinstance(value, Mapping):
        raise ResolutionError("Qt GL runtime evidence is invalid")
    _require_exact_keys(
        value,
        {"manager", "packages", "required_libraries"},
        "Qt GL runtime evidence",
    )
    if value.get("manager") != _OS_RUNTIME_MANAGER:
        raise ResolutionError("Qt GL runtime package manager is invalid")
    package_names = tuple(package_names)
    if not package_names or len(package_names) != len(set(package_names)):
        raise ResolutionError("Qt GL runtime package list is invalid")
    expected_architecture = _dpkg_architecture(machine)
    packages_value = value.get("packages")
    if not isinstance(packages_value, list) or len(packages_value) != len(
        package_names
    ):
        raise ResolutionError("Qt GL runtime packages are invalid")
    packages: list[dict[str, str]] = []
    for expected_name, package_value in zip(
        package_names, packages_value, strict=True
    ):
        if not isinstance(package_value, Mapping):
            raise ResolutionError("Qt GL runtime package is invalid")
        _require_exact_keys(
            package_value,
            {"architecture", "name", "status", "version"},
            "Qt GL runtime package",
        )
        name = package_value.get("name")
        architecture = package_value.get("architecture")
        status = package_value.get("status")
        if (
            name != expected_name
            or architecture != expected_architecture
            or status != _OS_RUNTIME_STATUS
        ):
            raise ResolutionError("Qt GL runtime package identity is invalid")
        packages.append(
            {
                "architecture": expected_architecture,
                "name": expected_name,
                "status": _OS_RUNTIME_STATUS,
                "version": _required_debian_version(
                    package_value.get("version"), "Qt GL runtime package version"
                ),
            }
        )
    libraries = value.get("required_libraries")
    if not isinstance(libraries, list) or tuple(libraries) != _REQUIRED_QT_GL_LIBRARIES:
        raise ResolutionError("Qt GL runtime libraries are invalid")
    return {
        "manager": _OS_RUNTIME_MANAGER,
        "packages": packages,
        "required_libraries": list(_REQUIRED_QT_GL_LIBRARIES),
    }


def _dpkg_architecture(machine: str) -> str:
    try:
        return _DPKG_ARCHITECTURES[machine]
    except KeyError as exc:
        raise ResolutionError("Qt GL runtime architecture is unsupported") from exc


def _required_debian_version(value: object, label: str) -> str:
    if not isinstance(value, str) or _DEBIAN_VERSION.fullmatch(value) is None:
        raise ResolutionError(f"{label} is invalid")
    return value


def _machine() -> str:
    machine = platform.machine().lower()
    if machine not in _MACHINES:
        raise ResolutionError("resolution host architecture is unsupported")
    return machine


def _glibc_version() -> str:
    library, version = platform.libc_ver()
    if library != "glibc" or _GLIBC_VERSION.fullmatch(version) is None:
        raise ResolutionError("resolution host must report a glibc version")
    return version


def _ubuntu_release(path: Path = Path("/etc/os-release")) -> tuple[str, str]:
    """Require the release target rather than inferring it from glibc alone."""
    fields = _read_os_release_fields(path)
    if (
        fields.get("ID") != _REQUIRED_OS_ID
        or fields.get("VERSION_ID") != _REQUIRED_OS_VERSION
    ):
        raise ResolutionError("resolution host must be Ubuntu 24.04")
    return _REQUIRED_OS_ID, _REQUIRED_OS_VERSION


def _debian_release(path: Path = Path("/etc/os-release")) -> tuple[str, str]:
    """Require the Debian 13 target rather than accepting a generic Linux host."""
    fields = _read_os_release_fields(path)
    if fields.get("ID") != "debian" or fields.get("VERSION_ID") != "13":
        raise ResolutionError("resolution host must be Debian 13")
    return "debian", "13"


def _target_os_release(os_id: str) -> tuple[str, str]:
    if os_id == _REQUIRED_OS_ID:
        return _ubuntu_release()
    if os_id == "debian":
        return _debian_release()
    raise ResolutionError("resolution target OS is unsupported")


def _read_os_release_fields(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ResolutionError("resolution host OS release is unavailable") from exc
    fields: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name in {"ID", "VERSION_ID"}:
            fields[name] = value.strip().strip('"')
    return fields


def _publish_evidence(
    artifact_bytes: Mapping[str, bytes],
    output: Path,
    checkout: Path,
) -> None:
    """Publish the complete evidence directory through one no-replace rename."""
    expected_names = set(artifact_bytes)
    if len(expected_names) != 2:
        raise ResolutionError("resolution publication must contain two artifacts")
    parent_descriptor: int | None = None
    stage_descriptor: int | None = None
    stage_name = ""
    published_status: os.stat_result | None = None
    published = False
    try:
        parent_descriptor = _open_output_parent(output, checkout)
        stage_name, stage_descriptor = _create_output_stage(
            parent_descriptor, output.name
        )
        _assert_output_parent_path_matches_descriptor(output.parent, parent_descriptor)
        published_status = _write_artifacts_to_stage(artifact_bytes, stage_descriptor)
        _assert_output_parent_path_matches_descriptor(output.parent, parent_descriptor)
        _assert_output_stage_bound(parent_descriptor, stage_name, stage_descriptor)
        _rename_no_replace(
            stage_descriptor,
            "artifacts",
            parent_descriptor,
            output.name,
        )
        published = True
        _assert_output_parent_path_matches_descriptor(output.parent, parent_descriptor)
        _assert_published_artifacts(
            parent_descriptor,
            output.name,
            published_status,
            expected_names,
        )
    except FileExistsError as exc:
        raise ResolutionError("resolution output appeared before publication") from exc
    except (_TrialBuildError, OSError) as exc:
        raise ResolutionError("resolution output could not be published") from exc
    finally:
        if stage_descriptor is not None and parent_descriptor is not None:
            if published:
                _remove_empty_output_stage(
                    parent_descriptor, stage_name, stage_descriptor
                )
            else:
                _cleanup_output_stage(parent_descriptor, stage_name, stage_descriptor)
            os.close(stage_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _run(command: Sequence[str], *, cwd: Path) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        timeout=600,
        env=_subprocess_environment(),
    )
    if completed.returncode != 0:
        raise ResolutionError("binary-only runtime installation failed")


def _run_pip_check(python: Path, cwd: Path) -> None:
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "check",
            "--isolated",
            "--no-input",
            "--disable-pip-version-check",
        ],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
        env=_subprocess_environment(),
    )
    if completed.returncode != 0:
        raise ResolutionError("fresh runtime dependency check failed")


def _pip_inspect(python: Path, cwd: Path) -> bytes:
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "inspect",
            "--isolated",
            "--no-input",
            "--disable-pip-version-check",
            "--local",
        ],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=False,
        timeout=120,
        env=_subprocess_environment(),
    )
    if completed.returncode != 0:
        raise ResolutionError("fresh runtime pip inspect failed")
    return completed.stdout


def _pip_version(python: Path, cwd: Path) -> str:
    completed = subprocess.run(
        [str(python), "-m", "pip", "--version", "--isolated", "--no-input"],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
        env=_subprocess_environment(),
    )
    if completed.returncode != 0:
        raise ResolutionError("fresh runtime pip version query failed")
    match = re.match(r"pip ([^ ]+)", completed.stdout)
    if match is None:
        raise ResolutionError("fresh runtime pip version output is invalid")
    return _required_version(match.group(1), "pip version")


def _python_version(python: Path, cwd: Path) -> str:
    completed = subprocess.run(
        [str(python), "-c", "import sys; print(sys.version.split()[0])"],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
        env=_subprocess_environment(),
    )
    if completed.returncode != 0:
        raise ResolutionError("fresh runtime Python version query failed")
    return completed.stdout.strip()


def _verify_python_isolation(python: Path, *, cwd: Path) -> None:
    """Reject user-site imports even when the interpreter is a conda Python."""
    sentinel_name = "gwexpy_trial_user_site_sentinel"
    try:
        with tempfile.TemporaryDirectory(
            prefix=".gwexpy-user-site-", dir=cwd
        ) as userbase_name:
            userbase = Path(userbase_name)
            site_packages = userbase / "lib" / "python3.12" / "site-packages"
            site_packages.mkdir(parents=True)
            (site_packages / f"{sentinel_name}.py").write_text(
                "sentinel = 'ambient-user-site'\n", encoding="ascii"
            )
            environment = _subprocess_environment()
            # Deliberately expose a benign fake userbase to prove the child
            # cannot import it; the production environment removes this key.
            environment["PYTHONUSERBASE"] = str(userbase)
            completed = subprocess.run(
                (
                    str(python),
                    "-c",
                    "import importlib.util, json, site, sys; "
                    "print(json.dumps({"
                    "'enable_user_site': site.ENABLE_USER_SITE, "
                    "'no_user_site': sys.flags.no_user_site, "
                    f"'sentinel': importlib.util.find_spec('{sentinel_name}') "
                    "is not None"
                    "}, sort_keys=True))",
                ),
                cwd=cwd,
                capture_output=True,
                check=False,
                text=True,
                timeout=60,
                env=environment,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolutionError("fresh conda Python isolation probe failed") from exc
    if completed.returncode != 0:
        raise ResolutionError("fresh conda Python isolation probe failed")
    try:
        evidence = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResolutionError("fresh conda Python isolation probe is invalid") from exc
    if evidence != {
        "enable_user_site": False,
        "no_user_site": 1,
        "sentinel": False,
    }:
        raise ResolutionError("fresh conda Python user-site isolation is invalid")


def _verify_runtime_imports(
    python: Path,
    *,
    cwd: Path,
    checkout_root: Path,
    require_studio: bool,
) -> dict[str, object]:
    """Prove imports resolve from the fresh prefix, not ambient checkout state."""
    prefix = python.resolve(strict=True).parent.parent
    checkout = checkout_root.resolve(strict=True)
    required = ("gwexpy_studio", "gwexpy", "numpy", "PySide6")
    if not require_studio:
        required = tuple(name for name in required if name != "gwexpy_studio")
    code = (
        "import importlib, importlib.util, json, os, pathlib, sys\n"
        f"_required = {required!r}\n"
        "_studio = importlib.util.find_spec('gwexpy_studio')\n"
        "_modules = {}\n"
        "for _name in _required:\n"
        "    _module = importlib.import_module(_name)\n"
        "    _path = getattr(_module, '__file__', None)\n"
        "    if not isinstance(_path, str):\n"
        "        raise RuntimeError('runtime import has no file')\n"
        "    _modules[_name] = str(pathlib.Path(_path).resolve())\n"
        "print(json.dumps({'python': list(sys.version_info[:2]),\n"
        "'no_user_site': sys.flags.no_user_site,\n"
        "'pythonpath': 'PYTHONPATH' in os.environ,\n"
        "'studio': _studio is not None,\n"
        "'modules': _modules,\n"
        "'sys_path': [str(pathlib.Path(_item or '.').resolve()) "
        "for _item in sys.path]},\n"
        "sort_keys=True))\n"
    )
    try:
        completed = subprocess.run(
            (str(python), "-I", "-c", code),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=120,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolutionError("fresh runtime import probe failed") from exc
    if completed.returncode != 0:
        raise ResolutionError("fresh runtime import probe failed")
    try:
        evidence = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResolutionError("fresh runtime import probe is invalid") from exc
    if not isinstance(evidence, Mapping):
        raise ResolutionError("fresh runtime import probe is invalid")
    if evidence.get("python") != [3, 12] or evidence.get("no_user_site") != 1:
        raise ResolutionError("fresh runtime Python identity is invalid")
    if evidence.get("pythonpath") is not False:
        raise ResolutionError("fresh runtime PYTHONPATH isolation is invalid")
    if evidence.get("studio") is not require_studio:
        raise ResolutionError("fresh runtime Studio visibility is invalid")
    modules = evidence.get("modules")
    sys_path = evidence.get("sys_path")
    if not isinstance(modules, Mapping) or not isinstance(sys_path, list):
        raise ResolutionError("fresh runtime import probe is invalid")
    for value in sys_path:
        if not isinstance(value, str):
            raise ResolutionError("fresh runtime sys.path is invalid")
        try:
            Path(value).relative_to(checkout)
        except ValueError:
            pass
        else:
            raise ResolutionError("fresh runtime sys.path reaches the checkout")
    for name in required:
        value = modules.get(name)
        if not isinstance(value, str):
            raise ResolutionError("fresh runtime module origin is invalid")
        try:
            Path(value).relative_to(prefix)
        except ValueError as exc:
            raise ResolutionError(
                "fresh runtime module did not import from the prefix"
            ) from exc
    return {
        "checkout_on_sys_path": False,
        "imports": list(required),
        "no_user_site": True,
        "python": "3.12",
        "pythonpath": False,
        "studio_visible": require_studio,
    }


def validate_import_isolation(
    value: object, *, require_studio: bool
) -> dict[str, object]:
    """Validate bounded import-isolation evidence without retaining paths."""
    if not isinstance(value, Mapping):
        raise ResolutionError("runtime import isolation evidence is invalid")
    required = ["gwexpy", "numpy", "PySide6"]
    if require_studio:
        required.insert(0, "gwexpy_studio")
    expected = {
        "checkout_on_sys_path": False,
        "imports": required,
        "no_user_site": True,
        "python": "3.12",
        "pythonpath": False,
        "studio_visible": require_studio,
    }
    if dict(value) != expected:
        raise ResolutionError("runtime import isolation evidence is invalid")
    return expected


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {
            "CONDA_DEFAULT_ENV",
            "CONDA_PREFIX",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        } or key.startswith("PIP_"):
            environment.pop(key, None)
    # Conda leaves Python's user-site import path enabled by default.  Keep
    # ambient packages from shadowing a freshly constructed trial prefix.
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _constraints_filename(machine: str, target_id: str = "wsl2-ubuntu24") -> str:
    try:
        return _trial_target(target_id).constraints_filename(machine)
    except _TrialTargetError as exc:
        raise ResolutionError("resolution target architecture is unsupported") from exc


def _resolution_filename(machine: str, target_id: str = "wsl2-ubuntu24") -> str:
    try:
        return _trial_target(target_id).resolution_filename(machine)
    except _TrialTargetError as exc:
        raise ResolutionError("resolution target architecture is unsupported") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-target", choices=_target_ids())
    parser.add_argument(
        "--backend",
        choices=("conda",),
        help="Explicit environment backend for new target resolution captures.",
    )
    parser.add_argument(
        "--conda",
        "--conda-executable",
        dest="conda_executable",
        type=Path,
        help="Conda executable used for fresh target environments.",
    )
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--trial-manifest", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--staging-manifest", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Capture one native runtime closure without retaining raw pip URLs or paths."""
    arguments = _parser().parse_args(argv)
    try:
        constraints, resolution = capture_trial_resolution(
            wheel=arguments.wheel,
            trial_manifest=arguments.trial_manifest,
            source_manifest=arguments.source_manifest,
            staging_manifest=arguments.staging_manifest,
            checkout_root=arguments.checkout,
            output_directory=arguments.output,
            trial_target_id=arguments.trial_target,
            environment_backend=arguments.backend,
            conda_executable=arguments.conda_executable,
        )
    except ResolutionError as exc:
        print(f"capture_trial_resolution: error: {exc}", file=sys.stderr)
        return 1
    print(constraints.name)
    print(resolution.name)
    return 0


if __name__ == "__main__":  # pragma: no cover - direct CLI invocation.
    raise SystemExit(main())
