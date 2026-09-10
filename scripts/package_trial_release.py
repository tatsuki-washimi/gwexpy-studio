"""Create and verify the deterministic outer trial release archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import stat
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path

try:
    from . import verify_trial_bundle as _bundle_verifier
    from .assemble_trial_bundle import TrialBundleError, verify_trial_bundle_bytes
    from .trial_targets import TrialTargetError, trial_target
except ImportError:  # pragma: no cover
    import verify_trial_bundle as _bundle_verifier  # type: ignore
    from assemble_trial_bundle import (  # type: ignore
        TrialBundleError,
        verify_trial_bundle_bytes,
    )
    from trial_targets import TrialTargetError, trial_target  # type: ignore[no-redef]


class TrialReleaseError(RuntimeError):
    """Raised when an outer release archive is not safe and complete."""


def package_trial_release(
    bundle_directory: Path, output_directory: Path
) -> tuple[Path, Path]:
    """Verify a flat bundle and write its deterministic archive and sidecar."""
    try:
        files = _bundle_verifier.read_trial_bundle_bytes(Path(bundle_directory))
        manifest = verify_trial_bundle_bytes(files)
    except TrialBundleError as exc:
        raise TrialReleaseError("internal bundle verification failed") from exc
    build = manifest.get("build")
    if not isinstance(build, dict) or not isinstance(build.get("id"), str):
        raise TrialReleaseError("verified bundle has no build ID")
    root = _archive_root(manifest)
    output = Path(output_directory)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise TrialReleaseError("output directory must be fresh and empty")
    else:
        output.mkdir(parents=True)
    archive_path = output / f"{root}.zip"
    sidecar_path = output / f"{archive_path.name}.sha256"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files, key=lambda item: item.encode("utf-8")):
            info = zipfile.ZipInfo(f"{root}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            info.extra = b""
            info.comment = b""
            archive.writestr(info, files[name])
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sidecar_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")
    return archive_path, sidecar_path


def verify_trial_release(archive_path: Path, sidecar_path: Path) -> dict[str, object]:
    """Verify sidecar, outer archive safety, and delegated internal bytes."""
    manifest, _archive_bytes = read_verified_trial_release(archive_path, sidecar_path)
    return manifest


def read_verified_trial_release(
    archive_path: Path, sidecar_path: Path
) -> tuple[dict[str, object], bytes]:
    """Return a verified archive byte snapshot and its derived manifest."""
    archive = Path(archive_path)
    sidecar = Path(sidecar_path)
    try:
        archive_bytes = _read_stable_path(archive, "archive")
        sidecar_bytes = _read_stable_path(sidecar, "sidecar")
    except OSError as exc:
        raise TrialReleaseError("archive or sidecar is missing") from exc
    manifest = _verify_release_bytes(archive.name, archive_bytes, sidecar_bytes)
    return manifest, archive_bytes


def _verify_release_bytes(
    archive_name: str, archive_bytes: bytes, sidecar_bytes: bytes
) -> dict[str, object]:
    """Verify one already-captured archive and sidecar byte snapshot."""
    expected_line = (
        f"{hashlib.sha256(archive_bytes).hexdigest()}  {archive_name}\n".encode("ascii")
    )
    if sidecar_bytes != expected_line:
        raise TrialReleaseError("sidecar digest or filename is incorrect")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as contents:
            infos = contents.infolist()
            if not infos:
                raise TrialReleaseError("archive is empty")
            if contents.comment:
                raise TrialReleaseError("archive is not canonical")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise TrialReleaseError("archive contains duplicate entries")
            if names != sorted(names, key=lambda name: name.encode("utf-8")):
                raise TrialReleaseError("archive is not canonical")
            root = names[0].split("/", 1)[0]
            if not root or not root.startswith("gwexpy-studio-trial-"):
                raise TrialReleaseError("archive root is invalid")
            files: dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                if not name or "\\" in name or name.startswith("/"):
                    raise TrialReleaseError("archive member path is unsafe")
                if info.date_time != (1980, 1, 1, 0, 0, 0):
                    raise TrialReleaseError("archive is not canonical")
                if info.compress_type != zipfile.ZIP_STORED:
                    raise TrialReleaseError("archive is not canonical")
                parts = name.split("/")
                if ".." in parts or len(parts) != 2 or parts[0] != root or not parts[1]:
                    raise TrialReleaseError("archive member path is unsafe")
                mode = (info.external_attr >> 16) & 0o777777
                if stat.S_IFMT(mode) != stat.S_IFREG or mode != (stat.S_IFREG | 0o644):
                    raise TrialReleaseError("archive member is not a regular 0644 file")
                if info.extra or info.comment or info.is_dir():
                    raise TrialReleaseError("archive member metadata is invalid")
                files[parts[1]] = contents.read(info)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TrialReleaseError("archive cannot be read") from exc
    try:
        manifest = verify_trial_bundle_bytes(files)
    except TrialBundleError as exc:
        raise TrialReleaseError("internal bundle files are invalid") from exc
    build = manifest.get("build")
    if not isinstance(build, dict) or not isinstance(build.get("id"), str):
        raise TrialReleaseError("verified bundle has no build ID")
    expected_root = _archive_root(manifest)
    if root != expected_root or archive_name != f"{expected_root}.zip":
        raise TrialReleaseError("archive root or filename does not match build ID")
    return manifest


def _archive_root(manifest: dict[str, object]) -> str:
    """Return the exact schema-bound top-level directory and asset basename."""
    build = manifest.get("build")
    if not isinstance(build, dict) or not isinstance(build.get("id"), str):
        raise TrialReleaseError("verified bundle has no build ID")
    prefix = "gwexpy-studio-trial"
    if manifest.get("schema") == 4:
        target_record = manifest.get("target")
        if not isinstance(target_record, dict) or not isinstance(
            target_record.get("id"), str
        ):
            raise TrialReleaseError("verified bundle has no target ID")
        try:
            target = trial_target(target_record["id"])
        except TrialTargetError as exc:
            raise TrialReleaseError("verified bundle target is invalid") from exc
        prefix = f"{prefix}-{target.id}"
    return f"{prefix}-{build['id']}"


def verify_trial_release_directory(release_directory: Path) -> dict[str, object]:
    """Verify exactly one archive and its matching sidecar in a directory."""
    directory = Path(release_directory)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(directory, flags)
    except OSError as exc:
        raise TrialReleaseError("release directory cannot be inspected") from exc
    try:
        status = os.fstat(directory_fd)
        if not stat.S_ISDIR(status.st_mode):
            raise TrialReleaseError("release path is not a directory")
        names = os.listdir(directory_fd)
        if len(names) != 2:
            raise TrialReleaseError("release directory must contain exactly two files")
        archives = [name for name in names if name.endswith(".zip")]
        if len(archives) != 1:
            raise TrialReleaseError("release directory must contain one ZIP archive")
        archive_name = archives[0]
        sidecar_name = f"{archive_name}.sha256"
        if sidecar_name not in names:
            raise TrialReleaseError("release directory sidecar name is incorrect")
        archive_bytes = _read_stable_at(directory_fd, archive_name, "archive")
        sidecar_bytes = _read_stable_at(directory_fd, sidecar_name, "sidecar")
        final_status = os.fstat(directory_fd)
        if (
            os.listdir(directory_fd) != names
            or final_status.st_dev != status.st_dev
            or final_status.st_ino != status.st_ino
            or final_status.st_mtime_ns != status.st_mtime_ns
            or final_status.st_ctime_ns != status.st_ctime_ns
        ):
            raise TrialReleaseError("release directory changed while being read")
        return _verify_release_bytes(archive_name, archive_bytes, sidecar_bytes)
    except OSError as exc:
        raise TrialReleaseError("release file cannot be read safely") from exc
    finally:
        os.close(directory_fd)


def _read_stable_path(path: Path, label: str) -> bytes:
    """Read one regular file through a no-follow descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        return _read_stable_file(descriptor, label)
    finally:
        os.close(descriptor)


def _read_stable_at(directory_fd: int, name: str, label: str) -> bytes:
    """Read one directory entry through its already-open parent descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        return _read_stable_file(descriptor, label)
    finally:
        os.close(descriptor)


def _read_stable_file(descriptor: int, label: str) -> bytes:
    """Read a regular descriptor and reject identity or metadata changes."""
    initial = os.fstat(descriptor)
    if not stat.S_ISREG(initial.st_mode):
        raise TrialReleaseError(f"{label} is not a regular file")
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
        raise TrialReleaseError(f"{label} changed while being read")
    return bytes(content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the packaging CLI."""
    args = _parser().parse_args(argv)
    try:
        archive, sidecar = package_trial_release(args.bundle, args.output)
    except (OSError, TrialReleaseError) as exc:
        print(f"package_trial_release: error: {exc}", file=sys.stderr)
        return 1
    print(archive)
    print(sidecar)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
