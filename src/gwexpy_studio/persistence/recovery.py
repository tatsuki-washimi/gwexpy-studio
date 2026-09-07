"""Data-only, bounded, two-generation recovery snapshots for independent runs."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..domain.project import Project
from ..errors import ProjectFormatError
from .atomic import (
    atomic_write_bytes,
    cleanup_atomic_writes,
    ensure_directory,
    sync_directory,
)
from .locking import FileLock, state_directory
from .project_io import (
    MAX_GRAPH_DEPTH,
    PROJECT_SIZE_LIMIT_BYTES,
    _bounded_json_depth,
    _check_finite_recursive,
    _reject_constant,
    _reject_duplicate_keys,
    validate_project_document,
)

_FIELDS = {
    "schema_version",
    "run_id",
    "revision",
    "generation",
    "created_at",
    "original_path",
    "pending",
    "project",
    "sha256",
}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ProjectFormatError("Recovery run ID must be a UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ProjectFormatError("Recovery run ID must be a canonical UUID4")
    return value


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Immutable serialized state whose decoded values never alias a controller."""

    run_id: str
    revision: int
    generation: int
    created_at: str
    original_path: str | None
    _project_json: bytes
    _pending_json: bytes

    @property
    def pending(self) -> dict[str, Any] | None:
        """Return independent interrupted-command metadata, never executable work."""
        return json.loads(self._pending_json)  # type: ignore[no-any-return]

    def project(self) -> Project:
        """Materialize a fresh data-only project without reading any source."""
        return Project.from_dict(json.loads(self._project_json))

    def to_dict(self) -> dict[str, Any]:
        """Return independent candidate metadata for a recovery chooser."""
        return {
            "run_id": self.run_id,
            "revision": self.revision,
            "generation": self.generation,
            "created_at": self.created_at,
            "original_path": self.original_path,
            "pending": self.pending,
        }


def _decode(content: bytes, run_id: str) -> RecoverySnapshot:
    if len(content) > PROJECT_SIZE_LIMIT_BYTES:
        raise ProjectFormatError("Recovery snapshot exceeds size limit")
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProjectFormatError("Malformed recovery JSON") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ProjectFormatError("Invalid recovery envelope fields")
    if _bounded_json_depth(value, limit=MAX_GRAPH_DEPTH) > MAX_GRAPH_DEPTH:
        raise ProjectFormatError("Recovery snapshot exceeds depth limit")
    _check_finite_recursive(value)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ProjectFormatError("Unsupported recovery schema version")
    if _run_id(value["run_id"]) != run_id:
        raise ProjectFormatError("Recovery run identity mismatch")
    for key, minimum in (("revision", 0), ("generation", 1)):
        if type(value[key]) is not int or value[key] < minimum:
            raise ProjectFormatError(f"Recovery {key} must be an integer >= {minimum}")
    created_at = value["created_at"]
    try:
        timestamp = datetime.fromisoformat(created_at)
    except (TypeError, ValueError) as exc:
        raise ProjectFormatError("Invalid recovery timestamp") from exc
    if timestamp.tzinfo is None:
        raise ProjectFormatError("Recovery timestamp must specify a timezone")
    original_path = value["original_path"]
    if original_path is not None and (
        not isinstance(original_path, str) or not original_path
    ):
        raise ProjectFormatError("Recovery original path must be a nonempty string")
    if value["pending"] is not None and not isinstance(value["pending"], dict):
        raise ProjectFormatError("Recovery pending command must be an object")
    digest = value.pop("sha256")
    try:
        canonical = _json_bytes(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProjectFormatError(
            "Recovery snapshot contains invalid JSON data"
        ) from exc
    if not isinstance(digest, str) or digest != hashlib.sha256(canonical).hexdigest():
        raise ProjectFormatError("Recovery integrity hash mismatch")
    validate_project_document(value["project"])
    return RecoverySnapshot(
        run_id=run_id,
        revision=value["revision"],
        generation=value["generation"],
        created_at=created_at,
        original_path=original_path,
        _project_json=_json_bytes(value["project"]),
        _pending_json=_json_bytes(value["pending"]),
    )


def _read(path: Path, run_id: str) -> tuple[RecoverySnapshot, bytes]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ProjectFormatError("Recovery generation must be a regular file")
        if info.st_size > PROJECT_SIZE_LIMIT_BYTES:
            raise ProjectFormatError("Recovery snapshot exceeds size limit")
        content = stream.read(PROJECT_SIZE_LIMIT_BYTES + 1)
    return _decode(content, run_id), content


def _latest(directory: Path, run_id: str) -> tuple[RecoverySnapshot, bytes]:
    for name in ("current.json", "previous.json"):
        try:
            return _read(directory / name, run_id)
        except (OSError, ProjectFormatError):
            continue
    raise ProjectFormatError(f"No valid recovery generation for run {run_id}")


class RecoveryStore:
    """Own one run's journal and inspect only abandoned runs under their locks."""

    def __init__(
        self, root: str | Path | None = None, *, run_id: str | None = None
    ) -> None:
        """Acquire one run's lifetime lock before writing any recovery state."""
        self.root = Path(root) if root is not None else state_directory() / "recovery"
        self.run_id = _run_id(run_id or str(uuid.uuid4()))
        self.directory = self.root / self.run_id
        ensure_directory(self.directory)
        if self.directory.is_symlink():
            raise ProjectFormatError("Recovery directory must not be a symlink")
        self._lock = FileLock(self.directory / "owner.lock")
        if not self._lock.acquire():
            raise BlockingIOError("Recovery run is in use")
        self._generation = 0
        try:
            self._generation = _latest(self.directory, self.run_id)[0].generation
        except ProjectFormatError:
            pass
        sync_directory(self.root)

    def checkpoint(
        self,
        project: Project,
        *,
        revision: int,
        original_path: str | None = None,
        pending: Mapping[str, Any] | None = None,
    ) -> RecoverySnapshot:
        """Persist a complete immutable snapshot, retaining the last valid state."""
        if not self._lock.writable:
            raise RuntimeError("Recovery store is closed or belongs to another process")
        document = project.to_dict()
        validate_project_document(document)
        document = Project.from_dict(document).to_dict()
        value = {
            "schema_version": 1,
            "run_id": self.run_id,
            "revision": revision,
            "generation": self._generation + 1,
            "created_at": datetime.now(UTC).isoformat(),
            "original_path": original_path,
            "pending": dict(pending) if pending is not None else None,
            "project": document,
        }
        if _bounded_json_depth(value, limit=MAX_GRAPH_DEPTH) > MAX_GRAPH_DEPTH:
            raise ProjectFormatError("Recovery snapshot exceeds depth limit")
        _check_finite_recursive(value)
        try:
            digest = hashlib.sha256(_json_bytes(value)).hexdigest()
            content = _json_bytes({**value, "sha256": digest})
        except (ValueError, TypeError, RecursionError) as exc:
            raise ProjectFormatError(
                "Recovery snapshot must contain only JSON data"
            ) from exc
        snapshot = _decode(content, self.run_id)
        try:
            _, previous = _latest(self.directory, self.run_id)
        except ProjectFormatError:
            previous = None
        if previous is not None:
            atomic_write_bytes(self.directory / "previous.json", previous)
        atomic_write_bytes(self.directory / "current.json", content)
        self._generation = snapshot.generation
        return snapshot

    def _candidate_lock(self, run_id: str) -> FileLock:
        directory = self.root / _run_id(run_id)
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectFormatError("Recovery run directory does not exist")
        lock = FileLock(directory / "owner.lock")
        if not lock.acquire():
            raise BlockingIOError("Recovery run is in use by another session")
        return lock

    def load(self, run_id: str) -> RecoverySnapshot:
        """Validate an abandoned run without claiming, deleting, or executing it."""
        lock = self._candidate_lock(run_id)
        try:
            return _latest(self.root / run_id, run_id)[0]
        finally:
            lock.close()

    def candidates(self) -> list[RecoverySnapshot]:
        """List valid abandoned generations, leaving corrupt and live runs alone."""
        result = []
        for directory in self.root.iterdir():
            try:
                result.append(self.load(directory.name))
            except (ProjectFormatError, OSError):
                continue
        return sorted(result, key=lambda item: item.created_at, reverse=True)

    def discard(self, run_id: str) -> None:
        """Remove abandoned generations only while owning that run's lock."""
        lock = self._candidate_lock(run_id)
        try:
            self._remove_generations(self.root / run_id)
        finally:
            lock.close()

    @staticmethod
    def _remove_generations(directory: Path) -> None:
        for name in ("current.json", "previous.json"):
            cleanup_atomic_writes(directory / name)
        for name in ("current.json", "previous.json"):
            (directory / name).unlink(missing_ok=True)
        sync_directory(directory)

    def close(self, *, clean: bool = False) -> None:
        """Release ownership; explicitly clean only this run after normal exit."""
        try:
            if clean and self._lock.writable:
                self._remove_generations(self.directory)
        finally:
            self._lock.close()
