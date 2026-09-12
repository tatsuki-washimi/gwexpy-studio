"""Run one source-bound native platform qualification.

The controller keeps the technical and owner stages in one temporary runtime,
removes both temporary conda prefixes, and writes the final canonical result
only after cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

_BOOTSTRAP_SNAPSHOT: dict[str, bytes] = {}
_BOOTSTRAP_SCRIPTS: dict[str, bytes] = {}


def _bootstrap_read(path: Path) -> bytes:
    """Read a kit file using only stdlib code before helper imports."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeError("kit entry is not a regular file")
        value = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            value.extend(chunk)
        final = os.fstat(descriptor)
        if (
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        ) != (
            final.st_dev,
            final.st_ino,
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise RuntimeError("kit entry changed while being read")
        return bytes(value)
    finally:
        os.close(descriptor)


def _bootstrap_runner_bytes(
    target_id: str, archive_name: str, sidecar_name: str
) -> bytes:
    """Render the runner without importing any kit-provided module."""
    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
CONDA_EXE="${{CONDA_EXE:-conda}}"
command -v "$CONDA_EXE" >/dev/null 2>&1 || {{
  echo "conda is required" >&2
  exit 1
}}
# conda create --prefix python=3.12 is performed by the verified controller
# after checksum, source, kit, and host preconditions have passed.
"$CONDA_EXE" run --no-capture-output -n base \\
  python -I -c '
import pathlib,sys
r=pathlib.Path(sys.argv[1]); p=pathlib.Path(sys.argv[2])
rb=r.read_bytes(); b=p.read_bytes()
ns={{"__name__":"__main__","__file__":str(p),"__a3_runner_bytes__":rb,"__a3_controller_bytes__":b}}
sys.argv=[str(p), *sys.argv[3:]]
exec(compile(b,str(p),"exec"),ns)
' \\
  "$0" \\
  "$SCRIPT_DIR/run_platform_qualification.py" \\
  --kit "$SCRIPT_DIR" \\
  --trial-target {shlex.quote(target_id)} \\
  --archive "$SCRIPT_DIR/{shlex.quote(archive_name)}" \\
  --sidecar "$SCRIPT_DIR/{shlex.quote(sidecar_name)}"
status=$?
exit $status
""".encode()


def _bootstrap_verify_kit() -> dict[str, bytes]:
    """Verify all executable kit bytes before importing any kit helper.

    This function intentionally uses only the Python standard library.  It is
    called while this file is still being imported in direct kit execution, so
    an extra ``json.py``/``subprocess.py`` beside the kit cannot shadow the
    standard library before provenance checks run.
    """
    if __name__ != "__main__":
        return {}
    try:
        kit_arg = next(
            sys.argv[index + 1]
            for index, argument in enumerate(sys.argv[:-1])
            if argument == "--kit"
        )
    except StopIteration:
        return {}
    kit = Path(kit_arg)
    manifest_bytes = _bootstrap_read(kit / "QUALIFICATION-KIT.json")
    manifest = json.loads(manifest_bytes)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 2
        or manifest.get("legacy_fixture") is True
        or _canonical_bootstrap(manifest) != manifest_bytes
    ):
        raise RuntimeError("kit manifest failed pre-execution verification")
    records = manifest.get("scripts")
    if not isinstance(records, list):
        raise RuntimeError("kit script records are invalid")
    archive = manifest.get("archive")
    sidecar = manifest.get("sidecar")
    runner = manifest.get("runner")
    if not all(isinstance(item, dict) for item in (archive, sidecar, runner)):
        raise RuntimeError("kit identity records are invalid")
    allowed = {
        "QUALIFICATION-KIT.json",
        "SOURCE-MANIFEST.json",
        cast(dict[str, object], archive).get("filename"),
        cast(dict[str, object], sidecar).get("filename"),
        cast(dict[str, object], runner).get("filename"),
    }
    snapshot: dict[str, bytes] = {
        "QUALIFICATION-KIT.json": manifest_bytes,
    }
    script_names: set[str] = set()
    supplied_controller = globals().get("__a3_controller_bytes__")
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"filename", "sha256", "source_path"}
            or not isinstance(record.get("filename"), str)
            or record.get("source_path") != f"scripts/{record.get('filename')}"
        ):
            raise RuntimeError("kit script record is invalid")
        filename = cast(str, record["filename"])
        if not filename or "/" in filename or filename in script_names:
            raise RuntimeError("kit script filename is invalid")
        if filename == "run_platform_qualification.py" and isinstance(
            supplied_controller, bytes
        ):
            content = supplied_controller
        else:
            content = _bootstrap_read(kit / filename)
        if hashlib.sha256(content).hexdigest() != record.get("sha256"):
            raise RuntimeError("kit script bytes are not source-bound")
        script_names.add(filename)
        snapshot[filename] = content
    allowed.update(script_names)
    entries = {path.name for path in kit.iterdir()}
    if entries != allowed:
        raise RuntimeError("kit contains an unmanifested entry")
    archive_record = cast(dict[str, object], archive)
    sidecar_record = cast(dict[str, object], sidecar)
    archive_name = archive_record.get("filename")
    sidecar_name = sidecar_record.get("filename")
    if not isinstance(archive_name, str) or not isinstance(sidecar_name, str):
        raise RuntimeError("kit release filenames are invalid")
    archive_bytes = _bootstrap_read(kit / archive_name)
    sidecar_bytes = _bootstrap_read(kit / sidecar_name)
    snapshot[archive_name] = archive_bytes
    snapshot[sidecar_name] = sidecar_bytes
    if hashlib.sha256(archive_bytes).hexdigest() != archive_record.get("sha256"):
        raise RuntimeError("kit archive bytes are not source-bound")
    if hashlib.sha256(sidecar_bytes).hexdigest() != sidecar_record.get("sha256"):
        raise RuntimeError("kit sidecar bytes are not source-bound")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive_zip:
        root = archive_name.removesuffix(".zip") + "/"
        embedded = {
            info.filename[len(root) :]: archive_zip.read(info)
            for info in archive_zip.infolist()
            if info.filename.startswith(root)
        }
    source = _bootstrap_read(kit / "SOURCE-MANIFEST.json")
    snapshot["SOURCE-MANIFEST.json"] = source
    if embedded.get("SOURCE-MANIFEST.json") != source:
        raise RuntimeError("loose source manifest differs from release snapshot")
    if hashlib.sha256(source).hexdigest() != manifest.get("source_manifest_sha256"):
        raise RuntimeError("source manifest hash is not source-bound")
    source_payload = json.loads(source)
    source_entries = {
        item.get("path"): item
        for item in source_payload.get("entries", [])
        if isinstance(item, dict)
    }
    expected_scripts = {f"scripts/{name}" for name in script_names}
    if {
        path for path in source_entries if path.startswith("scripts/")
    } != expected_scripts:
        raise RuntimeError("kit script set differs from source manifest")
    for name in script_names:
        source_entry = source_entries[f"scripts/{name}"]
        if source_entry.get("sha256") != next(
            item["sha256"] for item in records if item["filename"] == name
        ):
            raise RuntimeError("kit script hash differs from source manifest")
    runner_record = cast(dict[str, object], runner)
    runner_name = runner_record.get("filename")
    if runner_name != "run-qualification.sh":
        raise RuntimeError("kit runner name is invalid")
    runner_bytes = globals().get("__a3_runner_bytes__")
    if not isinstance(runner_bytes, bytes):
        runner_bytes = _bootstrap_read(kit / runner_name)
    if hashlib.sha256(runner_bytes).hexdigest() != runner_record.get("sha256"):
        raise RuntimeError("kit runner bytes are not source-bound")
    expected_runner = _bootstrap_runner_bytes(
        cast(str, manifest["target_id"]), archive_name, sidecar_name
    )
    if runner_bytes != expected_runner:
        raise RuntimeError("kit runner is not reproducible from verified inputs")
    snapshot[runner_name] = runner_bytes
    return snapshot


def _canonical_bootstrap(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


_BOOTSTRAP_ERROR_CODES = frozenset(
    {
        "checksum-mismatch",
        "source-mismatch",
        "kit-mismatch",
        "target-mismatch",
        "host-mismatch",
        "precondition-failed",
        "environment-failed",
        "runner-failed",
    }
)


def _bootstrap_failure_code(error: str) -> str:
    lowered = error.lower()
    if "source" in lowered:
        return "source-mismatch"
    if "archive" in lowered or "sidecar" in lowered or "checksum" in lowered:
        return "checksum-mismatch"
    if "target" in lowered:
        return "target-mismatch"
    if "precondition" in lowered:
        return "precondition-failed"
    return "kit-mismatch"


def _bootstrap_untrusted_bytes(error_code: str) -> bytes:
    if error_code not in _BOOTSTRAP_ERROR_CODES:
        error_code = "kit-mismatch"
    return _canonical_bootstrap(
        {
            "architecture": None,
            "automated_checks": {},
            "build_id": None,
            "cleanup": None,
            "error_code": error_code,
            "host": {},
            "kit_manifest_sha256": None,
            "owner_confirmations": {},
            "schema": 2,
            "source_sha": None,
            "stage": "preflight",
            "status": "failed",
            "target_id": None,
            "wheel_sha256": None,
            "zip_sha256": None,
        }
    )


def _bootstrap_output_path() -> Path | None:
    try:
        output = next(
            Path(sys.argv[index + 1])
            for index, argument in enumerate(sys.argv[:-1])
            if argument == "--output"
        )
    except StopIteration:
        try:
            kit = next(
                Path(sys.argv[index + 1])
                for index, argument in enumerate(sys.argv[:-1])
                if argument == "--kit"
            )
        except StopIteration:
            return None
        output = kit / "results"
    return output / "qualification-untrusted.json"


def _bootstrap_write_once(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _bootstrap_record_failure(error: str) -> None:
    destination = _bootstrap_output_path()
    if destination is None:
        return
    try:
        _bootstrap_write_once(
            destination, _bootstrap_untrusted_bytes(_bootstrap_failure_code(error))
        )
    except (OSError, RuntimeError):
        return


class _VerifiedKitLoader:
    def __init__(self, name: str, source: bytes) -> None:
        self.name = name
        self.source = source

    def create_module(self, _spec: object) -> None:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = f"<verified-kit:{self.name}>"
        module.__cached__ = None
        exec(compile(self.source, module.__file__, "exec"), module.__dict__)


class _VerifiedKitFinder:
    def __init__(self, sources: Mapping[str, bytes]) -> None:
        self.sources = dict(sources)

    def find_spec(
        self, fullname: str, _path: object = None, _target: object = None
    ) -> object:
        source = self.sources.get(fullname)
        if source is None:
            return None
        return importlib.util.spec_from_loader(
            fullname,
            _VerifiedKitLoader(fullname, source),
            origin=f"<verified-kit:{fullname}>",
        )


def _install_verified_importer(snapshot: Mapping[str, bytes]) -> None:
    sources = {
        Path(filename).stem: content
        for filename, content in snapshot.items()
        if filename.endswith(".py")
    }
    if sources:
        sys.meta_path.insert(0, _VerifiedKitFinder(sources))


try:
    _BOOTSTRAP_SNAPSHOT = _bootstrap_verify_kit()
    _BOOTSTRAP_SCRIPTS = {
        Path(filename).stem: content
        for filename, content in _BOOTSTRAP_SNAPSHOT.items()
        if filename.endswith(".py")
    }
    _install_verified_importer(_BOOTSTRAP_SNAPSHOT)
except (
    OSError,
    RuntimeError,
    StopIteration,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
    zipfile.BadZipFile,
) as exc:
    if __name__ == "__main__":
        _bootstrap_record_failure(str(exc))
        print(f"qualification preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

try:
    from .capture_trial_resolution import (
        _archive_sha256,
        _conda_phase_packages,
        _filename_from_url,
        _fresh_conda_phase,
        _inspect_packages,
        _normalized_package_name,
        _pip_inspect,
        _run_pip_check,
        _verify_runtime_imports,
        pip_install_command,
        pip_runtime_install_command,
        run_installed_technical_gate,
        validate_conda_package_records,
        validate_import_isolation,
    )
    from .package_trial_release import TrialReleaseError, _verify_release_bytes
    from .trial_targets import TrialTargetError, trial_target
    from .trial_toolchain import (
        ToolchainError,
        conda_environment_manager_record_from_info,
        conda_subdir,
    )
    from .verify_platform_qualification import (
        QualificationError,
        qualification2_result_bytes,
        qualification2_untrusted_failure_bytes,
        read_qualification2_result,
        required_schema2_automated_checks,
        required_schema2_owner_confirmations,
    )
except ImportError:  # pragma: no cover - standalone kit execution.
    from capture_trial_resolution import (  # type: ignore[no-redef]
        _archive_sha256,
        _conda_phase_packages,
        _filename_from_url,
        _fresh_conda_phase,
        _inspect_packages,
        _normalized_package_name,
        _pip_inspect,
        _run_pip_check,
        _verify_runtime_imports,
        pip_install_command,
        pip_runtime_install_command,
        run_installed_technical_gate,
        validate_conda_package_records,
        validate_import_isolation,
    )
    from package_trial_release import (  # type: ignore[no-redef]
        TrialReleaseError,
        _verify_release_bytes,
    )
    from trial_targets import TrialTargetError, trial_target  # type: ignore[no-redef]
    from trial_toolchain import (  # type: ignore[no-redef]
        ToolchainError,
        conda_environment_manager_record_from_info,
        conda_subdir,
    )
    from verify_platform_qualification import (  # type: ignore[no-redef]
        QualificationError,
        qualification2_result_bytes,
        qualification2_untrusted_failure_bytes,
        read_qualification2_result,
        required_schema2_automated_checks,
        required_schema2_owner_confirmations,
    )


class PlatformQualificationError(RuntimeError):
    """Raised when a qualification cannot complete safely."""


_ALLOWED_ERRORS = frozenset(
    {
        "checksum-mismatch",
        "source-mismatch",
        "kit-mismatch",
        "target-mismatch",
        "host-mismatch",
        "precondition-failed",
        "conda-unavailable",
        "environment-failed",
        "import-isolation-failed",
        "automatic-failed",
        "owner-false",
        "owner-unanswered",
        "owner-cancelled",
        "interrupted",
        "cleanup-failed",
        "result-save-failed",
        "runner-failed",
    }
)
_BUILD_ID = re.compile(r"P-[0-9a-f]{7}-[0-9]{8}-r[1-9][0-9]*-a[1-9][0-9]*")


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def qualification_filename(target_id: str, architecture: str, build_id: str) -> str:
    """Return the target-, architecture-, and Build-ID-bound result filename."""
    try:
        target = trial_target(target_id)
        target.require_architecture(architecture)
    except TrialTargetError as exc:
        raise PlatformQualificationError(str(exc)) from exc
    if not isinstance(build_id, str) or _BUILD_ID.fullmatch(build_id) is None:
        raise PlatformQualificationError("invalid Build ID")
    return f"qualification-{target.id}-{architecture}-{build_id}.json"


def _read_regular(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            initial = os.fstat(descriptor)
            if not stat.S_ISREG(initial.st_mode):
                raise PlatformQualificationError(f"{label} is not a regular file")
            content = bytearray()
            while chunk := os.read(descriptor, 1024 * 1024):
                content.extend(chunk)
            final = os.fstat(descriptor)
            if (
                initial.st_dev,
                initial.st_ino,
                initial.st_size,
                initial.st_mtime_ns,
                initial.st_ctime_ns,
            ) != (
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
                final.st_ctime_ns,
            ):
                raise PlatformQualificationError(f"{label} changed while being read")
            return bytes(content)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PlatformQualificationError(f"{label} cannot be read") from exc


def _kit_archive_files(archive_name: str, archive_bytes: bytes) -> dict[str, bytes]:
    root = archive_name.removesuffix(".zip")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            result: dict[str, bytes] = {}
            for info in archive.infolist():
                prefix = f"{root}/"
                if not info.filename.startswith(prefix):
                    raise PlatformQualificationError(
                        "candidate archive root is invalid"
                    )
                result[info.filename[len(prefix) :]] = archive.read(info)
            return result
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise PlatformQualificationError("candidate archive cannot be read") from exc


def _kit_file_bytes(
    kit: Path, filename: str, label: str, snapshot: Mapping[str, bytes] | None
) -> bytes:
    if snapshot is not None and filename in snapshot:
        return snapshot[filename]
    return _read_regular(kit / filename, label)


def read_qualification_kit(
    kit_directory: Path, *, snapshot: Mapping[str, bytes] | None = None
) -> dict[str, Any]:
    """Read all kit bytes once and verify the kit manifest and release pair."""
    kit = Path(kit_directory)
    manifest_bytes = _kit_file_bytes(
        kit, "QUALIFICATION-KIT.json", "kit manifest", snapshot
    )
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformQualificationError("kit manifest is invalid") from exc
    expected_fields = {
        "archive",
        "build_id",
        "legacy_fixture",
        "runner",
        "schema",
        "scripts",
        "sidecar",
        "source_manifest_sha256",
        "source_sha",
        "target_id",
        "wheel",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_fields
        or manifest.get("schema") != 2
    ):
        raise PlatformQualificationError("kit manifest schema is invalid")
    if manifest.get("legacy_fixture") is True:
        raise PlatformQualificationError("legacy kit cannot qualify a new target")
    if _canonical_json(manifest) != manifest_bytes:
        raise PlatformQualificationError("kit manifest is not canonical")
    target_id = manifest.get("target_id")
    try:
        target = trial_target(cast(str, target_id))
    except (TrialTargetError, TypeError) as exc:
        raise PlatformQualificationError("kit target is invalid") from exc
    archive_record = manifest["archive"]
    sidecar_record = manifest["sidecar"]
    wheel_record = manifest["wheel"]
    runner_record = manifest["runner"]
    if not all(
        isinstance(item, dict)
        for item in (archive_record, sidecar_record, wheel_record, runner_record)
    ):
        raise PlatformQualificationError("kit identity records are invalid")
    archive_name = archive_record.get("filename")
    sidecar_name = sidecar_record.get("filename")
    if not isinstance(archive_name, str) or not isinstance(sidecar_name, str):
        raise PlatformQualificationError("kit release filenames are invalid")
    archive_bytes = _kit_file_bytes(kit, archive_name, "kit archive", snapshot)
    sidecar_bytes = _kit_file_bytes(kit, sidecar_name, "kit sidecar", snapshot)
    if _sha256(archive_bytes) != archive_record.get("sha256") or _sha256(
        sidecar_bytes
    ) != sidecar_record.get("sha256"):
        raise PlatformQualificationError("kit release bytes do not match manifest")
    try:
        release_manifest = _verify_release_bytes(
            archive_name, archive_bytes, sidecar_bytes
        )
    except (TrialReleaseError, OSError) as exc:
        raise PlatformQualificationError("kit release verification failed") from exc
    release_target = release_manifest.get("target")
    if not isinstance(release_target, dict) or release_target.get("id") != target.id:
        raise PlatformQualificationError("kit release target does not match manifest")
    if release_manifest.get("build", {}).get("id") != manifest.get("build_id"):
        raise PlatformQualificationError("kit Build ID does not match release")
    if release_manifest.get("build", {}).get("source_sha") != manifest.get(
        "source_sha"
    ):
        raise PlatformQualificationError("kit source SHA does not match release")
    source = _kit_archive_files(archive_name, archive_bytes)
    source_bytes = source.get("SOURCE-MANIFEST.json")
    if source_bytes is None or _sha256(source_bytes) != manifest.get(
        "source_manifest_sha256"
    ):
        raise PlatformQualificationError("kit source manifest does not match")
    loose_source = _kit_file_bytes(
        kit, "SOURCE-MANIFEST.json", "kit source manifest", snapshot
    )
    if loose_source != source_bytes:
        raise PlatformQualificationError("kit source manifest snapshot differs")
    if release_manifest.get("source", {}).get("manifest_sha256") != manifest.get(
        "source_manifest_sha256"
    ):
        raise PlatformQualificationError("release source manifest does not match kit")
    try:
        source_document = json.loads(source_bytes)
        source_entries = {
            item["path"]: item
            for item in source_document["entries"]
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformQualificationError("kit source manifest is invalid") from exc
    wheel_name = wheel_record.get("filename")
    if (
        not isinstance(wheel_name, str)
        or source.get(wheel_name) is None
        or _sha256(source[wheel_name]) != wheel_record.get("sha256")
    ):
        raise PlatformQualificationError("kit wheel does not match manifest")
    runner_name = runner_record.get("filename")
    runner_bytes = _kit_file_bytes(kit, runner_name, "kit runner", snapshot)
    if runner_name != "run-qualification.sh" or _sha256(
        runner_bytes
    ) != runner_record.get("sha256"):
        raise PlatformQualificationError("kit runner does not match manifest")
    if runner_bytes != _bootstrap_runner_bytes(target.id, archive_name, sidecar_name):
        raise PlatformQualificationError("kit runner is not reproducible")
    scripts = manifest.get("scripts")
    if not isinstance(scripts, list):
        raise PlatformQualificationError("kit scripts record is invalid")
    script_bytes: dict[str, bytes] = {}
    for record in scripts:
        if not isinstance(record, dict) or set(record) != {
            "filename",
            "sha256",
            "source_path",
        }:
            raise PlatformQualificationError("kit script record is invalid")
        filename = record["filename"]
        source_path = record["source_path"]
        if not isinstance(filename, str) or source_path != f"scripts/{filename}":
            raise PlatformQualificationError("kit script path is invalid")
        content = _kit_file_bytes(kit, filename, f"kit script {filename}", snapshot)
        if _sha256(content) != record["sha256"]:
            raise PlatformQualificationError("kit script hash does not match")
        source_record = source_entries.get(source_path)
        if (
            not isinstance(source_record, dict)
            or source_record.get("sha256") != record["sha256"]
        ):
            raise PlatformQualificationError("kit script is not release-source-bound")
        script_bytes[filename] = content
    embedded_scripts = {path for path in source_entries if path.startswith("scripts/")}
    if embedded_scripts != {f"scripts/{name}" for name in script_bytes}:
        raise PlatformQualificationError("kit script set differs from release source")
    return {
        "archive_bytes": archive_bytes,
        "archive_name": archive_name,
        "files": source,
        "kit": kit,
        "manifest": manifest,
        "manifest_bytes": manifest_bytes,
        "release_manifest": release_manifest,
        "scripts": script_bytes,
        "target": target,
    }


def _windows_value(script: str, *, cwd: Path) -> str:
    try:
        completed = subprocess.run(
            (
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlatformQualificationError("Windows host query failed") from exc
    if completed.returncode != 0:
        raise PlatformQualificationError("Windows host query failed")
    return completed.stdout.strip().replace("\r", "")


def _linux_os_release() -> dict[str, str]:
    try:
        return dict(
            line.split("=", 1)
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )
    except OSError as exc:
        raise PlatformQualificationError("host OS identity is unavailable") from exc


def validate_host(
    target_id: str, architecture: str | None = None, *, cwd: Path | None = None
) -> dict[str, str]:
    """Reject unknown, hosted, emulated, and mismatched physical hosts."""
    try:
        target = trial_target(target_id)
    except TrialTargetError as exc:
        raise PlatformQualificationError(str(exc)) from exc
    observed_architecture = (
        platform.machine().lower() if architecture is None else architecture
    )
    target.require_architecture(observed_architecture)
    work = Path.cwd() if cwd is None else Path(cwd)
    python_version = ".".join(str(value) for value in sys.version_info[:3])
    if target_id in {"ubuntu24-x86_64", "debian13-x86_64"}:
        if (
            sys.platform != "linux"
            or "microsoft" in platform.release().lower()
            or os.environ.get("WSL_INTEROP")
        ):
            raise PlatformQualificationError(
                "qualification requires a native Linux desktop"
            )
        fields = _linux_os_release()
        expected_id = target.os_id
        if (
            fields.get("ID", "").strip('"') != expected_id
            or fields.get("VERSION_ID", "").strip('"') != target.os_version
        ):
            raise PlatformQualificationError("host OS does not match target")
        return {
            "architecture": observed_architecture,
            "host_os": expected_id,
            "os_version": target.os_version,
            "python_version": python_version,
            "qt_backend": target.backend,
        }
    if target_id == "macos15-arm64":
        if sys.platform != "darwin" or observed_architecture != "arm64":
            raise PlatformQualificationError(
                "qualification requires native Apple Silicon macOS"
            )
        version = platform.mac_ver()[0]
        if not version or int(version.split(".", 1)[0]) < 15:
            raise PlatformQualificationError("qualification requires macOS 15 or newer")
        return {
            "architecture": observed_architecture,
            "macos_version": version,
            "python_version": python_version,
            "qt_backend": target.backend,
        }
    if (
        sys.platform != "linux"
        or "microsoft" not in platform.release().lower()
        or not os.environ.get("WSL_INTEROP")
    ):
        raise PlatformQualificationError("qualification requires WSL2")
    fields = _linux_os_release()
    if (
        fields.get("ID", "").strip('"') != "ubuntu"
        or fields.get("VERSION_ID", "").strip('"') != "24.04"
    ):
        raise PlatformQualificationError("WSL guest is not Ubuntu 24.04")
    values = _windows_value(
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "[Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE') + '|' + "
        "[Environment]::OSVersion.Version.Build + '|' + "
        "(Get-CimInstance Win32_OperatingSystem).ProductType",
        cwd=work,
    ).split("|")
    if len(values) != 3 or values[2] != "1":
        raise PlatformQualificationError("WSL host is Windows Server or unavailable")
    windows_architecture = {"AMD64": "x86_64", "ARM64": "aarch64"}.get(
        values[0].upper()
    )
    if (
        windows_architecture != observed_architecture
        or not values[1].isdigit()
        or int(values[1]) < 22000
    ):
        raise PlatformQualificationError(
            "Windows client or architecture does not match target"
        )
    return {
        "guest_architecture": observed_architecture,
        "guest_os": "ubuntu",
        "guest_version": "24.04",
        "kernel_release": platform.release(),
        "python_version": python_version,
        "qt_backend": target.backend,
        "windows_architecture": observed_architecture,
        "windows_version": f"11.0.{values[1]}",
    }


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ if environment is None else environment)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=False,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_owned_process(process)
            raise
        except KeyboardInterrupt:
            _terminate_owned_process(process)
            raise
        completed = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlatformQualificationError("qualification subprocess failed") from exc
    if completed.returncode != 0:
        raise PlatformQualificationError("qualification subprocess failed")
    return completed


def _terminate_owned_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate one owned process group and reap it within a bounded time."""
    # ``start_new_session=True`` gives every owned child a private process
    # group.  The group must be signalled even after its leader has exited:
    # a worker descendant can outlive the leader and otherwise leak into the
    # next qualification.  Never use a broad process-name or user kill.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise PlatformQualificationError(
                "owned subprocess could not be reaped"
            ) from exc
    # If the leader exited between the first signal and communicate(), a
    # descendant may still be alive.  A second bounded group signal closes
    # that race while the group identity remains owned by this Popen object.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def _conda_info(
    conda: Path, *, cwd: Path, target_id: str, architecture: str
) -> dict[str, object]:
    completed = _run((str(conda), "info", "--json"), cwd=cwd, timeout=60)
    try:
        info = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformQualificationError("conda info is invalid") from exc
    if not isinstance(info, dict):
        raise PlatformQualificationError("conda info is invalid")
    try:
        conda_environment_manager_record_from_info(
            target_id=target_id, architecture=architecture, info=info
        )
    except ToolchainError as exc:
        raise PlatformQualificationError("conda is not target-compatible") from exc
    return cast(dict[str, object], info)


def _runtime_python_version(python: Path, *, cwd: Path) -> str:
    completed = _run(
        (
            str(python),
            "-I",
            "-c",
            "import sys; print('.'.join(str(v) for v in sys.version_info[:3]))",
        ),
        cwd=cwd,
        timeout=60,
    )
    try:
        version = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise PlatformQualificationError("runtime Python version is invalid") from exc
    if not re.fullmatch(r"3\.12\.[0-9]+", version):
        raise PlatformQualificationError("runtime Python is not CPython 3.12")
    return version


def _extract_candidate(
    files: Mapping[str, bytes],
    root: Path,
    *,
    wheel_name: str,
    constraints_name: str,
    resolution_name: str,
) -> tuple[Path, Path, Path]:
    candidate = root / "candidate"
    candidate.mkdir()
    selected = {
        name: files.get(name)
        for name in (wheel_name, constraints_name, resolution_name)
    }
    if any(content is None for content in selected.values()):
        raise PlatformQualificationError("candidate runtime files are incomplete")
    for name, content in selected.items():
        assert content is not None
        (candidate / name).write_bytes(content)
    return candidate, candidate / wheel_name, candidate / constraints_name


def _pip_report_projection(report: Mapping[str, object]) -> list[dict[str, str]]:
    installs = report.get("install")
    if not isinstance(installs, list) or not installs:
        raise PlatformQualificationError("pip report closure is empty")
    projected: list[dict[str, str]] = []
    for item in installs:
        if not isinstance(item, Mapping):
            raise PlatformQualificationError("pip report closure is invalid")
        metadata = item.get("metadata")
        download = item.get("download_info")
        if not isinstance(metadata, Mapping) or not isinstance(download, Mapping):
            raise PlatformQualificationError("pip report closure is invalid")
        name = metadata.get("name")
        version = metadata.get("version")
        url = download.get("url")
        archive = download.get("archive_info")
        if not all(isinstance(value, str) for value in (name, version, url)):
            raise PlatformQualificationError("pip report closure is invalid")
        if not isinstance(archive, Mapping):
            raise PlatformQualificationError("pip report artifact hash is invalid")
        try:
            normalized_name = _normalized_package_name(name)
            filename = _filename_from_url(url)
            sha256 = _archive_sha256(archive)
        except Exception as exc:
            raise PlatformQualificationError(
                "pip report artifact identity is invalid"
            ) from exc
        projected.append(
            {
                "filename": filename,
                "name": normalized_name,
                "sha256": sha256,
                "version": version,
            }
        )
    projected.sort(key=lambda item: item["name"].encode("utf-8"))
    if len({item["name"] for item in projected}) != len(projected):
        raise PlatformQualificationError("pip report closure contains duplicates")
    return projected


def _verify_installed_phase(
    *,
    python: Path,
    conda: Path,
    prefix: Path,
    cwd: Path,
    checkout: Path,
    phase: Mapping[str, object],
    expected_subdir: str,
    require_studio: bool,
) -> None:
    """Compare each fresh environment's observed closure to resolution bytes."""
    expected_conda = validate_conda_package_records(phase.get("conda_packages"))
    observed_conda = _conda_phase_packages(
        conda_executable=conda,
        prefix=prefix,
        cwd=cwd,
        expected_subdir=expected_subdir,
    )
    if observed_conda != expected_conda:
        raise PlatformQualificationError(
            "environment closure does not match resolution"
        )
    report_path = cwd / (f"{prefix.name}-pip-report.json")
    # The caller writes the report at this deterministic location.
    try:
        report = json.loads(report_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformQualificationError("pip report cannot be verified") from exc
    expected_report = phase.get("report")
    if not isinstance(expected_report, Mapping) or {"artifacts"} != set(
        expected_report
    ):
        raise PlatformQualificationError("resolution pip report is invalid")
    if _pip_report_projection(report) != expected_report["artifacts"]:
        raise PlatformQualificationError("pip report closure does not match resolution")
    inspect = json.loads(_pip_inspect(python, cwd))
    if not isinstance(inspect, Mapping):
        raise PlatformQualificationError("pip inspect closure is invalid")
    expected_inspect = phase.get("inspect")
    if not isinstance(expected_inspect, Mapping) or set(expected_inspect) != {
        "installed"
    }:
        raise PlatformQualificationError("resolution pip inspect is invalid")
    if _inspect_packages(inspect) != expected_inspect["installed"]:
        raise PlatformQualificationError(
            "pip inspect closure does not match resolution"
        )
    import_evidence = _verify_runtime_imports(
        python,
        cwd=cwd,
        checkout_root=checkout,
        require_studio=require_studio,
    )
    expected_imports = phase.get("import_isolation")
    if not isinstance(expected_imports, Mapping):
        raise PlatformQualificationError("resolution import isolation is invalid")
    try:
        validate_import_isolation(expected_imports, require_studio=require_studio)
    except Exception as exc:
        raise PlatformQualificationError(
            "resolution import isolation is invalid"
        ) from exc
    if dict(import_evidence) != dict(expected_imports):
        raise PlatformQualificationError(
            "runtime import isolation differs from resolution"
        )


def _isolated_gui_environment(root: Path, *, purpose: str) -> dict[str, str]:
    """Give each GUI child owned user/config/cache and SHM namespaces."""
    environment = dict(os.environ)
    environment.pop("QT_QPA_PLATFORM", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["HOME"] = str(root / f"{purpose}-home")
    for name, suffix in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
    ):
        environment[name] = str(root / f"{purpose}-{suffix}")
    environment["MPLCONFIGDIR"] = str(root / f"{purpose}-mpl")
    digest = hashlib.sha256(f"{root}\0{purpose}".encode()).hexdigest()
    environment["GWEXPY_STUDIO_SHM_PREFIX"] = f"g3{digest[:10]}"
    return environment


def _probe_qt_backend(python: Path, target_id: str, root: Path) -> str:
    """Require a real native Qt backend from the installed runtime."""
    target = trial_target(target_id)
    environment = _isolated_gui_environment(root, purpose="qt")
    completed = _run(
        (
            str(python),
            "-I",
            "-c",
            "from PySide6.QtWidgets import QApplication; "
            "app=QApplication([]); print(app.platformName())",
        ),
        cwd=root,
        environment=environment,
        timeout=120,
    )
    try:
        backend = completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PlatformQualificationError("Qt backend probe is invalid") from exc
    try:
        target.require_backend(backend)
    except TrialTargetError as exc:
        raise PlatformQualificationError("host-mismatch") from exc
    return backend


_PHYSICAL_QT_PROBE = r"""
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from PySide6.QtGui import QOffscreenSurface, QOpenGLContext
from PySide6.QtWidgets import QApplication, QFileDialog

from gwexpy_studio.ui.app import _trial_capability_environment
from gwexpy_studio.ui.support import AboutDialog

target = sys.argv[1]
root = Path(sys.argv[2])
expected_build = sys.argv[3]
app = QApplication([])
screen = app.primaryScreen()
surface = QOffscreenSurface()
surface.create()
context = QOpenGLContext()
opengl = context.create() and surface.isValid() and context.makeCurrent(surface)
if opengl:
    context.doneCurrent()
token = "gwexpy-clipboard-" + uuid.uuid4().hex
app.clipboard().setText(token)
app.processEvents()
clipboard = app.clipboard().text() == token
dialog_path = str(root / "解析 結果.py")
original = QFileDialog.getSaveFileName
QFileDialog.getSaveFileName = staticmethod(
    lambda *_args, **_kwargs: (dialog_path, "Python Scripts (*.py)")
)
selected, _filter = QFileDialog.getSaveFileName(
    None, "Export", "export.py", "Python Scripts (*.py)"
)
QFileDialog.getSaveFileName = original
with _trial_capability_environment():
    about = AboutDialog()
    about.copy_diagnostics()
    diagnostics = app.clipboard().text()
result = {
    "about_build_id": f"Build ID: {expected_build}" in diagnostics,
    "clipboard": clipboard,
    "diagnostics_copy": "Build ID:" in diagnostics,
    "dpi_scaling": bool(
        screen
        and screen.logicalDotsPerInch() > 0
        and screen.devicePixelRatio() > 0
    ),
    "file_dialog_path": selected == dialog_path,
    "opengl": bool(opengl),
    "platform": app.platformName(),
    "screen": bool(
        screen
        and screen.geometry().width() > 0
        and screen.geometry().height() > 0
    ),
}
if target == "wsl2-ubuntu24":
    egl_gl = True
    try:
        for library in ("libEGL.so.1", "libGL.so.1"):
            ctypes.CDLL(library)
    except OSError:
        egl_gl = False
    getter = subprocess.run(
        (
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-Clipboard",
        ),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    incoming = "gwexpy-windows-" + uuid.uuid4().hex
    setter = subprocess.run(
        (
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Set-Clipboard -Value '{incoming}'",
        ),
        capture_output=True, check=False, text=True, timeout=30,
    )
    for _ in range(100):
        app.processEvents()
        if app.clipboard().text() == incoming:
            break
        time.sleep(0.02)
    result["qt_egl_gl"] = egl_gl
    result["windows_clipboard_roundtrip"] = (
        getter.returncode == 0
        and getter.stdout.strip() == token
        and setter.returncode == 0
        and app.clipboard().text() == incoming
    )
print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
"""


def _physical_qt_probe(
    python: Path, *, target_id: str, root: Path, build_id: str
) -> dict[str, object]:
    environment = _isolated_gui_environment(root, purpose="qt")
    completed = _run(
        (
            str(python),
            "-I",
            "-c",
            _PHYSICAL_QT_PROBE,
            target_id,
            str(root),
            build_id,
        ),
        cwd=root,
        environment=environment,
        timeout=180,
    )
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformQualificationError("Qt qualification probe is invalid") from exc
    if not isinstance(value, dict):
        raise PlatformQualificationError("Qt qualification probe is invalid")
    return value


def _wslg_display_available() -> bool:
    """Require the WSLg display contract before accepting a WSL probe."""
    return bool(
        os.environ.get("WAYLAND_DISPLAY")
        and os.environ.get("DISPLAY")
        and Path("/mnt/wslg").is_dir()
    )


def _unicode_space_path_roundtrip(root: Path) -> bool:
    directory = Path(tempfile.mkdtemp(prefix="GWexpy 試験 ", dir=root))
    try:
        payload = directory / "測定 データ.txt"
        payload.write_text("roundtrip\n", encoding="utf-8")
        return payload.read_text(encoding="utf-8") == "roundtrip\n"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _windows_path_roundtrip(cwd: Path) -> bool:
    windows_temp = _windows_value(
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "[IO.Path]::GetTempPath()",
        cwd=cwd,
    )
    translated = _run(("wslpath", "-u", windows_temp), cwd=cwd, timeout=30)
    try:
        parent = Path(translated.stdout.decode("utf-8").strip())
    except UnicodeDecodeError as exc:
        raise PlatformQualificationError("Windows temporary path is invalid") from exc
    directory = Path(tempfile.mkdtemp(prefix="GWexpy 試験 ", dir=parent))
    try:
        payload = directory / "測定 データ.txt"
        payload.write_text("roundtrip\n", encoding="utf-8")
        return payload.read_text(encoding="utf-8") == "roundtrip\n"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def _sealed_gate_script(content: bytes, root: Path) -> Any:
    """Yield an unlinked read-only descriptor containing verified gate bytes."""
    descriptor, name = tempfile.mkstemp(prefix=".sealed-gate.", dir=root)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        read_descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        path.unlink()
        try:
            yield read_descriptor
        finally:
            os.close(read_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if path.exists():
            path.unlink()


_OWNER_CHILD = r"""
import os
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from gwexpy_studio.ui.app import _trial_capability_environment, create_app

(
    stop_file,
    ready_file,
    operation_file,
    started_file,
    returned_file,
    closed_file,
    project_file,
) = sys.argv[1:]
project_path = Path(project_file).resolve()

def mark(path, value="ok"):
    with open(path, "w", encoding="ascii") as stream:
        stream.write(value + "\n")

def bridge_idle(window):
    bridge = getattr(window, "_bridge", None)
    return (
        getattr(window, "_io_capability_ready", False) is True
        and bridge is not None
        and getattr(window._bridge_state(), "value", "") == "idle"
        and not getattr(window, "_command_reserved", False)
        and not getattr(window, "_modal_active", False)
    )

def project_status(window):
    status = getattr(window, "_workspace_status", {})
    value = status.get("project_path")
    return status, Path(str(value)).resolve() if value else None

def main():
    with _trial_capability_environment():
        app, window = create_app((sys.argv[0],))
        app.setQuitOnLastWindowClosed(False)
        window.show()
        window.initialize_workspace()
        dialog_result = {"returned": False, "path": None}
        request_document_change = window._request_document_change

        def record_document_change(kind, payload):
            if kind == "open_project":
                dialog_result["returned"] = True
                value = payload.get("path")
                dialog_result["path"] = Path(str(value)).resolve() if value else None
            return request_document_change(kind, payload)

        # Keep the ordinary QFileDialog and request path untouched; this records
        # the completed dialog outcome so asynchronous workspace settling cannot
        # be mistaken for a user cancellation.
        window._request_document_change = record_document_change
        state = {"phase": "capability", "close_requested": False}

        def stop_when_requested():
            phase = state["phase"]
            if phase == "capability" and bridge_idle(window):
                project_path.parent.mkdir(parents=True, exist_ok=True)
                if window.save_project_to(str(project_path)):
                    state["phase"] = "save"
            elif phase == "save" and bridge_idle(window):
                status, current = project_status(window)
                if (
                    current == project_path
                    and status.get("dirty") is False
                    and status.get("needs_restore") is not True
                    and getattr(window, "_checkpoint_pending", False) is False
                    and not window.plot_canvas._view_timer.isActive()
                ):
                    window.close_project_action.trigger()
                    state["phase"] = "close"
            elif phase == "close" and bridge_idle(window):
                status, current = project_status(window)
                project = getattr(window, "_project", None)
                objects = getattr(project, "objects", ()) if project is not None else ()
                if not status.get("project_path") and current is None:
                    if not objects:
                        mark(ready_file)
                        state["phase"] = "ready"
            elif phase == "ready" and os.path.exists(operation_file):
                if window.open_project_action.isEnabled():
                    mark(started_file)
                    state["phase"] = "opening"
                    window.open_project_action.trigger()
                    if (
                        state["phase"] == "opening"
                        and dialog_result["path"] == project_path
                    ):
                        dialog_result["returned"] = True
                        state["phase"] = "open"
                    elif state["phase"] == "opening":
                        dialog_result["returned"] = True
                        if dialog_result["path"] is None:
                            state["phase"] = "cancelled"
                        else:
                            mark(returned_file, "failed")
                            state["phase"] = "await-stop"
            elif (
                phase in {"opening", "open", "reopen-save", "cancelled"}
                and bridge_idle(window)
            ):
                status, current = project_status(window)
                if (
                    current == project_path
                    and status.get("needs_restore") is not True
                    and status.get("dirty") is False
                    and getattr(window, "_checkpoint_pending", False) is False
                    and not window.plot_canvas._view_timer.isActive()
                ):
                    mark(returned_file, "selected")
                    state["phase"] = "await-stop"
                elif (
                    phase == "open"
                    and current == project_path
                    and status.get("needs_restore") is not True
                    and status.get("dirty") is True
                    and getattr(window, "_checkpoint_pending", False) is False
                    and not window.plot_canvas._view_timer.isActive()
                    and window.save_project_to(str(project_path))
                ):
                    state["phase"] = "reopen-save"
                elif phase == "cancelled" and (
                    not getattr(window, "_modal_active", False)
                    and getattr(window, "_checkpoint_pending", False) is False
                    and not window.plot_canvas._view_timer.isActive()
                ):
                    mark(returned_file, "cancelled")
                    state["phase"] = "await-stop"
            if os.path.exists(stop_file) and state["phase"] != "stopped":
                modal = QApplication.activeModalWidget()
                if modal is not None:
                    modal.reject()
                state["phase"] = "stopping"
                if (
                    not state["close_requested"]
                    and bridge_idle(window)
                    and getattr(window, "_checkpoint_pending", False) is False
                    and not window.plot_canvas._view_timer.isActive()
                ):
                    state["close_requested"] = True
                    # Let this timer callback return before entering the
                    # window's bounded bridge-close event loop.  The next
                    # timer ticks keep observing visibility and worker
                    # shutdown, including a close that completes later.
                    # The qualification owns this temporary project and has
                    # already completed the owner operation; authorize
                    # cleanup so the normal unsaved-project prompt cannot
                    # strand the child during automated shutdown.
                    window._workspace_close_authorized = True
                    QTimer.singleShot(0, window.close)
                bridge = getattr(window, "_bridge", None)
                thread = getattr(bridge, "worker_thread", None)
                if (
                    not window.isVisible()
                    and (thread is None or not thread.isRunning())
                ):
                    mark(closed_file, "closed")
                    state["phase"] = "stopped"
                    app.quit()
        timer = QTimer()
        timer.timeout.connect(stop_when_requested)
        timer.start(100)
        app.exec()

if __name__ == "__main__":
    main()
"""


def _owner_session(
    python: Path,
    *,
    target_id: str,
    root: Path,
    build_id: str,
    answers: dict[str, bool | None],
    reader: Callable[[str], str],
) -> None:
    """Keep the real Studio window alive while the owner performs checks."""
    del build_id
    stop_file = root / ".owner-stop"
    ready_file = root / ".owner-ready"
    operation_file = root / ".owner-operation"
    started_file = root / ".owner-operation-started"
    returned_file = root / ".owner-operation-returned"
    closed_file = root / ".owner-closed"
    project_file = root / "owner-project" / "日本語 空白" / "確認.gwxproj"
    environment = _isolated_gui_environment(root, purpose="owner")
    process = subprocess.Popen(
        (
            str(python),
            "-I",
            "-c",
            _OWNER_CHILD,
            str(stop_file),
            str(ready_file),
            str(operation_file),
            str(started_file),
            str(returned_file),
            str(closed_file),
            str(project_file),
        ),
        cwd=root,
        env=environment,
        start_new_session=os.name == "posix",
    )
    primary_error: BaseException | None = None
    try:
        _wait_owner_marker(process, ready_file, "owner Studio readiness")
        print(
            f"Native owner check / 所有者確認: select this Japanese and "
            f"space-containing project in the real native file dialog: {project_file}. "
            "Then check normal and one available alternate scale. Reply y/n/c.",
            flush=True,
        )
        operation_file.write_text("save-as\n", encoding="ascii")
        _wait_owner_marker(process, started_file, "owner native dialog start")
        _wait_owner_marker(process, returned_file, "owner native dialog completion")
        returned_marker = returned_file.read_text(encoding="ascii").strip()
        if returned_marker == "cancelled":
            raise PlatformQualificationError("owner-cancelled")
        if returned_marker != "selected":
            raise PlatformQualificationError("automatic-failed")
        _owner_prompt(target_id, reader=reader, answers=answers, announce=False)
    except (KeyboardInterrupt, PlatformQualificationError) as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: PlatformQualificationError | None = None
        try:
            stop_file.write_text("stop\n", encoding="ascii")
        except OSError:
            pass
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        # A zero exit from the owner leader does not prove that its worker
        # descendants exited.  Always reap the owned process group; the
        # helper is idempotent when the group is already gone.
        try:
            _terminate_owned_process(process)
        except PlatformQualificationError as exc:
            cleanup_error = exc
        try:
            closed_marker = (
                closed_file.read_text(encoding="ascii").strip()
                if closed_file.is_file()
                else ""
            )
        except OSError:
            closed_marker = ""
        if (
            cleanup_error is None
            and primary_error is None
            and (process.returncode != 0 or closed_marker != "closed")
        ):
            cleanup_error = PlatformQualificationError("automatic-failed")
        if cleanup_error is not None:
            raise cleanup_error


def _wait_owner_marker(
    process: subprocess.Popen[bytes], marker: Path, label: str, timeout: int = 60
) -> None:
    deadline = time.monotonic() + timeout
    while not marker.is_file():
        if process.poll() is not None:
            raise PlatformQualificationError(f"{label} failed")
        if time.monotonic() >= deadline:
            _terminate_owned_process(process)
            raise PlatformQualificationError(f"{label} timed out")
        time.sleep(0.1)


def _owner_prompt(
    target_id: str,
    *,
    reader: Callable[[str], str] = input,
    answers: dict[str, bool | None] | None = None,
    announce: bool = True,
) -> dict[str, bool | None]:
    """Ask only bounded native dialog and scale confirmations."""
    if announce:
        print(
            "Native owner check / 所有者確認: open a Japanese and "
            "space-containing path in the real native file dialog, "
            "then check display and operation at normal and one "
            "available alternate scale. Reply y/n/c.",
            flush=True,
        )
    values = answers if answers is not None else {}
    prompts = {
        "native_file_dialog": (
            "Native dialog opened and selected the Japanese/space path?"
        ),
        "normal_scale_display": "Normal scale display is readable?",
        "normal_scale_operation": "Normal scale operation works?",
        "alternate_scale_display": "Available alternate scale display is readable?",
        "alternate_scale_operation": "Available alternate scale operation works?",
    }
    for name in required_schema2_owner_confirmations(target_id):
        try:
            answer = reader(f"{prompts[name]} / {name} [y/n/c]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise PlatformQualificationError("owner-unanswered") from None
        if answer == "y":
            values[name] = True
        elif answer == "n":
            values[name] = False
        elif answer == "c":
            raise PlatformQualificationError("owner-cancelled")
        else:
            raise PlatformQualificationError("owner-unanswered")
    return values


def _cleanup_prefix(prefix: Path) -> None:
    if prefix.exists() or prefix.is_symlink():
        shutil.rmtree(prefix)
    if prefix.exists() or prefix.is_symlink():
        raise PlatformQualificationError("cleanup-failed")


def _write_result_once(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise PlatformQualificationError("result-save-failed")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link is an atomic no-overwrite installation on the same
        # filesystem.  ``rename`` would replace a raced result file.
        os.link(temporary, path)
        temporary.unlink()
    except OSError as exc:
        raise PlatformQualificationError("result-save-failed") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


@contextmanager
def _interrupt_guard() -> Any:
    """Convert handled termination signals into the closed interruption code."""
    previous: dict[int, Any] = {}

    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            previous[number] = signal.getsignal(number)
            signal.signal(number, stop)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_qualification(
    **kwargs: Any,
) -> Path:
    """Run one qualification with bounded signal handling."""
    with _interrupt_guard():
        return _run_qualification(**kwargs)


def _run_qualification(
    *,
    target_id: str,
    archive: Path | None = None,
    sidecar: Path | None = None,
    output_directory: Path | None = None,
    work_root: Path | None = None,
    kit: Path | None = None,
    conda_executable: Path | None = None,
    owner_reader: Callable[[str], str] | None = None,
) -> Path:
    """Run checksum, conda, automatic, owner, cleanup, and final-save stages."""
    del archive, sidecar  # release paths are accepted for legacy CLI shape only.
    output = Path(
        output_directory or (Path(kit) / "results" if kit is not None else "results")
    )

    def record_untrusted(error: str) -> None:
        code = error if error in _ALLOWED_ERRORS else "kit-mismatch"
        if "source" in error:
            code = "source-mismatch"
        elif "archive" in error or "sidecar" in error or "checksum" in error:
            code = "checksum-mismatch"
        try:
            _write_result_once(
                output / "qualification-untrusted.json",
                qualification2_untrusted_failure_bytes(
                    stage="preflight", error_code=code
                ),
            )
        except (OSError, PlatformQualificationError):
            return

    if kit is None:
        record_untrusted("kit-mismatch")
        raise PlatformQualificationError("kit-mismatch")
    try:
        payload = read_qualification_kit(kit, snapshot=_BOOTSTRAP_SNAPSHOT or None)
    except PlatformQualificationError as exc:
        record_untrusted(str(exc))
        raise
    manifest = cast(dict[str, object], payload["manifest"])
    if manifest.get("target_id") != target_id:
        record_untrusted("target-mismatch")
        raise PlatformQualificationError("target-mismatch")
    architecture = platform.machine().lower()
    try:
        host = validate_host(target_id, architecture, cwd=Path(work_root or kit))
    except PlatformQualificationError:
        record_untrusted("host-mismatch")
        raise
    try:
        expected_subdir = conda_subdir(target_id, architecture)
    except ToolchainError as exc:
        record_untrusted("host-mismatch")
        raise PlatformQualificationError("host-mismatch") from exc
    target = payload["target"]
    files = cast(dict[str, bytes], payload["files"])
    resolution_name = target.resolution_filename(architecture)
    resolution_bytes = files.get(resolution_name)
    constraints_name = target.constraints_filename(architecture)
    constraints_bytes = files.get(constraints_name)
    if resolution_bytes is None or constraints_bytes is None:
        record_untrusted("precondition-failed")
        raise PlatformQualificationError("precondition-failed")
    try:
        resolution = json.loads(resolution_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        record_untrusted("precondition-failed")
        raise PlatformQualificationError("precondition-failed") from exc
    if (
        not isinstance(resolution, dict)
        or resolution.get("schema") != 4
        or resolution.get("target_id") != target_id
    ):
        record_untrusted("precondition-failed")
        raise PlatformQualificationError("precondition-failed")
    phase_one = resolution.get("phase_one")
    phase_replay = resolution.get("phase_replay")
    if (
        not isinstance(phase_one, dict)
        or not isinstance(phase_replay, dict)
        or not isinstance(phase_one.get("conda_packages"), list)
        or not isinstance(phase_replay.get("conda_packages"), list)
    ):
        record_untrusted("precondition-failed")
        raise PlatformQualificationError("precondition-failed")
    build_id = cast(str, manifest["build_id"])
    result_path = output / qualification_filename(target_id, architecture, build_id)
    if output.exists():
        raise PlatformQualificationError("result-save-failed")
    if work_root is None:
        root = Path(tempfile.mkdtemp(prefix="gwexpy-platform-qualification-"))
    else:
        root = Path(work_root)
        if root.exists() or root.is_symlink():
            raise PlatformQualificationError("work root must be fresh")
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir()
    owned_root = True
    runtime_prefix = root / "runtime"
    replay_prefix = root / "replay"
    checks: dict[str, bool | None] = {
        name: None for name in required_schema2_automated_checks(target_id)
    }
    owners: dict[str, bool | None] = {
        name: None for name in required_schema2_owner_confirmations(target_id)
    }
    checks["archive_checksum"] = True
    checks["source_manifest"] = True
    checks["kit_manifest"] = True
    checks["wheel_identity"] = True
    cleanup: bool | None = None
    stage = "preconditions"
    error_code: str | None = None
    failure_stage: str | None = None
    host_trusted = False
    conda = Path(conda_executable or os.environ.get("CONDA_EXE", "conda"))
    try:
        candidate, wheel, constraints = _extract_candidate(
            files,
            root,
            wheel_name=cast(str, manifest["wheel"]["filename"]),
            constraints_name=constraints_name,
            resolution_name=resolution_name,
        )
        stage = "environment"
        _conda_info(conda, cwd=root, target_id=target_id, architecture=architecture)
        runtime_python = _fresh_conda_phase(
            runtime_prefix,
            conda_executable=conda,
            target_id=target_id,
            architecture=architecture,
            package_records=cast(
                Sequence[Mapping[str, object]], phase_one["conda_packages"]
            ),
        ).python
        replay_python = _fresh_conda_phase(
            replay_prefix,
            conda_executable=conda,
            target_id=target_id,
            architecture=architecture,
            package_records=cast(
                Sequence[Mapping[str, object]], phase_replay["conda_packages"]
            ),
        ).python
        report = root / "runtime-pip-report.json"
        _run(
            pip_install_command(
                python=str(runtime_python),
                wheel=wheel,
                report_path=report,
                constraints_path=constraints,
            ),
            cwd=root,
            timeout=1200,
        )
        _run_pip_check(runtime_python, root)
        _verify_installed_phase(
            python=runtime_python,
            conda=conda,
            prefix=runtime_prefix,
            cwd=root,
            checkout=Path(kit),
            phase=cast(Mapping[str, object], phase_one),
            expected_subdir=expected_subdir,
            require_studio=True,
        )
        checks["conda_runtime"] = True
        replay_report = root / "replay-pip-report.json"
        package_names = [
            str(item["name"])
            for item in cast(
                Sequence[Mapping[str, object]], resolution.get("runtime_artifacts", [])
            )
        ]
        _run(
            pip_runtime_install_command(
                python=replay_python,
                package_names=package_names,
                constraints=constraints,
                report_path=replay_report,
            ),
            cwd=root,
            timeout=1200,
        )
        _run_pip_check(replay_python, root)
        _verify_installed_phase(
            python=replay_python,
            conda=conda,
            prefix=replay_prefix,
            cwd=root,
            checkout=Path(kit),
            phase=cast(Mapping[str, object], phase_replay),
            expected_subdir=expected_subdir,
            require_studio=False,
        )
        checks["replay_runtime"] = True
        checks["import_isolation"] = True
        host["python_version"] = _runtime_python_version(runtime_python, cwd=root)
        host_trusted = True
        stage = "automatic"
        gate_script = cast(dict[str, bytes], payload["scripts"])[
            "run_trial_technical_gate.py"
        ]
        gui_root = root / "GUI 試験"
        gui_root.mkdir()
        with _sealed_gate_script(gate_script, root) as gate_fd:
            gate_result = run_installed_technical_gate(
                python=runtime_python,
                gate_fd=gate_fd,
                checkout_root=Path(kit),
                work_root=gui_root,
                native_qt=True,
                replay_python=replay_python,
                legacy=False,
            )
        installed = gate_result.get("installed")
        if (
            gate_result.get("architecture") != architecture
            or not str(gate_result.get("python_version", "")).startswith("3.12.")
            or not isinstance(installed, Mapping)
            or installed.get("build_id") != build_id
            or installed.get("source_sha") != manifest.get("source_sha")
        ):
            raise PlatformQualificationError("technical gate identity mismatch")
        checks["technical_gate"] = True
        backend = _probe_qt_backend(runtime_python, target_id, root)
        host["qt_backend"] = backend
        checks["qt_backend"] = True
        probe = _physical_qt_probe(
            runtime_python, target_id=target_id, root=root, build_id=build_id
        )
        for name, value in probe.items():
            if name == "platform":
                target.require_backend(cast(str, value))
                continue
            if name == "opengl":
                checks["qt_opengl"] = value is True
            elif name == "screen":
                checks["qt_screen"] = value is True
            elif name in checks:
                checks[name] = value is True
        if target_id in {"ubuntu24-x86_64", "debian13-x86_64"}:
            checks["linux_unicode_space_path"] = _unicode_space_path_roundtrip(root)
        elif target_id == "wsl2-ubuntu24":
            checks["guest_architecture"] = host["guest_architecture"] == architecture
            checks["windows_architecture"] = (
                host["windows_architecture"] == architecture
            )
            checks["wsl2_kernel"] = "microsoft" in host["kernel_release"].lower()
            checks["wslg_display"] = (
                checks["qt_screen"] is True and _wslg_display_available()
            )
            checks["windows_unicode_space_path"] = _windows_path_roundtrip(root)
        else:
            checks["native_arm64"] = host["architecture"] == "arm64"
            checks["macos_version"] = int(host["macos_version"].split(".", 1)[0]) >= 15
            checks["cocoa_platform"] = probe.get("platform") == "cocoa"
            checks["macos_unicode_space_path"] = _unicode_space_path_roundtrip(root)
        if not all(value is True for value in checks.values()):
            raise PlatformQualificationError("automatic-failed")
        stage = "owner"
        reader = input if owner_reader is None else owner_reader
        _owner_session(
            runtime_python,
            target_id=target_id,
            root=root,
            build_id=build_id,
            answers=owners,
            reader=reader,
        )
    except KeyboardInterrupt:
        error_code = "interrupted"
        failure_stage = stage
    except PlatformQualificationError as exc:
        code = str(exc)
        if code in _ALLOWED_ERRORS:
            error_code = code
        elif stage == "environment":
            error_code = "conda-unavailable"
        elif stage == "automatic":
            error_code = "automatic-failed"
        else:
            error_code = "runner-failed"
        failure_stage = stage
    except (
        OSError,
        subprocess.SubprocessError,
        ToolchainError,
        QualificationError,
    ):
        error_code = (
            "automatic-failed" if stage == "automatic" else "environment-failed"
        )
        failure_stage = stage
    except Exception:
        error_code = (
            "automatic-failed" if stage == "automatic" else "environment-failed"
        )
        failure_stage = stage
    finally:
        stage = "cleanup"
        cleanup_failed = False
        for prefix in (runtime_prefix, replay_prefix):
            try:
                _cleanup_prefix(prefix)
            except (OSError, PlatformQualificationError):
                cleanup_failed = True
        cleanup = not cleanup_failed
        if cleanup_failed:
            error_code = "cleanup-failed"
            failure_stage = "cleanup"
        if owned_root:
            shutil.rmtree(root, ignore_errors=True)
        if owned_root and (root.exists() or root.is_symlink()):
            cleanup = False
            error_code = "cleanup-failed"
            failure_stage = "cleanup"
    if error_code is None and not all(value is True for value in owners.values()):
        error_code = (
            "owner-false"
            if any(value is False for value in owners.values())
            else "owner-unanswered"
        )
        failure_stage = "owner"
    final_stage = (
        "complete" if error_code is None else (failure_stage or "preconditions")
    )
    if not host_trusted:
        raw = qualification2_untrusted_failure_bytes(
            stage=final_stage,
            error_code=error_code or "environment-failed",
            cleanup=cleanup,
        )
    else:
        raw = qualification2_result_bytes(
            target_id=target_id,
            architecture=architecture,
            build_id=build_id,
            source_sha=cast(str, manifest["source_sha"]),
            wheel_sha256=cast(str, manifest["wheel"]["sha256"]),
            zip_sha256=cast(str, manifest["archive"]["sha256"]),
            kit_manifest_sha256=_sha256(cast(bytes, payload["manifest_bytes"])),
            host=host,
            automated_checks=checks,
            owner_confirmations=owners,
            cleanup=cleanup,
            stage=final_stage,
            error_code=error_code,
        )
    try:
        _write_result_once(result_path, raw)
    except PlatformQualificationError:
        raise
    return result_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-target", required=True)
    parser.add_argument("--kit", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--conda-executable", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one qualification from command-line arguments."""
    arguments = _parser().parse_args(argv)
    try:
        result = run_qualification(
            target_id=arguments.trial_target,
            kit=arguments.kit,
            archive=arguments.archive,
            sidecar=arguments.sidecar,
            output_directory=arguments.output,
            work_root=arguments.work_root,
            conda_executable=arguments.conda_executable,
        )
        document = read_qualification2_result(result.read_bytes())
    except (OSError, PlatformQualificationError, QualificationError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        return 1
    print(result.name)
    return 0 if document.get("status") == "passed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
