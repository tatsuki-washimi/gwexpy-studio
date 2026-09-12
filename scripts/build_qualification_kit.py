"""Build a source-bound, standalone platform qualification kit.

The kit builder captures the verified release pair before it creates any
output. Everything copied into a kit is then read from those captured bytes,
so a replacement of the release directory cannot change the candidate after
verification.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

try:
    from .package_trial_release import (
        TrialReleaseError,
        _read_stable_file,
        _verify_release_bytes,
    )
    from .release_source_manifest import ManifestFormatError, read_manifest
    from .trial_targets import TrialTargetError, trial_target
except ImportError:  # pragma: no cover - direct kit execution.
    from package_trial_release import (  # type: ignore[no-redef]
        TrialReleaseError,
        _read_stable_file,
        _verify_release_bytes,
    )
    from release_source_manifest import (  # type: ignore[no-redef]
        ManifestFormatError,
        read_manifest,
    )
    from trial_targets import TrialTargetError, trial_target  # type: ignore[no-redef]


class QualificationKitError(RuntimeError):
    """Raised when a kit cannot preserve the candidate identity."""


_KIT_MANIFEST = "QUALIFICATION-KIT.json"
_SOURCE_MANIFEST = "SOURCE-MANIFEST.json"


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_descriptor(descriptor: int, label: str) -> bytes:
    try:
        return _read_stable_file(descriptor, label)
    except (OSError, TrialReleaseError) as exc:
        raise QualificationKitError(f"{label} cannot be captured") from exc


def _capture_release_directory(
    directory: Path,
) -> tuple[dict[str, object], dict[str, bytes]]:
    """Capture and verify exactly one archive pair through one directory FD."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError as exc:
        raise QualificationKitError("release directory cannot be inspected") from exc
    try:
        initial = os.fstat(directory_fd)
        if not stat.S_ISDIR(initial.st_mode):
            raise QualificationKitError("release path is not a directory")
        names = os.listdir(directory_fd)
        if len(names) != 2:
            raise QualificationKitError(
                "release directory must contain one ZIP and sidecar"
            )
        archives = [name for name in names if name.endswith(".zip")]
        if len(archives) != 1:
            raise QualificationKitError(
                "release directory must contain one ZIP archive"
            )
        archive_name = archives[0]
        sidecar_name = f"{archive_name}.sha256"
        if sidecar_name not in names:
            raise QualificationKitError("release sidecar is missing")
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        archive_fd = os.open(archive_name, file_flags, dir_fd=directory_fd)
        try:
            sidecar_fd = os.open(sidecar_name, file_flags, dir_fd=directory_fd)
            try:
                archive_bytes = _read_descriptor(archive_fd, "release archive")
                sidecar_bytes = _read_descriptor(sidecar_fd, "release sidecar")
            finally:
                os.close(sidecar_fd)
        finally:
            os.close(archive_fd)
        manifest = _verify_release_bytes(archive_name, archive_bytes, sidecar_bytes)
        final = os.fstat(directory_fd)
        if (
            os.listdir(directory_fd) != names
            or (initial.st_dev, initial.st_ino) != (final.st_dev, final.st_ino)
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
        ):
            raise QualificationKitError("release directory changed while being read")
        return manifest, {archive_name: archive_bytes, sidecar_name: sidecar_bytes}
    except (OSError, TrialReleaseError) as exc:
        if isinstance(exc, QualificationKitError):
            raise
        raise QualificationKitError("release pair cannot be captured safely") from exc
    finally:
        os.close(directory_fd)


def _archive_files(archive_name: str, archive_bytes: bytes) -> dict[str, bytes]:
    """Extract validated ZIP members from already captured archive bytes."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = {}
            root = archive_name.removesuffix(".zip")
            for info in archive.infolist():
                name = info.filename
                prefix = f"{root}/"
                if not name.startswith(prefix) or name[len(prefix) :] == "":
                    raise QualificationKitError("candidate archive member is invalid")
                members[name[len(prefix) :]] = archive.read(info)
            return members
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise QualificationKitError("candidate archive cannot be read") from exc


def _source_entries(source_bytes: bytes) -> dict[str, object]:
    temporary = Path(tempfile.mkdtemp(prefix="gwexpy-source-manifest-"))
    path = temporary / _SOURCE_MANIFEST
    try:
        path.write_bytes(source_bytes)
        manifest = read_manifest(path)
    except (OSError, ManifestFormatError, ValueError) as exc:
        raise QualificationKitError("source manifest is invalid") from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return {entry.path: entry for entry in manifest.entries}


def _runner_script(target_id: str, archive_name: str, sidecar_name: str) -> str:
    """Render the deterministic operator launcher from verified source data."""
    target = shlex.quote(target_id)
    archive = shlex.quote(archive_name)
    sidecar = shlex.quote(sidecar_name)
    return f"""#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=\"$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd -P)\"
CONDA_EXE=\"${{CONDA_EXE:-conda}}\"
command -v \"$CONDA_EXE\" >/dev/null 2>&1 || {{
  echo \"conda is required\" >&2
  exit 1
}}
# conda create --prefix python=3.12 is performed by the verified controller
# after checksum, source, kit, and host preconditions have passed.
\"$CONDA_EXE\" run --no-capture-output -n base \\
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
  --kit \"$SCRIPT_DIR\" \\
  --trial-target {target} \\
  --archive \"$SCRIPT_DIR/{archive}\" \\
  --sidecar \"$SCRIPT_DIR/{sidecar}\"
status=$?
exit $status
"""


def _write_file(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.write_bytes(content)
    path.chmod(mode)


def _read_source_file(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise QualificationKitError(f"{label} cannot be read") from exc
    try:
        return _read_descriptor(descriptor, label)
    finally:
        os.close(descriptor)


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically install a directory without replacing a raced destination."""
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        parent_fd = os.open(
            destination.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            function_name = (
                "renameat2" if sys.platform.startswith("linux") else "renameatx_np"
            )
            rename_noreplace = getattr(libc, function_name, None)
            if rename_noreplace is None:
                raise QualificationKitError(
                    "atomic no-replace directory install unavailable"
                )
            rename_noreplace.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename_noreplace.restype = ctypes.c_int
            result = rename_noreplace(
                parent_fd,
                os.fsencode(source.name),
                parent_fd,
                os.fsencode(destination.name),
                1 if sys.platform.startswith("linux") else 4,
            )
            if result != 0:
                error = ctypes.get_errno()
                if error == errno.EEXIST:
                    raise QualificationKitError(
                        "qualification kit output appeared during build"
                    )
                raise OSError(error, os.strerror(error))
            return
        finally:
            os.close(parent_fd)
    if destination.exists() or destination.is_symlink():
        raise QualificationKitError("qualification kit output appeared during build")
    os.rename(source, destination)


def build_qualification_kit(
    *,
    release_directory: Path,
    output_directory: Path,
    target_id: str,
    scripts_directory: Path | None = None,
) -> Path:
    """Bind one verified release snapshot to a fresh, atomic operator kit."""
    try:
        target = trial_target(target_id)
    except TrialTargetError as exc:
        raise QualificationKitError(str(exc)) from exc
    manifest, captured = _capture_release_directory(Path(release_directory))
    target_record = manifest.get("target")
    if not isinstance(target_record, dict) or target_record.get("id") != target.id:
        raise QualificationKitError("candidate release target does not match kit")
    build = manifest.get("build")
    wheel = manifest.get("wheel")
    source_record = manifest.get("source")
    if (
        not isinstance(build, dict)
        or not isinstance(wheel, dict)
        or not isinstance(source_record, dict)
    ):
        raise QualificationKitError("candidate release identity is incomplete")
    build_id = build.get("id")
    source_sha = build.get("source_sha")
    wheel_name = wheel.get("filename")
    wheel_sha = wheel.get("sha256")
    if not all(
        isinstance(value, str)
        for value in (build_id, source_sha, wheel_name, wheel_sha)
    ):
        raise QualificationKitError("candidate release identity is invalid")
    archive_name = next(name for name in captured if name.endswith(".zip"))
    sidecar_name = f"{archive_name}.sha256"
    archive_bytes = captured[archive_name]
    sidecar_bytes = captured[sidecar_name]
    files = _archive_files(archive_name, archive_bytes)
    source_bytes = files.get(_SOURCE_MANIFEST)
    if source_bytes is None:
        raise QualificationKitError("candidate source manifest is missing")
    source_entries = _source_entries(source_bytes)
    source_manifest_sha = _sha256(source_bytes)
    if source_record.get("manifest_sha256") != source_manifest_sha:
        raise QualificationKitError("candidate source manifest hash is invalid")
    if files.get(wheel_name) is None or _sha256(files[wheel_name]) != wheel_sha:
        raise QualificationKitError("candidate wheel bytes are invalid")

    source_root = (
        Path(__file__).resolve().parent
        if scripts_directory is None
        else Path(scripts_directory).resolve(strict=True)
    )
    builder_source = source_root / "build_qualification_kit.py"
    builder_entry = source_entries.get("scripts/build_qualification_kit.py")
    if builder_entry is not None and (
        builder_source.is_symlink() or not builder_source.is_file()
    ):
        raise QualificationKitError("qualification builder source is missing")
    if builder_entry is not None and builder_entry.sha256 != _sha256(
        _read_source_file(builder_source, "qualification builder source")
    ):
        raise QualificationKitError("qualification builder source is not source-bound")
    selected: dict[str, bytes] = {}
    manifest_script_names = sorted(
        path.removeprefix("scripts/")
        for path in source_entries
        if path.startswith("scripts/") and path.endswith(".py")
    )
    if not manifest_script_names:
        raise QualificationKitError(
            "candidate source manifest has no qualification helper scripts"
        )
    script_names = tuple(manifest_script_names)
    for name in script_names:
        source = source_root / name
        if source.is_symlink() or not source.is_file():
            raise QualificationKitError(f"qualification script is missing: {name}")
        content = _read_source_file(source, f"qualification script {name}")
        bound = source_entries.get(f"scripts/{name}")
        if bound is not None and bound.sha256 != _sha256(content):
            raise QualificationKitError(
                f"qualification script is not source-bound: {name}"
            )
        if bound is None:
            raise QualificationKitError(
                f"qualification script is absent from source manifest: {name}"
            )
        selected[name] = content

    runner_bytes = _runner_script(target.id, archive_name, sidecar_name).encode("utf-8")
    kit_manifest = {
        "archive": {"filename": archive_name, "sha256": _sha256(archive_bytes)},
        "build_id": build_id,
        "legacy_fixture": False,
        "runner": {"filename": "run-qualification.sh", "sha256": _sha256(runner_bytes)},
        "schema": 2,
        "scripts": [
            {
                "filename": name,
                "source_path": f"scripts/{name}",
                "sha256": _sha256(content),
            }
            for name, content in sorted(selected.items())
        ],
        "sidecar": {"filename": sidecar_name, "sha256": _sha256(sidecar_bytes)},
        "source_manifest_sha256": source_manifest_sha,
        "source_sha": source_sha,
        "target_id": target.id,
        "wheel": {"filename": wheel_name, "sha256": wheel_sha},
    }
    manifest_bytes = _canonical_json(kit_manifest)

    output = Path(output_directory).absolute()
    if output.exists() or output.is_symlink():
        raise QualificationKitError("qualification kit output must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        _write_file(stage / archive_name, archive_bytes)
        _write_file(stage / sidecar_name, sidecar_bytes)
        _write_file(stage / _SOURCE_MANIFEST, source_bytes)
        for name, content in selected.items():
            _write_file(stage / name, content)
        _write_file(stage / "run-qualification.sh", runner_bytes, 0o755)
        _write_file(stage / _KIT_MANIFEST, manifest_bytes)
        _rename_directory_noreplace(stage, output)
    except (OSError, QualificationKitError) as exc:
        if isinstance(exc, QualificationKitError):
            raise
        raise QualificationKitError("qualification kit could not be written") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--trial-target", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build one kit from command-line arguments."""
    arguments = _parser().parse_args(argv)
    try:
        output = build_qualification_kit(
            release_directory=arguments.release,
            output_directory=arguments.output,
            target_id=arguments.trial_target,
        )
    except (OSError, QualificationKitError) as exc:
        print(f"build_qualification_kit: error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
