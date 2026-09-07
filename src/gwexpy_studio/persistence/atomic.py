"""Atomic, durable replacement of data files without changing their old content."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from .locking import FileLock


def sync_directory(directory: Path) -> None:
    """Flush directory entries after a durable rename or removal."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory(directory: Path) -> None:
    """Create parent directories and flush each new entry before saving data."""
    missing = []
    ancestor = directory
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for created in reversed(missing):
        sync_directory(created.parent)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Sync a private temporary file, atomically replace, and sync its directory.

    An existing target remains recoverable even if the final directory sync
    fails. Short temporary names also support targets near the filename limit.
    """
    identity = uuid.uuid4().hex
    temporary = path.parent / f".gwx-{identity}.tmp"
    backup = path.parent / f".gwx-{identity}.old"
    owner = path.parent / f".gwx-{identity}.owner"
    lock = FileLock(owner)
    if not lock.acquire():
        raise FileExistsError("Atomic transaction already belongs to another writer")
    replaced = False
    had_original = False
    try:
        journal = {
            "schema_version": 1,
            "kind": "gwexpy-studio-atomic",
            "target": str(path.resolve()),
        }
        with owner.open("w", encoding="utf-8") as stream:
            json.dump(journal, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(path.parent)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.link(path, backup)
            had_original = True
        os.replace(temporary, path)
        replaced = True
        sync_directory(path.parent)
    except BaseException:
        if replaced:
            if had_original:
                os.replace(backup, path)
            else:
                path.unlink(missing_ok=True)
            try:
                sync_directory(path.parent)
            except OSError:
                pass
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            owner.unlink(missing_ok=True)
        finally:
            lock.close()


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate atomic owner field")
    return result


def cleanup_atomic_writes(target: str | Path) -> None:
    """Remove abandoned temporary pairs for exactly one explicitly recovered path.

    A validated durable owner journal names the target. The transaction flock
    excludes all live writers; only files sharing that journal's UUID are
    removed. The saved project itself is never changed or rolled back here.
    """
    path = Path(target).expanduser().resolve()
    if not path.parent.is_dir():
        return
    changed = False
    for owner in path.parent.glob(".gwx-*.owner"):
        match = re.fullmatch(r"\.gwx-([0-9a-f]{32})\.owner", owner.name)
        if match is None:
            continue
        lock = FileLock(owner)
        try:
            try:
                if not lock.acquire(create=False):
                    continue
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ELOOP, errno.EINVAL):
                    continue
                raise
            descriptor = os.open(owner, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                raw = stream.read(8193)
            if len(raw) > 8192:
                continue
            try:
                journal = json.loads(raw, object_pairs_hook=_unique_fields)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if (
                not isinstance(journal, dict)
                or set(journal) != {"schema_version", "kind", "target"}
                or type(journal["schema_version"]) is not int
                or journal["schema_version"] != 1
                or journal["kind"] != "gwexpy-studio-atomic"
                or journal["target"] != str(path)
            ):
                continue
            for suffix in ("tmp", "old"):
                (path.parent / f".gwx-{match[1]}.{suffix}").unlink(missing_ok=True)
            owner.unlink(missing_ok=True)
            changed = True
        finally:
            lock.close()
    if changed:
        sync_directory(path.parent)
