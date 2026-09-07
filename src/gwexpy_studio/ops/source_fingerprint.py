"""Bounded streaming content evidence for every ordered input, without previews."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .intake import (
    _MAX_IO_DEPTH,
    DEFAULT_IO_BYTES,
    DEFAULT_IO_ENTRIES,
    _intake_limit,
    _intake_path,
)

_CHUNK_BYTES = 1024 * 1024


def _metadata(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _unchanged(path: Path, expected: os.stat_result, actual: os.stat_result) -> None:
    if _metadata(expected) != _metadata(actual):
        raise ValueError(f"Source changed while fingerprinting: {path}")


def _encoded(value: Any) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )


@dataclass(frozen=True, slots=True)
class _Entry:
    root: int
    relative: str
    path: Path
    info: os.stat_result
    kind: str


def _inventory(
    paths: list[Path], max_bytes: int, max_entries: int
) -> tuple[list[_Entry], int, int]:
    entries = []
    count = 0
    total_bytes = 0
    for root_index, root in enumerate(paths):
        stack: list[tuple[Path, frozenset[tuple[int, int]], int]] = [
            (root, frozenset(), 0)
        ]
        while stack:
            path, ancestors, depth = stack.pop()
            if depth > _MAX_IO_DEPTH:
                raise ValueError("Source directory nesting exceeds depth limit")
            info = path.stat()
            if stat.S_ISREG(info.st_mode):
                kind = "file"
                total_bytes += info.st_size
                if total_bytes > max_bytes:
                    raise ValueError(
                        f"Source byte count exceeds byte limit {max_bytes}"
                    )
            elif stat.S_ISDIR(info.st_mode):
                kind = "directory"
            else:
                raise ValueError(f"Source is not a regular file or directory: {path}")
            if path != root or kind == "file":
                count += 1
                if count > max_entries:
                    raise ValueError("Source entry count exceeds entry limit")
            entries.append(
                _Entry(root_index, path.relative_to(root).as_posix(), path, info, kind)
            )
            if kind == "directory":
                identity = (info.st_dev, info.st_ino)
                if identity in ancestors:
                    raise ValueError(f"Source directory cycle detected: {path}")
                descendants = ancestors | {identity}
                children: list[Path] = []
                with os.scandir(path) as listing:
                    for child in listing:
                        if count + len(stack) + len(children) >= max_entries:
                            raise ValueError("Source entry count exceeds entry limit")
                        children.append(Path(child.path))
                for child_path in sorted(children, reverse=True):
                    stack.append((child_path, descendants, depth + 1))
    return entries, count, total_bytes


def _file_digest(entry: _Entry) -> str:
    descriptor = os.open(entry.path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        current = os.fstat(descriptor)
        _unchanged(entry.path, entry.info, current)
        if not stat.S_ISREG(current.st_mode):
            raise ValueError(f"Source changed while fingerprinting: {entry.path}")
        digest = hashlib.sha256()
        read_bytes = 0
        while chunk := os.read(descriptor, _CHUNK_BYTES):
            read_bytes += len(chunk)
            if read_bytes > entry.info.st_size:
                raise ValueError(f"Source changed while fingerprinting: {entry.path}")
            digest.update(chunk)
        _unchanged(entry.path, entry.info, os.fstat(descriptor))
        if read_bytes != entry.info.st_size:
            raise ValueError(f"Source changed while fingerprinting: {entry.path}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def fingerprint_sources(request: Mapping[str, Any]) -> dict[str, Any]:
    """Hash complete ordered inputs with original intake bounds and race checks.

    Inventory evidence covers root order and names, all relative file and
    directory names, kinds, and sizes. Content evidence includes each file's
    streaming SHA-256. Metadata is used to detect concurrent edits, but an
    unchanged file with a newer mtime still produces the same evidence.
    """
    raw_paths = request.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError("Select at least one source")
    max_bytes = _intake_limit(request.get("max_bytes", DEFAULT_IO_BYTES), "byte")
    max_entries = _intake_limit(request.get("max_entries", DEFAULT_IO_ENTRIES), "entry")
    if len(raw_paths) > max_entries:
        raise ValueError("Selected source count exceeds entry limit")
    paths = [_intake_path(raw) for raw in raw_paths]
    entries, count, total_bytes = _inventory(paths, max_bytes, max_entries)
    inventory = hashlib.sha256()
    content = hashlib.sha256()
    for index, path in enumerate(paths):
        inventory.update(_encoded(["root", index, str(path)]))
    for entry in entries:
        descriptor = [
            entry.root,
            entry.relative,
            entry.kind,
            entry.info.st_size if entry.kind == "file" else 0,
        ]
        inventory.update(_encoded(descriptor))
        content.update(_encoded(descriptor))
        if entry.kind == "file":
            content.update(_encoded(_file_digest(entry)))
    for entry in entries:
        _unchanged(entry.path, entry.info, entry.path.stat())
    return {
        "schema_version": 1,
        "algorithm": "sha256",
        "content_sha256": content.hexdigest(),
        "inventory_sha256": inventory.hexdigest(),
        "root_count": len(paths),
        "entry_count": count,
        "total_bytes": total_bytes,
        "max_bytes": max_bytes,
        "max_entries": max_entries,
    }
