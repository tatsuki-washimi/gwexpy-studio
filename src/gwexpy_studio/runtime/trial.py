"""Pure-Python persistence and sample support for the Studio trial."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from importlib import resources
from pathlib import Path

from .._version import __version__
from ..user_paths import config_directory, data_directory

_MAX_RECENT_PROJECTS = 10
_RECENT_PROJECTS_FILENAME = "recent-projects.json"
_SAMPLE_RESOURCE = ("assets", "alpha-timeseries.csv")


def _sync_directory(directory: Path) -> None:
    """Flush a directory entry after a replacement or newly linked file."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomically(path: Path, content: bytes) -> None:
    """Replace one Studio-owned file after safely writing its temporary content."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".recent-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_if_absent(path: Path, content: bytes) -> bool:
    """Link fully written sample bytes into place without replacing user content."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sample-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _sync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_path(value: str | os.PathLike[str]) -> Path:
    """Normalize one project path without requiring that the project exists."""
    return Path(value).expanduser().resolve(strict=False)


class RecentProjectStore:
    """Keep a bounded canonical-path-only recent-project list in XDG config."""

    def __init__(self) -> None:
        """Point at the Studio-owned JSON document without creating it."""
        self.path = config_directory() / _RECENT_PROJECTS_FILENAME

    def _load(self) -> tuple[Path, ...]:
        """Read valid path-only records, treating corrupt user data as empty."""
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            return ()
        if not isinstance(document, list):
            return ()
        records: list[Path] = []
        for value in document:
            if type(value) is not str:
                continue
            try:
                path = _canonical_path(value)
            except (OSError, RuntimeError, ValueError):
                continue
            if path not in records:
                records.append(path)
            if len(records) == _MAX_RECENT_PROJECTS:
                break
        return tuple(records)

    def _write(self, records: tuple[Path, ...]) -> None:
        """Persist canonical paths with an atomic replacement of this owned file."""
        content = json.dumps(
            [os.fspath(path) for path in records],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_atomically(self.path, content)

    def record(self, path: str | os.PathLike[str]) -> None:
        """Place one canonical project path first, without inspecting its contents."""
        canonical = _canonical_path(path)
        records = (canonical,) + tuple(
            item for item in self._load() if item != canonical
        )
        self._write(records[:_MAX_RECENT_PROJECTS])

    def list(self) -> tuple[Path, ...]:
        """Return recent canonical paths in newest-first order, including missing ones.

        Missing files are intentionally retained for the UI to render safely.
        """
        return self._load()

    def clear(self) -> None:
        """Remove the owned recent-project file when it is present."""
        try:
            self.path.unlink()
        except (FileNotFoundError, IsADirectoryError):
            return
        _sync_directory(self.path.parent)


def _sample_bytes() -> tuple[str, bytes]:
    """Return the packaged sample name and its immutable resource content."""
    resource = resources.files("gwexpy_studio").joinpath(*_SAMPLE_RESOURCE)
    return resource.name, resource.read_bytes()


def _path_exists(path: Path) -> bool:
    """Treat dangling symlinks as existing user-controlled paths to preserve."""
    return os.path.lexists(path)


def _content_matches(path: Path, digest: bytes) -> bool:
    """Whether a readable target has the expected sample content digest."""
    try:
        return hashlib.sha256(path.read_bytes()).digest() == digest
    except OSError:
        return False


def _alternative_path(directory: Path, name: str, digest: str, attempt: int) -> Path:
    """Build a hash-suffixed target name that cannot overwrite a user sample."""
    source = Path(name)
    suffix = f"-{digest}" if attempt == 1 else f"-{digest}-{attempt}"
    return directory / f"{source.stem}{suffix}{source.suffix}"


def sample_path() -> Path:
    """Copy or safely reuse the packaged alpha sample under Studio's data root."""
    name, content = _sample_bytes()
    digest = hashlib.sha256(content).digest()
    digest_hex = digest.hex()
    directory = data_directory() / "samples" / __version__ / digest_hex
    normal = directory / name
    while True:
        if _content_matches(normal, digest):
            return normal
        if not _path_exists(normal):
            if _copy_if_absent(normal, content):
                return normal
            continue
        break
    attempt = 1
    while True:
        alternative = _alternative_path(directory, name, digest_hex, attempt)
        if _content_matches(alternative, digest):
            return alternative
        if not _path_exists(alternative) and _copy_if_absent(alternative, content):
            return alternative
        attempt += 1


def copy_alpha_timeseries_sample() -> Path:
    """Return the safely materialized alpha time-series sample path."""
    return sample_path()
