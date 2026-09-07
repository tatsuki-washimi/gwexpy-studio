"""Fail closed before extracting the selected GitHub trial-artifact ZIP."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

sys.dont_write_bytecode = True


class TrialArtifactError(RuntimeError):
    """Raised when an artifact archive cannot be bound and safely unpacked."""


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_MEMBERS = 64
_MAX_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024


def extract_verified_trial_artifact(
    *,
    archive: Path,
    output_directory: Path,
    expected_digest: str,
) -> str:
    """Verify the exact ZIP bytes, then write only safe top-level regular files."""
    expected = _validate_expected_digest(expected_digest)
    archive_file, archive_status = _open_regular_archive(Path(archive))
    try:
        digest = _sha256_file(archive_file)
        if digest != expected:
            raise TrialArtifactError(
                "artifact digest does not match the selected build"
            )
        archive_file.seek(0)
        members: tuple[zipfile.ZipInfo, ...] = ()
        extracted = False
        try:
            with zipfile.ZipFile(archive_file) as contents:
                members = _validate_members(contents.infolist())
                _extract_members(contents, members, Path(output_directory))
                extracted = True
            if not _same_file_status(archive_status, os.fstat(archive_file.fileno())):
                raise TrialArtifactError("artifact ZIP changed during verification")
        except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
            if extracted:
                _remove_new_output_directory(
                    Path(output_directory), tuple(member.filename for member in members)
                )
            raise TrialArtifactError("artifact ZIP cannot be safely extracted") from exc
        except TrialArtifactError:
            if extracted:
                _remove_new_output_directory(
                    Path(output_directory), tuple(member.filename for member in members)
                )
            raise
    finally:
        archive_file.close()
    return digest


def _validate_expected_digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise TrialArtifactError("expected artifact digest is invalid")
    return value.removeprefix("sha256:")


def _open_regular_archive(path: Path) -> tuple[BinaryIO, os.stat_result]:
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise TrialArtifactError("artifact ZIP cannot be inspected") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise TrialArtifactError("artifact ZIP must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TrialArtifactError("artifact ZIP cannot be opened safely") from exc
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISREG(opened_status.st_mode) or not _same_file_status(
            status, opened_status
        ):
            raise TrialArtifactError("artifact ZIP changed while opening")
        return os.fdopen(descriptor, "rb"), opened_status
    except Exception:
        os.close(descriptor)
        raise


def _same_file_status(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
    ) == (
        actual.st_dev,
        actual.st_ino,
        actual.st_size,
        actual.st_mtime_ns,
        actual.st_ctime_ns,
    )


def _sha256_file(file: BinaryIO) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = file.read(_CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _validate_members(members: list[zipfile.ZipInfo]) -> tuple[zipfile.ZipInfo, ...]:
    if not members or len(members) > _MAX_MEMBERS:
        raise TrialArtifactError("artifact ZIP member count is invalid")
    names: set[str] = set()
    total_size = 0
    for member in members:
        _validate_member(member, names)
        if member.file_size < 0 or member.file_size > _MAX_MEMBER_BYTES:
            raise TrialArtifactError("artifact ZIP member size is invalid")
        total_size += member.file_size
        if total_size > _MAX_TOTAL_BYTES:
            raise TrialArtifactError("artifact ZIP total size is invalid")
    return tuple(members)


def _validate_member(member: zipfile.ZipInfo, names: set[str]) -> None:
    name = member.filename
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise TrialArtifactError("artifact ZIP member path is unsafe")
    if name in names:
        raise TrialArtifactError("artifact ZIP has a duplicate member name")
    names.add(name)
    mode = member.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if (
        member.is_dir()
        or member.flag_bits & 0x1
        or member.external_attr & 0x10
        or file_type not in {0, stat.S_IFREG}
    ):
        raise TrialArtifactError(
            "artifact ZIP member must be an unencrypted regular file"
        )


def _extract_members(
    contents: zipfile.ZipFile,
    members: Sequence[zipfile.ZipInfo],
    output_directory: Path,
) -> None:
    output = Path(os.path.abspath(output_directory))
    _create_output_directory(output)
    try:
        with _open_directory(output) as output_descriptor:
            for member in members:
                _extract_member(contents, member, output_descriptor)
    except Exception:
        _remove_new_output_directory(
            output, tuple(member.filename for member in members)
        )
        raise


def _create_output_directory(output: Path) -> None:
    try:
        parent_status = os.lstat(output.parent)
    except OSError as exc:
        raise TrialArtifactError("artifact output parent cannot be inspected") from exc
    if stat.S_ISLNK(parent_status.st_mode) or not stat.S_ISDIR(parent_status.st_mode):
        raise TrialArtifactError("artifact output parent is unsafe")
    try:
        os.mkdir(output, 0o700)
    except FileExistsError as exc:
        raise TrialArtifactError("artifact output directory already exists") from exc
    except OSError as exc:
        raise TrialArtifactError("artifact output directory cannot be created") from exc


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TrialArtifactError("artifact output directory is unsafe") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise TrialArtifactError("artifact output directory is unsafe")
        yield descriptor
    finally:
        os.close(descriptor)


def _extract_member(
    contents: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    output_descriptor: int,
) -> None:
    descriptor = os.open(
        member.filename,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=output_descriptor,
    )
    written = 0
    try:
        with contents.open(member, "r") as source:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                _write_all(descriptor, chunk)
        if written != member.file_size:
            raise TrialArtifactError(
                "artifact ZIP member size changed during extraction"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("artifact output write failed")
        remaining = remaining[written:]


def _remove_new_output_directory(output: Path, names: Sequence[str]) -> None:
    try:
        with _open_directory(output) as descriptor:
            for name in names:
                try:
                    os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
        os.rmdir(output)
    except OSError:
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-digest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Extract one explicitly named and digest-bound trial artifact ZIP."""
    arguments = _parser().parse_args(argv)
    try:
        digest = extract_verified_trial_artifact(
            archive=arguments.archive,
            output_directory=arguments.output,
            expected_digest=arguments.expected_digest,
        )
    except TrialArtifactError as exc:
        print(f"extract_trial_artifact: error: {exc}", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":  # pragma: no cover - direct CLI invocation.
    raise SystemExit(main())
