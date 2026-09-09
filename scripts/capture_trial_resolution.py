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
import uuid
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
        GateError as _GateError,
    )
    from .run_trial_technical_gate import (
        gate_environment as _technical_gate_environment,
    )
    from .run_trial_technical_gate import (
        read_gate_result as _read_gate_result,
    )
    from .run_trial_technical_gate import (
        technical_gate_command as _technical_gate_command,
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
        GateError as _GateError,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        gate_environment as _technical_gate_environment,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        read_gate_result as _read_gate_result,
    )
    from run_trial_technical_gate import (  # type: ignore[no-redef]
        technical_gate_command as _technical_gate_command,
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
_MACHINES = frozenset({"x86_64", "aarch64"})
_REQUIRED_OS_ID = "ubuntu"
_REQUIRED_OS_VERSION = "24.04"
_STUDIO_NAME = "gwexpy-studio"
_OS_RUNTIME_MANAGER = "dpkg"
_OS_RUNTIME_PACKAGES = ("libegl1", "libgl1")
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
            f"gwexpy-gate-{uuid.uuid4().hex}-",
        )
        completed = subprocess.run(
            _technical_gate_command(
                python=python,
                gate_fd=gate_fd,
                checkout_root=checkout_root,
                work_root=work_root,
                result_path=result_path,
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
        raise ResolutionError("installed technical gate failed")
    try:
        result = _read_gate_result(
            _read_regular_bytes(result_path, "technical-gate result")
        )
    except _GateError as exc:
        raise ResolutionError("installed technical gate result is invalid") from exc
    if result["status"] != "passed":
        raise ResolutionError("installed technical gate failed")
    return result


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


def capture_trial_resolution(
    *,
    wheel: Path,
    trial_manifest: Path,
    source_manifest: Path,
    staging_manifest: Path,
    checkout_root: Path,
    output_directory: Path,
) -> tuple[Path, Path]:
    """Resolve and re-install an exact runtime closure in two fresh venvs."""
    artifact = load_trial_artifact(
        wheel=wheel,
        trial_manifest=trial_manifest,
        source_manifest=source_manifest,
        staging_manifest=staging_manifest,
    )
    machine = _machine()
    glibc_version = _glibc_version()
    os_id, os_version = _ubuntu_release()
    try:
        checkout = _trial_source_root(checkout_root)
        verify_capture_checkout(checkout, artifact)
        output = _trial_output_directory(output_directory, checkout)
        temporary_parent = _trial_temporary_parent(checkout)
    except _TrialBuildError as exc:
        raise ResolutionError("resolution output location is unsafe") from exc
    os_runtime = capture_os_runtime(machine, cwd=checkout)
    with tempfile.TemporaryDirectory(
        prefix=".gwexpy-studio-resolution-",
        dir=temporary_parent,
    ) as name:
        workspace = Path(name)
        sealed_wheel = _seal_wheel(workspace, artifact)
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
        constraints_bytes = runtime_constraints(
            phase_one_report, artifact=artifact
        ).encode("utf-8")
        constraints_path = workspace / _constraints_filename(machine)
        constraints_path.write_bytes(constraints_bytes)

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
            )
        pip_version = _pip_version(phase_two.python, workspace)
        projection = build_resolution_projection(
            artifact=artifact,
            machine=machine,
            glibc_version=glibc_version,
            os_runtime=os_runtime,
            python_version=_python_version(phase_two.python, workspace),
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
            os_id=os_id,
            os_version=os_version,
        )
        if projection["constraints_sha256"] != _sha256(constraints_bytes):
            raise ResolutionError("generated constraints digest does not match closure")
        constraints_name = _constraints_filename(machine)
        resolution_name = _resolution_filename(machine)
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
        if (
            not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (sealed.st_dev, sealed.st_ino)
        ):
            raise ResolutionError("technical-gate script seal name changed")
        os.unlink(destination)
        readonly_descriptor = os.open(
            f"/proc/self/fd/{descriptor}",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        readonly = os.fstat(readonly_descriptor)
        if (
            not stat.S_ISREG(readonly.st_mode)
            or (readonly.st_dev, readonly.st_ino) != (sealed.st_dev, sealed.st_ino)
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


def _phase_projection(
    *,
    report: Mapping[str, object],
    inspect: Mapping[str, object],
    artifact: TrialArtifact,
    expected_pip_version: str,
    report_bytes: bytes | None,
    inspect_bytes: bytes | None,
) -> dict[str, object]:
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
    artifacts = _selected_artifacts(report, artifact=artifact)
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
    report: Mapping[str, object], *, artifact: TrialArtifact
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
    if studio is None:
        raise ResolutionError("pip report omitted the trial Studio wheel")
    if (
        studio.filename != artifact.wheel_filename
        or studio.sha256 != artifact.wheel_sha256
        or studio.version != artifact.version
    ):
        raise ResolutionError("pip report Studio wheel does not match trial identity")
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
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ResolutionError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ResolutionError(f"{label} must be a JSON object")
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


def capture_os_runtime(machine: str, *, cwd: Path) -> dict[str, object]:
    """Capture the direct OS packages and SONAMEs required by Qt's GL runtime."""
    expected_architecture = _dpkg_architecture(machine)
    try:
        completed = subprocess.run(
            (
                "dpkg-query",
                "--show",
                f"--showformat={_DPKG_QUERY_FORMAT}",
                *_OS_RUNTIME_PACKAGES,
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
    if len(lines) != len(_OS_RUNTIME_PACKAGES):
        raise ResolutionError("Qt GL runtime package query is incomplete")
    for expected_name, line in zip(_OS_RUNTIME_PACKAGES, lines, strict=True):
        fields = line.split("\t")
        if len(fields) != 4:
            raise ResolutionError("Qt GL runtime package query is malformed")
        status, name, version, architecture = fields
        if status != "ii ":
            raise ResolutionError("Qt GL runtime package is not installed")
        if name != expected_name or architecture != expected_architecture:
            raise ResolutionError("Qt GL runtime package identity is invalid")
        packages.append(
            {
                "architecture": architecture,
                "name": name,
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
    )
    for library in _REQUIRED_QT_GL_LIBRARIES:
        try:
            ctypes.CDLL(library)
        except OSError as exc:
            raise ResolutionError("Qt GL runtime library cannot load") from exc
    return runtime


def validate_os_runtime(value: object, *, machine: str) -> dict[str, object]:
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
    expected_architecture = _dpkg_architecture(machine)
    packages_value = value.get("packages")
    if not isinstance(packages_value, list) or len(packages_value) != len(
        _OS_RUNTIME_PACKAGES
    ):
        raise ResolutionError("Qt GL runtime packages are invalid")
    packages: list[dict[str, str]] = []
    for expected_name, package_value in zip(
        _OS_RUNTIME_PACKAGES, packages_value, strict=True
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
    if (
        fields.get("ID") != _REQUIRED_OS_ID
        or fields.get("VERSION_ID") != _REQUIRED_OS_VERSION
    ):
        raise ResolutionError("resolution host must be Ubuntu 24.04")
    return _REQUIRED_OS_ID, _REQUIRED_OS_VERSION


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


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {
            "CONDA_DEFAULT_ENV",
            "CONDA_PREFIX",
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        } or key.startswith("PIP_"):
            environment.pop(key, None)
    return environment


def _constraints_filename(machine: str) -> str:
    return f"constraints-ubuntu24-{machine}.txt"


def _resolution_filename(machine: str) -> str:
    return f"resolution-ubuntu24-{machine}.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
        )
    except ResolutionError as exc:
        print(f"capture_trial_resolution: error: {exc}", file=sys.stderr)
        return 1
    print(constraints.name)
    print(resolution.name)
    return 0


if __name__ == "__main__":  # pragma: no cover - direct CLI invocation.
    raise SystemExit(main())
